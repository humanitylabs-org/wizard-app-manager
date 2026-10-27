"""Keep clean-main installation failure separate from candidate acceptance."""
import ast
from pathlib import Path
import subprocess
import json
import os

import pytest


RUNNER = Path(__file__).resolve().parents[1] / 'qa/privsep-slice3/run.py'


def runner_helpers():
    tree = ast.parse(RUNNER.read_text())
    names = {'INSTALL_PRIVATE_ACCESS', 'INSTALL_INPUT_DIFFERENCE', 'guest_bootstrap_command',
             'KNOWN_BASELINE_SHA', 'BASELINE_REASON', 'BASELINE_PROBE',
             'baseline_failure_matches', 'finalize_receipt'}
    nodes: list[ast.stmt] = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in names)
             or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in names)]
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(RUNNER), 'exec'), ns)
    return ns


def test_baseline_install_skips_only_unauthenticated_private_access_gate():
    ns = runner_helpers()
    ref = 'a' * 40
    baseline = ns['guest_bootstrap_command'](ref, 'baseline')
    candidate = ns['guest_bootstrap_command'](ref, 'candidate')
    assert 'WIZARD_PRIVATE_ACCESS=none' in baseline
    assert 'WIZARD_PRIVATE_ACCESS=tailscale' in candidate
    # Retain candidate operator provisioning and its real mutation-denial test.
    assert baseline.replace('WIZARD_PRIVATE_ACCESS=none',
                            'WIZARD_PRIVATE_ACCESS=tailscale') == candidate
    for command in (baseline, candidate):
        assert 'WAM_REF=' + ref in command
        assert 'WIZARD_MODE=alongside' in command
        assert 'WIZARD_HERMES_USER=hermes' in command
        assert 'HERMES_HOME=/home/hermes/.hermes' in command
        assert 'WIZARD_APPROVED' not in command  # no approval bypass
        assert '&&' in command  # never execute a stale download on curl failure
        subprocess.run(['bash', '-n'], input=command, text=True, check=True)
        assert 'bash -s start < /root/get.sh' in command
        assert 'bash -s status < /root/get.sh' in command
        assert 'WIZARD_AGENT_SAFE=1' in command
        assert 'SECONDS + 3000' in command
    assert 'start/status' in ns['INSTALL_INPUT_DIFFERENCE']


def test_private_access_difference_is_in_machine_readable_receipt():
    tree = ast.parse(RUNNER.read_text())
    receipt = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'receipt')
    assert isinstance(receipt, ast.Dict)
    values = {ast.literal_eval(k): v for k, v in zip(receipt.keys, receipt.values)
              if k is not None}
    assert isinstance(values['install_private_access'], ast.Name)
    assert values['install_private_access'].id == 'INSTALL_PRIVATE_ACCESS'
    assert isinstance(values['install_input_difference'], ast.Name)
    assert values['install_input_difference'].id == 'INSTALL_INPUT_DIFFERENCE'
    vm = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'guest_vm')
    install = next(n.value for n in ast.walk(vm) if isinstance(n, ast.Assign)
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'install')
    assert ast.unparse(install) == 'guest_bootstrap_command(ref, label)'


@pytest.fixture
def known_failure():
    # Reduced non-secret fields from the clean 20261027T220027Z guest.
    return {'status': {'state': 'failed', 'exit_code': 3,
            'failure_reason': 'Administrator rights are needed once for: host preparation'},
            'runtime_env': True, 'user_bus': False, 'docker_info': True,
            'sg_docker_info': True, 'named_host_ready_exit': 1, 'user_manager': 'active',
            'linger': 'yes', 'checkout_exists': False, 'sudo_available': False}


KNOWN_LOG = ('Administrator host preparation complete for hermes; no passwordless sudo was granted.\n'
             '[wizard-os] owner-admin: The account hermes is not in this server\n')


def test_only_exact_documented_main_failure_is_accepted(known_failure):
    ns = runner_helpers()
    matches = ns['baseline_failure_matches']
    assert matches(ns['KNOWN_BASELINE_SHA'], 3, known_failure, KNOWN_LOG)
    assert not matches('b' * 40, 3, known_failure, KNOWN_LOG)
    for code in (0, 1, 2, 124, 125):
        assert not matches(ns['KNOWN_BASELINE_SHA'], code, known_failure, KNOWN_LOG)
    assert not matches(ns['KNOWN_BASELINE_SHA'], 3, known_failure, '')
    assert not matches(ns['KNOWN_BASELINE_SHA'], 3, {}, KNOWN_LOG)


@pytest.mark.parametrize('key,value', [('runtime_env', False), ('user_bus', True),
    ('docker_info', False), ('sg_docker_info', False), ('named_host_ready_exit', 2),
    ('user_manager', 'inactive'), ('linger', 'no'), ('checkout_exists', True),
    ('sudo_available', True), ('status', {'state': 'done', 'exit_code': 0}),
    ('status', {'state': 'failed', 'exit_code': 3, 'failure_reason': 'network error'})])
