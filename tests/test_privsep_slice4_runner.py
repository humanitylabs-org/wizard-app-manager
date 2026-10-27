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
