"""Focused storage contract regressions. Kernel/VM claims live in the queued rig.

No pytest fixture here pretends a mock stopped flag is a production writer fence.
These tests are queued to forum, not run on the development/live host.
"""
import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO=Path(__file__).resolve().parents[1]
PRODUCT=REPO/'scripts/protected-agent-storage.py'


def load():
    spec=importlib.util.spec_from_file_location('storage_under_test',PRODUCT)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_normal_process_cannot_mutate_even_when_root_or_stopped(monkeypatch,tmp_path):
    m=load()
    monkeypatch.setattr(m.os,'getuid',lambda:0);monkeypatch.setattr(m.os,'geteuid',lambda:0)
    monkeypatch.setattr(m.os,'getpid',lambda:1234)
    monkeypatch.setattr(m.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=1100,pw_gid=1100,pw_name=name,pw_dir='/home/hermes'))
    monkeypatch.setattr(m,'ANCHOR',tmp_path/'physical');monkeypatch.setattr(m,'CONTROL',tmp_path/'control')
    with pytest.raises(m.Refusal,match='maintenance-needed'):
        m.Transaction('hermes','/home/hermes/.hermes').run()
    assert not (tmp_path/'physical').exists() and not (tmp_path/'control').exists()


@pytest.mark.parametrize('logical',('/home/hermes/../.hermes','/home/hermes//.hermes','relative','/home/hermes/.hermes/'))
def test_noncanonical_source_token_not_repaired(logical):
    with pytest.raises(ValueError):load().clean_path(logical)


def test_snapshot_rollback_guard_precedes_all_mutation(monkeypatch,tmp_path):
    m=load();calls=[]
    monkeypatch.setattr(m,'require_fence',lambda:calls.append('fence'))
    def guard():calls.append('db-intent-guard');raise m.Refusal('db_intent-or-later')
    monkeypatch.setattr(m,'rollback_guard',guard)
    monkeypatch.setattr(m.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=1100,pw_gid=1100,pw_name=name,pw_dir='/home/hermes'))
    monkeypatch.setattr(m,'ANCHOR',tmp_path/'physical');monkeypatch.setattr(m,'CONTROL',tmp_path/'control')
    with pytest.raises(m.Refusal,match='db_intent'):
        m.Transaction('hermes','/home/hermes/.hermes').run('rollback')
    assert calls==['fence','db-intent-guard'] and list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('phase',('db_intent','db_rotated','queue_intent','queue_rotated','file_committed','restarted','verified','unknown'))
def test_credential_intent_is_forward_only(monkeypatch,phase):
    m=load();monkeypatch.setattr(m,'protected_json',lambda *a,**k:{'phase':phase})
    with pytest.raises(m.Refusal,match='forward-only'):m.rollback_guard()


@pytest.mark.parametrize('phase',('prepared','quiesced','aborted'))
def test_precredential_mutation_guard_allows_rollback(monkeypatch,phase):
    m=load();monkeypatch.setattr(m,'protected_json',lambda *a,**k:{'phase':phase})
    m.rollback_guard()


def test_installer_and_updater_preflight_precede_mutations():
    get=(REPO/'get.sh').read_text();install=(REPO/'install.sh').read_text()
    begin=get.index('if separated_manager; then',get.index('storage_preflight()'))
    assert get.index('storage_preflight check-pairing',begin)<get.index('MODE=alongside; export',begin)
    branch=get.index('storage_preflight check "$ALONG_USER" "$ALONG_HOME"')
    assert branch<get.index('    prepare_server',branch)<get.index('prepare-alongside.py',branch)
    assert install.index('check-pairing')<install.index('(cd "$HERE" && python3 -m wam.release_pins)')
    prep=(REPO/'scripts/prepare-alongside.py').read_text()
    assert prep.index("storage['require_mapping'](name, hh)")<prep.index("groups = {")<prep.index('BASE.mkdir')