def test_different_baseline_failures_are_not_waived(known_failure, key, value):
    ns = runner_helpers()
    known_failure[key] = value
    assert not ns['baseline_failure_matches'](ns['KNOWN_BASELINE_SHA'], 3, known_failure, KNOWN_LOG)


def complete_receipt():
    names = ('fresh', 'tools', 'stock-hermes', 'install', 'mirror', 'source',
             'doctor', 'denials', 'actions', 'service-uid')
    return {'steps': [{'name': 'candidate-' + n, 'exit': 0} for n in names],
            'vms': [{'name': 'candidate', 'stopped': True}], 'failures': [],
            'smoke': False, 'candidate_doctor_passed': True,
            'baseline_status': 'cannot complete on main: missing user bus',
            'baseline_failure_matches_documented': True}


def test_candidate_result_and_baseline_result_are_separate():
    ns = runner_helpers()
    value = complete_receipt()
    ns['finalize_receipt'](value)
    assert value['candidate_passed'] and value['passed']
    value['baseline_failure_matches_documented'] = False
    ns['finalize_receipt'](value)
    assert value['candidate_passed'] and not value['passed']
    value['baseline_status'] = 'completed'
    ns['finalize_receipt'](value)
    assert value['passed']
    value['failures'].append('full pytest differs from known pre-existing failure')
    ns['finalize_receipt'](value)
    assert value['candidate_passed'] and not value['passed']


@pytest.mark.parametrize('mutation', ['missing_action', 'failed_denial', 'running_vm', 'no_vm', 'failed_doctor'])
def test_expected_baseline_bug_cannot_hide_incomplete_candidate(mutation):
    ns = runner_helpers()
    value = complete_receipt()
    if mutation == 'missing_action': value['steps'].pop(-2)
    if mutation == 'failed_denial': value['steps'][-3]['exit'] = 1
    if mutation == 'running_vm': value['vms'][0]['stopped'] = False
    if mutation == 'no_vm': value['vms'] = []
    if mutation == 'failed_doctor': value['candidate_doctor_passed'] = False
    ns['finalize_receipt'](value)
    assert not value['candidate_passed'] and not value['passed']


@pytest.mark.parametrize('state,exit_code,expected', [('done', 0, 0), ('failed', 3, 3),
    ('failed', 1, 1), ('needs_admin', 3, 3), ('stopped', 0, 125), ('timeout', 0, 124)])
def test_detached_route_reads_completion_not_start_exit(tmp_path, state, exit_code, expected):
    ns = runner_helpers()
    script = tmp_path / 'get.sh'
    status = tmp_path / 'install-status.json'
    log = tmp_path / 'install.log'
    fixture = tmp_path / 'fixture.sh'
    fixture.write_text('#!/bin/bash\nif [ "$1" = start ]; then\n  printf \'%s\' \'STATUS_JSON\' > \'STATUS_PATH\'\n  printf \'fixture install log\' > \'LOG_PATH\'\nelse\n  echo \'Wizard OS install: STATE\'\nfi\n'.replace('STATUS_JSON', json.dumps({'state': state, 'exit_code': exit_code}))
.replace('STATUS_PATH', str(status)).replace('LOG_PATH', str(log)).replace('STATE', state))
    curl = tmp_path / 'curl'
    curl.write_text('#!/bin/bash\ncp "' + str(fixture) + '" "${@: -1}"\n')
    curl.chmod(0o755)
    command = ns['guest_bootstrap_command']('a' * 40, 'baseline')
    command = command.replace('/root/get.sh', str(script)).replace('/root/.wizard-os', str(tmp_path))
    if state == 'timeout': command = command.replace('SECONDS + 3000', 'SECONDS + 0')
    result = subprocess.run(['bash', '-se'], input=command, text=True, capture_output=True,
                            env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']}, timeout=5)
    assert result.returncode == expected, result.stderr
    assert 'fixture install log' in result.stdout


def test_probe_executes_named_conditions_as_owner_via_stdin():
    probe = runner_helpers()['BASELINE_PROBE']
    assert "'bash','-s'" in probe  # sudo -i must not reparse a multiline bash -c argument
    assert "input=command+'\\n'" in probe
    subprocess.run(['bash', '-n'], input=probe, text=True, check=True)
    compile(probe.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0], 'probe', 'exec')


def test_failed_download_never_runs_stale_installer(tmp_path):
    ns = runner_helpers()
    script = tmp_path / 'get.sh'
    marker = tmp_path / 'executed'
    script.write_text('touch ' + str(marker))
    curl = tmp_path / 'curl'
    curl.write_text('#!/bin/bash\nexit 22\n')
    curl.chmod(0o755)
    command = ns['guest_bootstrap_command']('a' * 40, 'baseline').replace('/root/get.sh', str(script))
    result = subprocess.run(['bash', '-se'], input=command, text=True, capture_output=True,
                            env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']}, timeout=5)
    assert result.returncode == 22
    assert not marker.exists()
