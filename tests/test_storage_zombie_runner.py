"""Queued static/collector controls; no development-host process fixtures."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest

REPO=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    assert spec is not None and spec.loader is not None
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def runner():return load('zombie_runner_under_test',REPO/'qa/privsep-storage/zombie_run.py')


def exited():
    return {'fixture':'storage-child-reap-v1','mode':'exited','passed':True,'production_acceptance':False,
        'controls':dict.fromkeys(('actual_zombie_barrier','old_guard_refused','repaired_guard_admitted',
            'zombie_absent','repeated_fence_admitted','fixed_source_metadata_unchanged'),True)}


def test_complete_causal_row_accepted_not_production_acceptance():
    row=exited();assert runner().parse_fixture(json.dumps(row).encode(),'exited')==row


def test_clean_boot_without_forced_zombie_is_not_causal_proof():
    row=exited();del row['controls']['actual_zombie_barrier']
    with pytest.raises(AssertionError):runner().parse_fixture(json.dumps(row).encode(),'exited')


def test_missing_mutant_refusal_is_not_causal_proof():
    row=exited();row['controls']['old_guard_refused']=False
    with pytest.raises(AssertionError):runner().parse_fixture(json.dumps(row).encode(),'exited')


def test_duplicate_or_unbounded_or_canary_serial_refuses():
    raw=json.dumps(exited()).encode();m=runner()
    for data in (raw+b'\n'+raw,b'x'*1048577,b'QA_PRIVATE_CANARY\n'+raw):
        with pytest.raises(AssertionError):m.parse_fixture(data,'exited')


def test_fixture_error_never_becomes_guard_pass():
    row=exited();row['passed']=False
    with pytest.raises(AssertionError):runner().parse_fixture(json.dumps(row).encode(),'exited')


def test_live_writer_needs_progress_after_refusal():
    row={'fixture':'storage-child-reap-v1','mode':'live','passed':True,'production_acceptance':False,
         'controls':dict.fromkeys(('live_child_refused','same_live_child_after_guard',
              'no_guard_wait_or_kill','fixed_source_metadata_unchanged'),True)}
    with pytest.raises(AssertionError):runner().parse_fixture(json.dumps(row).encode(),'live')
    row['controls']['writer_progress_after_refusal']=True
    assert runner().parse_fixture(json.dumps(row).encode(),'live')==row


def test_namespace_control_needs_unreaped_subject_and_no_reaper():
    row={'fixture':'storage-child-reap-v1','mode':'nested','passed':True,'production_acceptance':False,
         'controls':{'actual_nested_pid1_refused_before_reaper':True}}
    with pytest.raises(AssertionError):runner().parse_fixture(json.dumps(row).encode(),'nested')


def test_exact_original_mutant_delta_and_no_other_guard_change():
    m=runner();current=(REPO/'scripts/protected-agent-storage.py').read_text();tree=ast.parse(current)
    tree.body=[n for n in tree.body if not (isinstance(n,ast.FunctionDef) and n.name in ('wait_exited_child','reap_exited_children'))
        and not (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CHILD_REAP_CALL_LIMIT' for t in n.targets))]
    fence=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='require_fence')
    fence.body=[n for n in fence.body if not (isinstance(n,ast.Expr) and ast.unparse(n)=='reap_exited_children()')]
    original=ast.unparse(tree)
    assert m.fence_delta(original,current)
    with pytest.raises(AssertionError):m.fence_delta(original,current.replace('len(threads) != 1','len(threads) != 2'))


def test_real_kernel_fixture_uses_wnowait_and_scoped_children():
    text=(REPO/'qa/privsep-storage/zombie_init.py').read_text();tree=ast.parse(text)
    funcs={n.name:ast.unparse(n) for n in tree.body if isinstance(n,ast.FunctionDef)}
    assert 'os.WEXITED | os.WNOWAIT' in funcs['zombie']
    assert 'waitpid' not in funcs['zombie'] and 'sleep' not in funcs['zombie']
    assert funcs['exited_case'].index('old_fence(m)') < funcs['exited_case'].index("assert m.require_fence()['userspace_tasks']")
    assert 'os.kill' not in text and 'systemd-udevd' in funcs['live_case']
    assert 'state(pid) == observed' in funcs['nested_case']


def test_real_constructor_manifest_complete_before_vm(tmp_path):
    fixtures=load('production_fixtures_for_reap',REPO/'tests/test_storage_production_integration.py')
    production,base,stage,doc=fixtures.constructor_fixture(tmp_path)
    (stage/'zombie-runner.py').write_bytes((REPO/'qa/privsep-storage/zombie_run.py').read_bytes())
    cls=runner().make_runner(production,base);r=cls(stage,doc)
    assert r.receipt['revision']==doc['revision'] and r.proc is None
    assert r.receipt['product_sha256']==doc['product_sha256']
    assert r.receipt['causal_cases']==[] and r.receipt['causal_proof_complete'] is False
    assert r.receipt['service_revision']==doc['service_revision']
    assert production.validate_runner_contract(stage,doc)['contract_validated']


def test_production_continuation_preserves_fourteen_case_oracle():
    source=(REPO/'qa/privsep-storage/zombie_run.py').read_text();tree=ast.parse(source)
    cls=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='ReapRunner')
    execute=ast.unparse(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='execute'))
    assert execute.index("self.causal('exited')") < execute.index("self.causal('live')") < execute.index('self.continue_production()')
    assert 'Transaction(' not in execute and 'self.doc[' not in execute
    assert 'expected_pytest_cases' in source and 'base.PENDING = PENDING' in ast.unparse(tree)
    m=load('production_cases_reap',REPO/'qa/privsep-storage/production_run.py')
    assert len(m.CASES)==14 and len(set(m.CASES))==14


def test_refusal_negatives_pin_intended_reasons_not_any_failure():
    text=(REPO/'qa/privsep-storage/production_run.py').read_text()
    for reason in ('wrong bootstrap refusal reason','wrong request refusal reason','request guard never reached',
                   'readonly-root','another-userspace-process','unsafe control record'):
        assert reason in text