def test_legacy_migration_storage_actions_are_one_primitive_not_a_new_migrator():
    text=(REPO/'wam/privilege_migration.py').read_text()
    body=text[text.index('def main('):]
    assert 'storage-relocate' in body and 'storage-rollback' in body
    assert body.index("storage['Transaction']")<body.index('CONTROL.mkdir')
    assert body.index("rollback_allowed(CONTROL/'credentials')")<body.index('CONTROL.mkdir')
    assert body.index("storage['require_mapping'](agent, logical)")<body.index('CONTROL.mkdir')
    assert 'NOT a storage writer fence' in text


def test_samefs_rename_crossfs_opaque_stage_and_no_cleanup_delete():
    text=PRODUCT.read_text();tree=ast.parse(text)
    assert "'method': 'rename' if st.st_dev == os.fstat(self.dest.fd).st_dev else 'copy'" in text
    assert 'rename_new(self.home, self.logical.name, self.dest, str(self.uid))' in text
    assert "self.transfer(self.control/self.doc['snapshot'], ANCHOR/name)" in text
    assert 'RENAME_NOREPLACE' not in text or 'renameat2' in text
    assert not any(isinstance(n,ast.Attribute) and n.attr in ('rmtree','chmod','chown','system') for n in ast.walk(tree))
    # Only fixed control files and the selected compatibility symlink are unlinked;
    # no recursive cleanup or home-ownership repair is a product recovery action.
    assert 'os.unlink(self.logical.name, dir_fd=self.home.fd)' in text
    assert 'os.fchmod(fd, mode)' in text


def test_runtime_inventory_binds_actual_power_cut_and_respawner_cases():
    guest=(REPO/'qa/privsep-storage/guest.py').read_text()
    host=(REPO/'qa/privsep-storage/run.py').read_text()
    assert 'Restart=always' in guest and 'removed_fence_permitted_forbidden_transaction' in guest
    assert 'real_payload_write_syscalls_observed' in guest
    assert "os.getpid() == 1" in guest and "os.getpid() != 1" in guest
    assert "signal.SIGKILL if power_cut" in host
    assert 'set(names)==set(self.doc[\'expected_cases\'])' in host
    assert "report.get('skipped')==0" in host and "self.doc['expected_pytest_cases']" in host
    assert 'WIZARD_HEAVY_LOCK_FD' in host and '/srv/work/heavy/production/worker.lock' in host
    assert '217179869184' in host and '21474836480' in host


