"""Slice 4 must not turn an unrun suite or an incomplete VM into a pass."""
import ast
from pathlib import Path
import subprocess

import pytest

RUNNER = Path(__file__).resolve().parents[1] / 'qa/privsep-slice4/run.py'


def helper():
    tree = ast.parse(RUNNER.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'finalize_receipt')
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(RUNNER), 'exec'), scope)
    names = ast.literal_eval(fn.body[0].value.generators[0].iter)
    return scope['finalize_receipt'], names


def receipt():
    _, names = helper()
    return {'steps': [{'name': 'candidate-' + n, 'exit': 0} for n in names],
            'vms': [{'name': 'candidate', 'stopped': True}], 'failures': [], 'smoke': False,
            'candidate_doctor_passed': True,
            'baseline_status': 'not rerun: main 67141228 accepted Slice 3 baseline',
            'pytest': {'failures': ['tests.test_chats_views::test_typescript_public_index_path_has_identical_seal']}}


def test_candidate_only_requires_all_slice4_actions():
    finalize, names = helper()
    for name in ('slice4-backup', 'slice4-restore', 'slice4-undo', 'slice4-links', 'slice4-build',
                 'slice4-hermes', 'slice4-denials', 'slice4-app-restored', 'slice4-app-undone',
                 'slice4-system-owned-build'):
        assert name in names
        doc = receipt()
        doc['steps'] = [r for r in doc['steps'] if r['name'] != 'candidate-' + name]
        finalize(doc)
        assert not doc['passed']
    doc = receipt(); finalize(doc)
    assert doc['passed']


@pytest.mark.parametrize('mutation', ['suite_missing', 'suite_extra_failure', 'no_vm', 'running_vm', 'failed_step'])
def test_slice4_missing_proof_never_passes(mutation):
    finalize, _ = helper(); doc = receipt()
    if mutation == 'suite_missing': doc.pop('pytest')
    if mutation == 'suite_extra_failure': doc['pytest']['failures'].append('extra')
    if mutation == 'no_vm': doc['vms'] = []
    if mutation == 'running_vm': doc['vms'][0]['stopped'] = False
    if mutation == 'failed_step': doc['steps'][-1]['exit'] = 1
    finalize(doc)
    assert not doc['passed']


def test_guest_boot_and_teardown_are_reused_and_no_stock_hermes_edit():
    text = RUNNER.read_text()
    assert 'qa/privsep-slice1/run.py' in text
    assert 'proc.kill(); proc.wait(timeout=15)' in text
    assert 'mirror.gcr.io' in text and 'finally:' in text
    assert 'not yet available without server access' not in text
    assert "grep -q 'usage:'" in text
    assert 'source ./activate' in text and 'sync_venv' in text
    assert "guest_vm('baseline'" not in text
    guest = (RUNNER.parent / 'guest.py').read_text()
    for verb in ("'backup', 'run'", "'backup', 'snapshots'", "'backup', 'import', '--from-repo'",
                 "'backup', 'undo'", "'builder', 'install-local'", "'builder', 'update-local'",
                 "'hermes', 'update', '--check'"):
        assert verb in guest
    assert "escape.symlink_to(target)" in guest and '/:/host' in guest
    compile(guest, 'guest.py', 'exec')
    tree = ast.parse(text)
    prep = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'prep')
    subprocess.run(['bash', '-n'], input=prep, text=True, check=True)


@pytest.mark.parametrize('failure', ['nonzero', 'exception'])
@pytest.mark.parametrize('failed_phase', ['links', 'restore', 'build'])
def test_slice4_phase_failure_does_not_hide_later_phases(tmp_path, failure, failed_phase):
    import hashlib
    import traceback

    tree = ast.parse(RUNNER.read_text())
    names = {'require_step', 'run_independent_phases'}
    scope = {'root': tmp_path, 'receipt': receipt(), 'save': lambda: None,
             'hashlib': hashlib, 'traceback': traceback}
    scope['receipt']['steps'] = []
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                                 and n.name in names], type_ignores=[]), str(RUNNER), 'exec'), scope)
    seen = []

    def phase(name, timeout=1800, required=True):
        assert not required  # the independent collector owns failure accounting
        seen.append(name)
        if name == failed_phase and failure == 'exception':
            raise RuntimeError('injected transport failure')
        code = 1 if name == failed_phase else 0
        scope['receipt']['steps'].append({'name': 'candidate-slice4-' + name, 'exit': code})
        return code

    def owned():
        seen.append('system-owned-build')
        scope['receipt']['steps'].append({'name': 'candidate-slice4-system-owned-build', 'exit': 0})
        return 0

    def service():
        seen.append('service-uid')
        scope['receipt']['steps'].append({'name': 'candidate-service-uid', 'exit': 0})
        return 0

    def guest(name, command, timeout):
        seen.append(name.removeprefix('slice4-'))
        scope['receipt']['steps'].append({'name': 'candidate-' + name, 'exit': 0})
        return 0

    scope.update(agent_phase=phase, system_owned_build=owned, service_uid=service, guest=guest)
    # Exercise the actual list wired into guest_vm, not a parallel test-only list.
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'outcomes'
                      and isinstance(n.value, ast.Call))
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(RUNNER), 'exec'), scope)
    assert seen == ['backup', 'app-mutate', 'restore', 'app-restored', 'undo', 'app-undone',
                    'links', 'build', 'system-owned-build', 'hermes', 'denials', 'service-uid']
    failed_name = 'candidate-slice4-' + failed_phase
    assert scope['receipt']['failures'] == [failed_name]
    assert scope['outcomes'][failed_name] == 1
    assert all(code == 0 for name, code in scope['outcomes'].items() if name != failed_name)
    finalize, _ = helper()
    finalize(scope['receipt'])
    assert not scope['receipt']['passed']


def test_slice4_deadline_interrupt_still_tears_down_instead_of_continuing(tmp_path):
    tree = ast.parse(RUNNER.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_independent_phases')
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(RUNNER), 'exec'), scope)
    seen = []

    def deadline():
        raise InterruptedError('deadline')

    with pytest.raises(InterruptedError):
        scope['run_independent_phases']([('deadline', deadline), ('later', lambda: seen.append(True))])
    assert not seen


@pytest.mark.parametrize('scenario', ['ready', 'timeout', 'unrelated_error'])
def test_link_readiness_wait_is_bounded_and_only_retries_admission(scenario):
    import json
    from types import SimpleNamespace

    tree = ast.parse((RUNNER.parent / 'guest.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'wait_private_link')
    clock = [0]
    calls = []
    rows = []
    result = {'operator_uid': 991, 'state': 'NeedsLogin', 'published': False}

    def wam(*argv, code):
        assert code == (0, 1)
        calls.append(argv)
        ready = scenario == 'ready' and len(calls) == 2
        error = ('operator permission denied' if scenario == 'unrelated_error'
                 else 'installed running app with a loopback page required')
        rows.append({'exit': 0 if ready else 1, 'stderr': json.dumps({'error': error})})
        return result if ready else ''

    def sleep(seconds):
        clock[0] += seconds

    scope = {'wam': wam, 'results': rows, 'json': json,
             'time': SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'guest.py', 'exec'), scope)
    if scenario == 'ready':
        assert scope['wait_private_link']('publish', 2) == result
        assert len(calls) == 2 and clock[0] == 1
    else:
        with pytest.raises(AssertionError):
            scope['wait_private_link']('publish', 2)
        assert len(calls) == (1 if scenario == 'unrelated_error' else 3)
        assert clock[0] <= 2
