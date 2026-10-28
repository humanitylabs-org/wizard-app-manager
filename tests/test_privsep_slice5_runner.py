import importlib.util
import json
from pathlib import Path
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('slice5_runner', ROOT / 'qa/privsep-slice5/run.py')
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)


def test_staged_arguments_prelude_and_skeleton_load_without_vm(tmp_path):
    source = tmp_path / 'source'
    for part in ('privsep-slice1', 'privsep-slice4', 'privsep-slice5'):
        shutil.copytree(ROOT / 'qa' / part, source / 'qa' / part)
    sha = 'b' * 40
    (tmp_path / 'source-export.json').write_text(json.dumps({'head_sha': sha}))
    evidence = Path('/srv/work/privsep/slice5/test-preflight-' + uuid.uuid4().hex)
    assert runner.main([str(evidence), str(source), sha, '--preflight']) == 0
    runner.load_skeleton(source)
    assert callable(runner.guest_vm) and callable(runner.suite)
    assert not evidence.exists()


def test_final_receipt_requires_all_phases_real_doctor_and_stopped_vm():
    names = ('fresh', 'tools', 'stock-hermes', 'install', 'mirror', 'source', 'doctor',
             'slice5-script', 'slice5-before', 'slice5-grant', 'slice5-expiry', 'slice5-off')
    value = {'steps': [{'name': 'candidate-' + name, 'exit': 0} for name in names],
        'vms': [{'stopped': True}], 'smoke': False, 'candidate_doctor_passed': True,
        'pytest': {'failures': ['tests.test_chats_views::test_typescript_public_index_path_has_identical_seal']}, 'failures': []}
    runner.finalize(value); assert value['passed']
    value['vms'][0]['stopped'] = False
    runner.finalize(value); assert not value['passed']
    value['vms'][0]['stopped'] = True
    value['steps'][-2]['exit'] = 1
    runner.finalize(value); assert not value['passed']
    value['steps'][-2]['exit'] = 0
    value['pytest']['failures'] = []
    runner.finalize(value); assert not value['passed']  # expected baseline cannot silently disappear
