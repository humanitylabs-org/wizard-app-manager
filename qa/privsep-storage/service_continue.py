#!/usr/bin/python3
"""Parent-admitted Service-only continuation of the committed 851 synthetic VM.

Static preflight only hashes/parses metadata and source. Execution creates two
thin overlays, ONE ordinary boot, new identity regressions and the two missing
Service cases. No Transaction, cold boot, request reader or old Python rerun.
"""
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

PRIOR_REV = '851c2e6c6ec7c905d1a95ac1aad40f9ad41070d7'
PRIOR = Path('/srv/work/privsep/storage-zombie-reap')/PRIOR_REV
PRIOR_RECEIPT = 'b17229bac856b319d6fa36488f017d71e919cac09373a16743b235ca36668067'
PRIOR_MANIFEST = '36aee5669e95cadc17e2c8c8323e938864ceaf89ffe168ffbb053a02923ffb58'
RESERVE = 217179869184
REMAINING = 66220945408  # original admitted peak less ALREADY allocated 2498531328
ROOT = '/root/storage-service-continuation'
NEW_FILES = ('qa/privsep-storage/service_continue.py','qa/privsep-storage/service_identity.py',
             'qa/privsep-storage/service_state.py','tests/test_storage_service_identity.py')
REMAINING_CASES = ('installed-compiled-backend-physical-service-consumer',
                   'stale-and-unavailable-mapping-before-update-restart')


def digest(p):
    with Path(p).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def load(name,p):
    spec = importlib.util.spec_from_file_location(name,p)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def process_absence(paths):
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal(): continue
        try: args = (p/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError,PermissionError,ProcessLookupError): continue
        if args and Path(os.fsdecode(args[0])).name.startswith('qemu-system-'):
            assert not any(str(path).encode() in arg for path in paths for arg in args), 'retained VM in use'


def disk_binding(p):
    s = p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1
    assert not s.st_mode & 0o022
    return {'sha256':digest(p),'stat':{k:getattr(s,k) for k in
        ('st_dev','st_ino','st_size','st_blocks','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_gid','st_nlink')}}


def service_tail(source, backend):
    """Compile the accepted actual Service tail, NOT its committed storage prefix.

    All shipped install/helper/bind/HTTP/stale assertions remain byte-AST exact.
    The portable backend config digest is supplied as its freshly content-attested
    engine handle; no arbitrary tag/identity waiver is introduced.
    """
    tree = ast.parse(source)
    cls = next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='ProductionRunner')
    fn = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='continue_production')
    starts = [i for i,n in enumerate(fn.body) if isinstance(n,ast.Expr) and
        isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=='self.guest' and
        n.value.args and isinstance(n.value.args[0],ast.Constant) and n.value.args[0].value=='package-exact-service']
    ends = [i for i,n in enumerate(fn.body) if isinstance(n,ast.Assign) and
            any(isinstance(t,ast.Name) and t.id=='rows' for t in n.targets)]
    assert len(starts)==len(ends)==1 and starts[0]<ends[0]
    body = copy.deepcopy(fn.body[starts[0]:ends[0]])
    assert [n.value.args[0].value for n in body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
        and ast.unparse(n.value.func)=='self.mark'] == list(REMAINING_CASES)
    function = ast.FunctionDef(name='accepted_service_tail', args=copy.deepcopy(fn.args),
        body=body, decorator_list=[], returns=None, type_comment=None)
    module = ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
    namespace = {'BACKEND':backend,'GUEST':'/usr/local/lib/wizard-storage/production-guest.py',
                 're':__import__('re'),'digest':digest}
    exec(compile(module,'accepted-service-tail','exec'),namespace)
    return namespace['accepted_service_tail']