def _collector_runner(tmp_path):
    """Real collector code, synthetic files/transport only; no VM is started."""
    spec=importlib.util.spec_from_file_location('storage_runner_under_test',REPO/'qa/privsep-storage/run.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    (tmp_path/'runner.py').write_text('synthetic collector fixture\n')
    expected=['tests/test_agent_storage.py::test_synthetic']
    runner=module.Runner(tmp_path,{'revision':'synthetic','expected_pytest_count':1,'expected_pytest_cases':expected})
    # Guard every external seam unless a test explicitly supplies a fake.
    def forbidden(*args,**kwargs):raise AssertionError('external command forbidden in collector unit fixture')
    runner.guest=forbidden;runner.copy=forbidden;runner.start=forbidden
    return runner


@pytest.mark.parametrize('artifact', ('failed-xml','missing-xml','malformed-xml'))
def test_pytest_nonzero_exports_and_parses_before_preserving_original_failure(tmp_path,artifact):
    runner=_collector_runner(tmp_path);events=[]
    def guest(*args,**kwargs):
        assert kwargs['required'] is False;events.append('pytest');return 1
    def copy(local,remote,download,**kwargs):
        assert download and kwargs['required'] is False;events.append('export')
        if artifact=='missing-xml':return 1
        local.write_text('<testsuites><testsuite><testcase classname="tests.test_agent_storage" name="test_synthetic"><failure/></testcase></testsuite></testsuites>'
                         if artifact=='failed-xml' else '<broken')
        return 0
    runner.guest=guest;runner.copy=copy
    with pytest.raises(AssertionError,match='fixed phase failed: focused-tests'):
        runner.focused_tests()
    assert events==['pytest','export']
    report=runner.receipt['focused_pytest']
    assert runner.receipt['checkpoints'][-1]['name']=='focused-tests-result-collected'
    assert report['exit']==1 and not runner.receipt['passed']
    if artifact=='failed-xml':assert report['actual_count']==report['failures']==1 and report['inventory_matches']
    elif artifact=='missing-xml':assert report['copy_exit']==1
    else:assert report['collection_error']=='ParseError'


@pytest.mark.parametrize('variant', ('exact','missing','wrong-id','duplicate','skipped'))
def test_pytest_zero_requires_exact_complete_unique_case_inventory(tmp_path,variant):
    runner=_collector_runner(tmp_path)
    runner.guest=lambda *a,**k:0
    def copy(local,*args,**kwargs):
        name='test_other' if variant=='wrong-id' else 'test_synthetic'
        case=f'<testcase classname="tests.test_agent_storage" name="{name}">'+('<skipped/>' if variant=='skipped' else '')+'</testcase>'
        body='' if variant=='missing' else case*(2 if variant=='duplicate' else 1)
        local.write_text('<testsuites><testsuite>'+body+'</testsuite></testsuites>');return 0
    runner.copy=copy
    if variant=='exact':runner.focused_tests()
    else:
        with pytest.raises(AssertionError,match='focused pytest evidence incomplete or nonpassing'):runner.focused_tests()
    assert runner.receipt['focused_pytest']['exit']==0
    assert runner.receipt['checkpoints'][-1]['name']=='focused-tests-result-collected'


def test_early_failure_diagnostics_never_fetch_unentered_storage_phase(tmp_path):
    runner=_collector_runner(tmp_path);stops=[]
    runner.receipt['first_error']={'reason':'fixed phase failed: focused-tests'}
    runner.phase('focused-tests');runner.stop=lambda:stops.append(True)
    runner.diagnostics()
    assert stops and [r['status'] for r in runner.receipt['diagnostics']]==['not_reached']*3
    assert runner.receipt['first_error']=={'reason':'fixed phase failed: focused-tests'}
    assert runner.receipt['cleanup_errors']==runner.receipt['diagnostic_errors']==runner.receipt['boots']==[]


@pytest.mark.parametrize('variant', ('not-reached','optional-absent','expected-absent','probe-failed','copy-failed','malformed','collected'))
def test_diagnostic_absence_is_not_vm_cleanup_and_transport_stays_distinct(tmp_path,variant):
    runner=_collector_runner(tmp_path);calls=[]
    def guest(*args,**kwargs):
        assert kwargs['required'] is False;calls.append('probe')
        return 255 if variant=='probe-failed' else 3 if variant.endswith('absent') else 0
    def copy(local,*args,**kwargs):
        assert kwargs['required'] is False;calls.append('copy')
        if variant=='copy-failed':return 1
        local.write_text('<bad' if variant=='malformed' else '[]');return 0
    runner.guest=guest;runner.copy=copy
    row=runner.collect_artifact('cases.json','failure-cases.json',reached=variant!='not-reached',expected=variant=='expected-absent')
    expected={'not-reached':'not_reached','optional-absent':'missing_optional','expected-absent':'missing_expected',
              'probe-failed':'probe_error','copy-failed':'transport_error','malformed':'collection_error','collected':'collected'}
    assert row['status']==expected[variant] and runner.receipt['cleanup_errors']==[]
    assert bool(runner.receipt['diagnostic_errors']) is (variant in ('expected-absent','probe-failed','copy-failed','malformed'))
    if variant=='not-reached':assert calls==[]
    if variant.endswith('absent') or variant=='probe-failed':assert calls==['probe']


def test_diagnostics_stop_failure_is_not_hidden_by_optional_absence(tmp_path):
    runner=_collector_runner(tmp_path)
    runner.receipt['first_error']={'reason':'original failure'}
    def stop():raise RuntimeError('synthetic stop failure')
    runner.stop=stop;runner.diagnostics()
    assert runner.receipt['cleanup_errors']==['diagnostic-stop:RuntimeError']
    assert runner.receipt['first_error']=={'reason':'original failure'}
    assert not runner.receipt['passed']


def test_runner_phase_exports_precede_required_assertions_and_preserve_case_oracles():
    tree=ast.parse((REPO/'qa/privsep-storage/run.py').read_text())
    runner=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Runner')
    execute=next(n for n in runner.body if isinstance(n,ast.FunctionDef) and n.name=='execute')
    text=ast.unparse(execute)
    assert text.index("self.collect_artifact('cases.json', 'normal-cases.json'")<text.index("'fixed phase failed: normal-refusal-and-controls'")
    assert text.index("self.checkpoint('maintenance-results-collected'")<text.index('set(seen) == set(expected_crashes)')
    assert "set(names) == set(self.doc['expected_cases'])" in text
    assert "len(names) == len(set(names)) == len(self.doc['expected_cases'])" in text


def _graph_fixture(m, monkeypatch, tmp_path):
    # Kernel metadata/read/write regressions in the disposable worker only.
    # No transaction/fence is admitted by this helper.
    monkeypatch.setattr(m, 'mount_check', lambda path: None)
    class Parent:
        def __init__(self, path, **kwargs): self.fd=os.open(path,m.DIRECTORY)
        def check(self): pass
        def __enter__(self): return self
        def __exit__(self,*args): os.close(self.fd)
    monkeypatch.setattr(m,'Pinned',Parent)
    root=tmp_path/'source';root.mkdir(mode=0o700)
    (root/'nested').mkdir();(root/'nested/data').write_bytes(b'original')
    os.link(root/'nested/data',root/'alias')
    (root/'link').symlink_to('nested/data')
    return root


@pytest.mark.parametrize('change',('substitution','edit','added','removed','mtime-restored'))
def test_recovery_graph_refuses_drift_before_any_timestamp_write(monkeypatch,tmp_path,change):
    m=load();root=_graph_fixture(m,monkeypatch,tmp_path);proof=m.graph(root)
    outside=tmp_path/'outside';outside.mkdir();(outside/'data').write_bytes(b'sentinel')
    if change=='substitution':
        (root/'nested').rename(root/'old');(root/'nested').symlink_to(outside)
    elif change=='edit':(root/'nested/data').write_bytes(b'legitimate change')
    elif change=='added':(root/'new').write_bytes(b'new')
    elif change=='removed':(root/'link').unlink()
    else:
        old=(root/'nested/data').stat();(root/'nested/data').write_bytes(b'changed!')
        os.utime(root/'nested/data',ns=(old.st_atime_ns,old.st_mtime_ns))
    sentinel=(outside/'data').stat();changed=(root/'alias').stat()
    writes=[];monkeypatch.setattr(m.os,'utime',lambda *a,**k:writes.append((a,k)))
    with pytest.raises(m.Refusal):m.restore_times(root,proof)
    assert writes==[] and (outside/'data').stat()==sentinel and (root/'alias').stat()==changed


def test_recovery_graph_allows_only_atime_and_restores_hardlink_once(monkeypatch,tmp_path):
    m=load();root=_graph_fixture(m,monkeypatch,tmp_path);proof=m.graph(root)
    data=root/'nested/data';old=data.stat();os.utime(data,ns=(old.st_atime_ns+500,old.st_mtime_ns))
    # utime itself changes ctime, unlike cp reads. Capture this clean baseline
    # and alter only its atime field to model a read's permitted metadata delta.
    proof=m.graph(root)
    for row in proof['rows']:
        if row['relative'] in (['alias'],['nested','data']):row['atime']=old.st_atime_ns
    calls=[];real=os.utime
    def record(*a,**k):calls.append(k);return real(*a,**k)
    monkeypatch.setattr(m.os,'utime',record)
    m.restore_times(root,proof)
    assert data.stat().st_mtime_ns==old.st_mtime_ns and data.stat().st_atime_ns==old.st_atime_ns
    assert len(calls)==1 and calls[0]['follow_symlinks'] is False and 'dir_fd' in calls[0]


def test_target_recovery_uses_distinct_identity_graph(monkeypatch,tmp_path):
    import shutil
    m=load();root=_graph_fixture(m,monkeypatch,tmp_path);source=m.graph(root)
    target=tmp_path/'copy';shutil.copytree(root,target,symlinks=True)
    # copytree does not preserve hardlinks; recreate the expected graph.
    (target/'alias').unlink();os.link(target/'nested/data',target/'alias')
    original=root.stat();os.utime(target,ns=(original.st_atime_ns,original.st_mtime_ns))
    target_proof=m.graph(target)
    assert target_proof['rows'][-1]['proof'][:2]!=source['rows'][-1]['proof'][:2]
    m.restore_times(target,target_proof,times=source)
    assert (target/'alias').stat().st_ino==(target/'nested/data').stat().st_ino


@pytest.mark.parametrize('record',([],{'schema':1,'rows':[]},{'schema':2,'rows':[]},
    {'schema':2,'rows':[{'relative':['..'],'proof':[0,0,0,0,0,0,0,0,0],'atime':0}]}))
def test_ambiguous_or_malformed_recovery_record_never_opens_payload(monkeypatch,record):
    m=load();calls=[];monkeypatch.setattr(m,'graph',lambda p:calls.append(p))
    with pytest.raises(m.Refusal):m.restore_times(Path('/synthetic-only'),record)
    assert calls==[]


@pytest.mark.parametrize('mode',(0o755,0o750,0o710,0o701))
def test_unsupported_leaf_privacy_refuses_without_mutation(monkeypatch,tmp_path,mode):
    m=load();root=_graph_fixture(m,monkeypatch,tmp_path);root.chmod(mode);before=root.stat()
    with pytest.raises(m.Refusal,match='privacy layout'):m.private_leaf(root)
    assert root.stat()==before


def test_private_leaf_positive_and_copy_parent_default_acl_negative(monkeypatch,tmp_path):
    m=load();root=_graph_fixture(m,monkeypatch,tmp_path);m.private_leaf(root)
    monkeypatch.setattr(m.os,'listxattr',lambda *a,**k:['system.posix_acl_default'])
    with pytest.raises(m.Refusal,match='default ACL'):m.copy_parent(tmp_path)
    with pytest.raises(m.Refusal,match='privacy layout'):m.private_leaf(root)


def test_rollback_unavailability_precedes_direction_and_logical_mutation():
    tree=ast.parse(PRODUCT.read_text())
    txn=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Transaction')
    methods={n.name:ast.unparse(n) for n in txn.body if isinstance(n,ast.FunctionDef)}
    begin=methods['begin_rollback'];run=methods['run']
    assert begin.index('rollback_guard()')<begin.index('atomic(maps,')<begin.index("self.doc['direction'] = 'rollback'")
    assert run.index("if action == 'rollback':\n        rollback_guard()")<run.index("mkdir(lib,")
    assert run.index('self.begin_rollback(checkpoint)')<run.index('restore_times(original, rows)')
    assert "mapping.get('state') != 'ready'" in run
    assert "self.doc.get('direction') == 'rollback'" in run
    checker=ast.unparse(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='require_mapping'))
    assert "'state': 'ready'" in checker and 'os.readlink' in checker and '.resolve(' not in checker


