"""Acceptance must not mistake a tailnet-login gate for a broken main install."""
import ast
from pathlib import Path
import subprocess


RUNNER = Path(__file__).resolve().parents[1] / 'qa/privsep-slice3/run.py'


def runner_helpers():
    tree = ast.parse(RUNNER.read_text())
    names = {'INSTALL_PRIVATE_ACCESS', 'INSTALL_INPUT_DIFFERENCE', 'guest_bootstrap_command'}
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
    assert 'OperatorUser' in ns['INSTALL_INPUT_DIFFERENCE']
    assert 'NeedsLogin' in ns['INSTALL_INPUT_DIFFERENCE']


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