def inherited(stage, snapshot, production):
    assert digest(PRIOR/'manifest.json') == PRIOR_MANIFEST
    assert digest(PRIOR/'evidence/receipt.json') == PRIOR_RECEIPT
    old = json.loads((PRIOR/'evidence/receipt.json').read_text())
    assert old == snapshot['receipt']
    assert old['cleanup_errors']==old['diagnostic_errors']==[]
    assert old['all_owned_vms_stopped'] and old['all_boot_pids_absent']
    assert len(old['boots']) == 25 and all(b['stopped'] and not Path('/proc',str(b['pid'])).exists() for b in old['boots'])
    assert old['guest_container_cleanup']=={'readback_absent':True,'removed_exact_ids':[]}
    assert old['original_disk_sha256_before']==old['original_disk_sha256_after']
    assert [r['name'] for r in old['actual_cases']] == list(production.CASES[:-2])
    assert len(old['actual_cases'])==12 and all(r['passed'] for r in old['actual_cases'])
    for name,row in snapshot['files'].items():
        p=PRIOR/name
        assert digest(p)==row['sha256'] and stat.S_IMODE(p.lstat().st_mode)==row['mode']
    assert len(old['steps']) == 73
    assert [s['name'] for s in old['steps'] if s['exit']!=0] == ['load-backend']
    for s in old['steps']: assert digest(PRIOR/'evidence'/s['log'])==s['sha256']
    xml=ET.parse(PRIOR/'evidence/pytest.xml'); cases=xml.findall('.//testcase')
    ids=[c.get('classname','').replace('.','/')+'.py::'+c.get('name','') for c in cases]
    assert len(ids)==len(set(ids))==278
    assert sorted(ids)==snapshot['manifest']['expected_pytest_cases']
    assert not any(xml.findall('.//'+tag) for tag in ('failure','error','skipped'))
    assert digest(PRIOR/'evidence/pytest.xml')=='0666847d62353ceeaf0554dbcaf0e61f9699cf8b02477f639bccdd723ab3bbbf'
    causal=load('inherited_causal_parser',PRIOR/'zombie-runner.py')
    assert old['causal_proof_complete'] and [r['name'] for r in old['causal_cases']]==list(causal.CAUSAL)
    causal.parse_fixture(json.dumps(json.loads((PRIOR/'evidence/nested.json').read_text())).encode(),'nested')
    for mode,index in (('exited',2),('live',4)):
        p=PRIOR/'evidence'/f'boot-{index}-serial.log'
        row=next(r for r in old['causal_cases'] if r['name']==mode)
        assert digest(p)==row['serial_sha256'] and causal.parse_fixture(p.read_bytes(),mode)==row['result']
    return {'python':{'count':278,'fresh':False,'xml_sha256':digest(PRIOR/'evidence/pytest.xml')},
            'causal':{'count':3,'fresh':False,'cases':old['causal_cases']},
            'production':{'count':12,'fresh':False,'cases':old['actual_cases']},
            'receipt_sha256':PRIOR_RECEIPT,'source_revision':PRIOR_REV}