@pytest.mark.parametrize('binding', ('wrong', 'malformed'))
def test_storage_stage_manifest_binding_precedes_json_and_external_probes(tmp_path, binding):
    spec=importlib.util.spec_from_file_location('storage_binding_under_test',REPO/'qa/privsep-storage/run.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    (tmp_path/'manifest.json').write_text('not JSON; must never be parsed')
    expected='0'*64 if binding=='wrong' else 'not-a-sha256'
    with pytest.raises(AssertionError,match='manifest hash mismatch' if binding=='wrong' else 'invalid manifest hash'):
        module.preflight(tmp_path,expected)


def test_storage_cold_init_oracle_requires_v2_same_fd_kernel_identity():
    tree=ast.parse((REPO/'qa/privsep-storage/guest.py').read_text())
    init=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='init')
    sole=next(n for n in init.body if isinstance(n,ast.FunctionDef) and n.name=='sole_init')
    text=ast.unparse(sole)
    for required in ('m.require_fence()', 'os.fstat(fd)', 'libc.fstatfs(fd,', 'fcntl.ioctl(fd,',
                     "'sole-host-init-v2'", "'device': st.st_dev"):
        assert required in text
    assert any(isinstance(n,ast.Try) and [ast.unparse(v) for v in n.finalbody]==['os.close(fd)']
               for n in ast.walk(sole))
    assert any(isinstance(n,ast.Assert) and isinstance(n.test,ast.Compare)
               and isinstance(n.test.left,ast.Name) and n.test.left.id=='value' for n in ast.walk(sole))