def validate(stage, expected):
    assert digest(stage/'manifest.json')==expected
    d=json.loads((stage/'manifest.json').read_text())
    assert stage==Path('/srv/work/privsep/storage-service-continuation')/d['harness_revision'] and Path.cwd()==stage
    assert d['schema']=='storage-service-continuation-v1' and d['review_required'] is True
    assert d['revision']==PRIOR_REV and d['reserve_bytes']==RESERVE and d['remaining_peak_bytes']==REMAINING
    assert d['expected_fresh_runtime_cases']==list(REMAINING_CASES)
    assert set(d['files']) == {'continuation.py','identity.py','state.py','test_storage_service_identity.py',
        'source.tar','source.bundle','source-members.json','inherited.json','backend.json','binding.json',
        'runner.py','storage-runner.py'}
    assert not (stage/'evidence').exists(), 'one-shot continuation already entered'
    for p in [stage,stage/'manifest.json',*(stage/n for n in d['files'])]:
        st=p.lstat(); assert not p.is_symlink() and st.st_uid==os.getuid() and not st.st_mode & 0o022
    for n,h in d['files'].items(): assert digest(stage/n)==h
    assert digest(__file__)==d['files']['continuation.py']
    snapshot=json.loads((stage/'inherited.json').read_text())
    process_absence([PRIOR,stage])
    for n,row in snapshot['disks'].items(): assert disk_binding(PRIOR/'evidence'/n)==row
    assert digest(PRIOR/'manifest.json') == PRIOR_MANIFEST
    assert digest(PRIOR/'evidence/receipt.json') == PRIOR_RECEIPT
    assert snapshot['manifest']['files']['runner.py'] == digest(PRIOR/'runner.py')
    for n,h in snapshot['manifest']['files'].items(): assert digest(PRIOR/n)==h
    production=load('inherited_production',PRIOR/'runner.py')
    production.validate_runner_contract(stage,d)
    assert digest(stage/'runner.py') == digest(PRIOR/'runner.py')
    assert digest(stage/'storage-runner.py') == digest(PRIOR/'storage-runner.py')
    base=load('inherited_storage',PRIOR/'storage-runner.py')
    closure=inherited(stage,snapshot,production)
    before=json.loads((PRIOR/'source-members.json').read_text())
    after=base.inventory(stage/'source.tar')
    assert after==json.loads((stage/'source-members.json').read_text())
    a,b=({r['path']:r for r in rows} for rows in (before,after))
    assert set(b)-set(a)==set(NEW_FILES) and not set(a)-set(b)
    assert all(a[n]==b[n] for n in a), 'inherited source closure changed'
    for n in ('service.tar','service-members.json','backend-inputs.json'):
        assert digest(PRIOR/n)==snapshot['manifest']['files'][n]
    inputs=json.loads((PRIOR/'backend-inputs.json').read_text())
    assert len(inputs)==622 and all(b[r['path']]==r for r in inputs)
    identity=load('static_image_identity',stage/'identity.py')
    with production.BACKEND_ARCHIVE.open('rb') as f: actual=identity.archive_identity(f)
    assert digest(production.BACKEND_ARCHIVE)==production.BACKEND_SHA==snapshot['backend']['archive_sha256']
    backend=json.loads((stage/'backend.json').read_text()); assert backend==snapshot['backend']
    identity.bind_archive(actual,backend,tag=backend['manifest'][0]['RepoTags'][0])
    accepted=Path('/srv/work/privsep/integrated/20261029T161239Z-595523fb-context-proof')
    for n,h in backend['proof_files'].items(): assert digest(accepted/n)==h
    assert backend['accepted_inspect']['Id']==production.BACKEND=='sha256:'+actual['config_sha256']
    # New harness files are exact pushed source bytes; all inherited tests/product are unchanged.
    import tarfile
    with tarfile.open(stage/'source.tar') as archive:
        for name,target in zip(NEW_FILES,('continuation.py','identity.py','state.py','test_storage_service_identity.py')):
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest()==d['files'][target]
        source_stream = archive.extractfile(NEW_FILES[-1]); assert source_stream is not None
        sources={'tests/test_storage_service_identity.py':source_stream.read().decode(),
                 'wam/privilege_migration.py':archive.extractfile('wam/privilege_migration.py').read().decode()}
    base.PYTEST_MODULES = ('tests/test_storage_service_identity.py',)
    assert base.pytest_inventory(sources)==d['expected_new_pytest_cases']
    heads=subprocess.check_output(['git','bundle','list-heads',str(stage/'source.bundle')],text=True,timeout=30)
    assert heads.splitlines()==[d['harness_revision']+' refs/heads/'+d['branch']]
    # Honest budget: retained guest allocation is already excluded from free.
    # Remaining original peak includes new overlay COW, Service build/cache and
    # guest+host Service exports simultaneously; no new 56GiB base/copy allocated.
    free=shutil.disk_usage(stage).free
    return d,snapshot,production,base,closure,{'static_only':True,'heavy_executed':False,
        'free_bytes':free,'reserve_bytes':RESERVE,'remaining_peak_bytes':REMAINING,
        'required_bytes':RESERVE+REMAINING,'capacity_admissible':free>=RESERVE+REMAINING,
        'new_python_cases':len(d['expected_new_pytest_cases']),'inherited_python_cases':278,
        'inherited_causal_cases':3,'inherited_runtime_cases':12,'fresh_runtime_cases':2,
        'aggregate_runtime_cases':14,'committed_storage_replay':False,'backend_config_digest':production.BACKEND,
        'all_previous_source_bytes_modes_unchanged':True,'retained_guest_disks_pinned':True}


def make_runner(production,base):
    class ServiceRunner(production.make_runner(base)):
        def capacity(self):
            allocated=sum(p.stat().st_blocks*512 for p in self.evidence.rglob('*') if p.is_file() and not p.is_symlink())
            pending=max(0,REMAINING-allocated);free=shutil.disk_usage(self.stage).free
            self.receipt['last_capacity']={'free_bytes':free,'reserve_bytes':RESERVE,
                'new_allocated_bytes':allocated,'remaining_pending_bytes':pending}
            self.save();assert free>=RESERVE+pending,'reserve plus remaining ORIGINAL peak unavailable'

        def execute_service(self,snapshot,closure):
            self.capacity();self.phase('service-only-thin-overlay')
            self.receipt.update(harness_revision=self.doc['harness_revision'],inherited=closure,
                production_source_revision=PRIOR_REV,source_archive_revision=self.doc['harness_revision'],
                source_binding='pushed harness archive adds QA only; complete inherited product byte/mode closure exact',
                submitted_continuation_sha256=digest(self.stage/'continuation.py'),
                scope='two missing Service cases only; 278 Python + 3 causal + 12 production inherited',
                committed_storage_replay=False,expected_fresh_runtime_cases=list(REMAINING_CASES))
            self.save()
            for n in snapshot['disks']: assert disk_binding(PRIOR/'evidence'/n)==snapshot['disks'][n]
            for n,dest in (('id_ed25519',self.key),('seed.iso',self.evidence/'seed.iso')):
                shutil.copyfile(PRIOR/'evidence'/n,dest);dest.chmod(0o600)
            for n,dest in (('disk.qcow2',self.disk),('secondary.qcow2',self.secondary)):
                self.run('thin-overlay-'+n,['qemu-img','create','-f','qcow2','-F','qcow2','-b',PRIOR/'evidence'/n,dest])
            self.start_normal()  # ONE ordinary systemd boot; no maintenance calls.
            self.guest('private-qa-directory','umask 077\nmkdir -m700 '+ROOT)
            for n,target in (('state.py','state.py'),('binding.json','binding.json'),('identity.py','identity.py'),
                             ('backend.json','backend.json')): self.copy(self.stage/n,ROOT+'/'+target)
            self.guest('committed-state-before','/usr/bin/python3 -I -B '+ROOT+'/state.py before')
            self.copy(self.evidence/'committed-before.json',ROOT+'/committed-before.json',True)
            self.receipt['committed_before_sha256']=digest(self.evidence/'committed-before.json');self.save()
            self.copy(self.stage/'source.tar',ROOT+'/source.tar')
            self.guest('new-harness-source','mkdir '+ROOT+'/source\ntar --no-same-permissions -xf '+ROOT+'/source.tar -C '+ROOT+'/source\n'
                'test "$(git -C /root/storage-proof/source rev-parse HEAD)" = '+PRIOR_REV+'\n'
                'test -z "$(git -C /root/storage-proof/source status --porcelain)"\n')
            rc=self.guest('new-identity-regressions','cd '+ROOT+'/source\nPYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m pytest -q '
                'tests/test_storage_service_identity.py --junitxml='+ROOT+'/new.xml',180,False)
            self.copy(self.evidence/'new.xml',ROOT+'/new.xml',True)
            tree=ET.parse(self.evidence/'new.xml');cases=tree.findall('.//testcase')
            ids=[c.get('classname','').replace('.','/')+'.py::'+c.get('name','') for c in cases]
            assert rc==0 and len(ids)==len(set(ids)) and sorted(ids)==self.doc['expected_new_pytest_cases']
            assert not any(tree.findall('.//'+tag) for tag in ('failure','error','skipped'))
            self.receipt['new_python']={'cases':sorted(ids),'xml_sha256':digest(self.evidence/'new.xml'),'fresh':True};self.save()
            self.phase('real-source-bound-service-and-installed-backend')
            self.guest('attest-loaded-backend','/usr/bin/python3 -I -B '+ROOT+'/identity.py '+ROOT+'/backend.json '+ROOT+'/backend-attestation.json',240)
            self.copy(self.evidence/'backend-attestation.json',ROOT+'/backend-attestation.json',True)
            attested=json.loads((self.evidence/'backend-attestation.json').read_text())
            assert attested['config_digest']==production.BACKEND
            self.receipt['backend_identity']=attested;self.save()
            service_tail((PRIOR/'runner.py').read_text(),attested['engine_image_id'])(self)
            assert [r['name'] for r in self.receipt['actual_cases']]==list(REMAINING_CASES)
            assert all(r['passed'] for r in self.receipt['actual_cases'])
            self.guest('committed-state-after','/usr/bin/python3 -I -B '+ROOT+'/state.py after')
            self.copy(self.evidence/'committed-after.json',ROOT+'/committed-after.json',True)
            assert (self.evidence/'committed-before.json').read_bytes()==(self.evidence/'committed-after.json').read_bytes()
            self.receipt['committed_after_sha256']=digest(self.evidence/'committed-after.json')
            aggregate=[dict(r,fresh=False,inherited_receipt_sha256=PRIOR_RECEIPT)
                for r in closure['production']['cases']]+[dict(r,fresh=True) for r in self.receipt['actual_cases']]
            assert len(aggregate)==len({r['name'] for r in aggregate})==14
            assert sorted(r['name'] for r in aggregate)==sorted(production.CASES) and all(r['passed'] for r in aggregate)
            self.receipt['aggregate_runtime_cases']=aggregate
            self.cleanup_guest();self.shutdown();self.receipt['passed']=True;self.save()
    return ServiceRunner


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',required=True,type=Path);p.add_argument('--manifest-sha256',required=True)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--preflight',action='store_true');g.add_argument('--execute',action='store_true')
    args=p.parse_args();d,snapshot,production,base,closure,report=validate(args.stage,args.manifest_sha256)
    if args.preflight:print(json.dumps(report,sort_keys=True));return 0
    base.native_lock();assert report['capacity_admissible'],'capacity-refused before allocation'
    os.umask(0o077);tmp=args.stage/'tmp';tmp.mkdir(mode=0o700);os.environ['TMPDIR']=str(tmp)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    r=make_runner(production,base)(args.stage,d);r.receipt['admission']=report;r.save()
    def interrupt(sig,frame):raise TimeoutError('bounded Service continuation deadline')
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):signal.signal(sig,interrupt)
    signal.alarm(13800)
    try:r.execute_service(snapshot,closure)
    except BaseException as exc:
        r.receipt['first_error']={'type':type(exc).__name__,'frames':[
            {'file':Path(f.filename).name,'function':f.name,'line':f.lineno}
            for f in __import__('traceback').extract_tb(exc.__traceback__)]}
        r.receipt['passed']=False;r.save()
    finally:
        try:
            if r.proc is not None and r.proc.poll() is None:r.diagnostics();r.cleanup_guest()
        except BaseException:r.receipt['cleanup_errors'].append('bounded-guest-diagnostics-cleanup')
        try:r.stop()
        except BaseException:r.receipt['cleanup_errors'].append('stop-guest')
        try:
            process_absence([PRIOR,args.stage])
            assert all(b['stopped'] and not Path('/proc',str(b['pid'])).exists() for b in r.receipt['boots'])
            for n,row in snapshot['disks'].items():assert disk_binding(PRIOR/'evidence'/n)==row
            inherited(args.stage,snapshot,production)
            r.receipt['original_committed_disks_preserved']=True
            r.receipt['all_owned_vms_stopped']=True;r.receipt['all_boot_pids_absent']=True
        except BaseException:r.receipt['cleanup_errors'].append('retained-evidence-preservation')
        r.receipt['passed']=bool(r.receipt['passed'] and r.receipt.get('original_committed_disks_preserved')
            and not r.receipt['cleanup_errors'] and not r.receipt['diagnostic_errors'])
        r.write_junit();r.save();signal.alarm(0)
    print(json.dumps({'passed':r.receipt['passed'],'receipt':str(r.evidence/'receipt.json'),'full_integration_acceptance':False},sort_keys=True))
    return 0 if r.receipt['passed'] else 1


if __name__=='__main__':sys.exit(main())
