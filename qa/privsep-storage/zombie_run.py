#!/usr/bin/python3
"""Parent-only forum admission: causal PID1 reaping then full production slice.

REUSE immutable synthetic a419 disks and exact production lifecycle/Service
consumer. No original disk writes, host Docker, real agent data or release.
--preflight is metadata/source/hash inspection only, never test collection.
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
import tarfile
import time
import xml.etree.ElementTree as ET

BASELINE = Path('/srv/work/privsep/storage-cold-intermittent/f661c358e9276f5d715bd54ea7deee1a7ab1b5db')
BASELINE_MANIFEST = '0a201b56a103fd0d2ee7ca5ee9be1d3cf5de42ccc1a0104b5a40c003d949775c'
BASELINE_RECEIPT = '8670bee5f70bb653d019ae200bfc29e8034427a59849a58ba5bae148a546f7d1'
BASELINE_XML = '024be2aa9a9e1c78239d5b9bafdb0273a59c9315c66b05a14ed60377cddc0b21'
ORIGIN = Path('/srv/work/privsep/storage-production/a4197c8ac84d5654f17e9f1cb4cb8d44008898e5')
ROOT = '/root/storage-zombie-qa'
INIT = ROOT+'/init.py'
RESERVE = 217179869184
PENDING = 68719476736
CAUSAL = ('actual-nested-pid1-before-reaper', 'exited', 'live')
ADDED = ('qa/privsep-storage/zombie_run.py','qa/privsep-storage/zombie_init.py',
         'tests/test_storage_child_reap.py','tests/test_storage_zombie_runner.py')
CHANGED = ('scripts/protected-agent-storage.py','scripts/storage-maintenance-init.py',
           'qa/privsep-storage/production_run.py','tests/test_storage_production_integration.py')


class Interrupted(BaseException): pass


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    assert spec is not None and spec.loader is not None
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def parse_fixture(raw,mode):
    assert len(raw)<=1048576 and b'QA_PRIVATE_CANARY' not in raw
    rows=[]
    for line in raw.splitlines():
        try: row=json.loads(line)
        except (ValueError,UnicodeError): continue
        if type(row) is dict and row.get('fixture')=='storage-child-reap-v1': rows.append(row)
    assert len(rows)<=1, 'duplicate causal fixture output'
    if not rows:return None
    row=rows[0]
    assert row.get('mode')==mode and row.get('production_acceptance') is False
    assert row.get('passed') is True, 'causal kernel fixture failed'
    controls={
        'exited':('actual_zombie_barrier','old_guard_refused','repaired_guard_admitted','zombie_absent',
                  'repeated_fence_admitted','fixed_source_metadata_unchanged'),
        'live':('live_child_refused','same_live_child_after_guard','writer_progress_after_refusal','no_guard_wait_or_kill','fixed_source_metadata_unchanged'),
        'nested':('actual_nested_pid1_refused_before_reaper','zombie_still_unreaped')}[mode]
    assert set(row.get('controls',{}))==set(controls) and all(row['controls'][k] is True for k in controls)
    return row


def fence_delta(before,after):
    """The real-kernel no-reaper mutant is the exact original fence AST."""
    a,b=ast.parse(before),ast.parse(after)
    defs=lambda tree:{n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    x,y=defs(a),defs(b)
    assert set(y)==set(x)|{'wait_exited_child','reap_exited_children'}
    changed={k for k in x if ast.dump(x[k])!=ast.dump(y[k])}
    assert changed=={'require_fence'}
    f=copy.deepcopy(y['require_fence'])
    calls=[n for n in f.body if isinstance(n,ast.Expr) and ast.unparse(n)=='reap_exited_children()']
    assert len(calls)==1;f.body.remove(calls[0])
    assert ast.dump(f)==ast.dump(x['require_fence'])
    # No hidden changes to module-level constants/imports besides the bound.
    nondefs=lambda tree:[ast.dump(n) for n in tree.body if not isinstance(n,(ast.FunctionDef,ast.ClassDef))
        and not (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CHILD_REAP_CALL_LIMIT' for t in n.targets))]
    assert nondefs(a)==nondefs(b)
    return True


def validate_bundle_head(doc,heads):
    branch=doc['branch']
    assert isinstance(branch,str) and branch and not branch.startswith('-')
    assert subprocess.run(['git','check-ref-format','refs/heads/'+branch],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30).returncode==0
    assert heads.splitlines().count(doc['revision']+' refs/heads/'+branch)==1, 'exact bundle branch/head missing'


def bundle_clone_command(doc,root=ROOT):
    # A named-branch bundle need not advertise HEAD; never use its default.
    return shlex.join(['git','clone','--template='+root+'/template',
                       '--branch',doc['branch'],'--',root+'/source.bundle',root+'/genuine'])


def validate(stage,expected):
    assert stage==Path('/srv/work/privsep/storage-zombie-reap')/stage.name and Path.cwd()==stage
    assert digest(stage/'manifest.json')==expected
    d=json.loads((stage/'manifest.json').read_text())
    assert d['schema']=='storage-zombie-reap-v1' and d['revision']==stage.name
    assert d['full_acceptance'] is False and d['review_required'] is True
    assert d['reserve_bytes']==RESERVE and d['pending_bytes']==PENDING
    assert d['expected_causal_cases']==list(CAUSAL)
    files={'runner.py','guest.py','storage-runner.py','zombie-runner.py','zombie-init.py',
           'source.tar','source.bundle','source-members.json','shallow','service.tar','service-members.json','backend-inputs.json'}
    assert set(d['files'])==files
    for p in [stage,stage/'manifest.json',*(stage/n for n in files)]:
        st=p.lstat();assert not stat.S_ISLNK(st.st_mode) and st.st_uid==os.getuid() and not st.st_mode & 0o022
    for n,h in d['files'].items():assert digest(stage/n)==h
    assert digest(__file__)==d['files']['zombie-runner.py']
    assert digest(BASELINE/'manifest.json')==BASELINE_MANIFEST
    assert digest(BASELINE/'evidence/receipt.json')==BASELINE_RECEIPT
    assert digest(BASELINE/'evidence/intermittent.xml')==BASELINE_XML
    previous=load('immutable_intermittent_validation',BASELINE/'runner.py')
    os.chdir(BASELINE)
    try: prior,diagnostic,base,report=previous.validate(BASELINE,BASELINE_MANIFEST)
    finally:os.chdir(stage)
    base.RESERVE=RESERVE;base.PENDING=PENDING
    r=json.loads((BASELINE/'evidence/receipt.json').read_text())
    assert r['cleanup_errors']==r['diagnostic_errors']==[] and r['campaign_complete']
    assert len(r['boots'])==4 and all(b['stopped'] and not Path('/proc',str(b['pid'])).exists() for b in r['boots'])
    assert len(r['steps'])==16 and all(s['exit']==0 and digest(BASELINE/'evidence'/s['log'])==s['sha256'] for s in r['steps'])
    assert previous.junit(BASELINE/'evidence/intermittent.xml',prior['new_test_cases'])['valid']
    assert r['original_disk_sha256_before']==r['original_disk_sha256_after']
    assert r['prepared_sha256_before']==r['prepared_sha256_after']
    assert r['transaction_executed'] is False and r['request_reader_executed'] is False
    assert len(r['observations'])==3
    assert [v['observation']['state'] for v in r['observations']]==['pre-request-stop','bootstrap-refused','bootstrap-refused']
    rows=base.inventory(stage/'source.tar');assert rows==json.loads((stage/'source-members.json').read_text())
    assert len(rows)==d['source_members'] and len(rows)==len({v['path'] for v in rows})
    a,b=({v['path']:v for v in inventory} for inventory in (json.loads((BASELINE/'source-members.json').read_text()),rows))
    assert set(b)-set(a)==set(ADDED) and not set(a)-set(b)
    assert {k for k in a if a[k]!=b[k]}==set(CHANGED)
    assert b['scripts/protected-agent-storage.py']['sha256']==d['product_sha256']
    assert b['scripts/storage-maintenance-init.py']['sha256']==d['bootstrap_sha256']
    production=load('current_production_continuation',stage/'runner.py')
    contract=production.validate_runner_contract(stage,d)
    assert d['expected_cases']==list(production.CASES)
    base.PYTEST_MODULES=production.MODULES+('tests/test_storage_child_reap.py','tests/test_storage_zombie_runner.py')
    with tarfile.open(stage/'source.tar') as arc, tarfile.open(BASELINE/'source.tar') as old:
        assert fence_delta(old.extractfile(CHANGED[0]).read(),arc.extractfile(CHANGED[0]).read())
        for member,standalone in (('qa/privsep-storage/production_run.py','runner.py'),
                ('qa/privsep-storage/production_guest.py','guest.py'),('qa/privsep-storage/run.py','storage-runner.py'),
                (ADDED[0],'zombie-runner.py'),(ADDED[1],'zombie-init.py')):
            assert hashlib.sha256(arc.extractfile(member).read()).hexdigest()==d['files'][standalone]
        sources={n:arc.extractfile(n).read().decode() for n in (*base.PYTEST_MODULES,'wam/privilege_migration.py')}
    assert base.pytest_inventory(sources)==d['expected_pytest_cases']
    assert len(d['expected_pytest_cases'])==d['expected_pytest_count']
    assert base.inventory(stage/'service.tar')==json.loads((stage/'service-members.json').read_text())
    origin=json.loads((ORIGIN/'manifest.json').read_text())
    assert digest(stage/'service.tar')==origin['files']['service.tar']
    assert digest(stage/'service-members.json')==origin['files']['service-members.json']
    assert digest(stage/'backend-inputs.json')==origin['files']['backend-inputs.json']
    assert d['service_revision']==origin['service_revision']
    closure=json.loads((stage/'backend-inputs.json').read_text())
    assert len(closure)==622 and all(b[v['path']]==v for v in closure)
    assert digest(production.BACKEND_ARCHIVE)==production.BACKEND_SHA
    heads=subprocess.check_output(['git','bundle','list-heads',str(stage/'source.bundle')],text=True,timeout=30)
    validate_bundle_head(d,heads)
    diagnostic.process_absence([ORIGIN,BASELINE,stage])
    free=shutil.disk_usage(stage).free
    return d,diagnostic,base,production,{'static_only':True,'heavy_executed':False,'exact_source_members':len(rows),
        'exact_pytest_count':d['expected_pytest_count'],'causal_cases':list(CAUSAL),'production_cases':len(production.CASES),
        'effective_production_manifest_reads':contract,'all_original_fence_checks_retained':True,
        'backend_inputs_unchanged':622,'service_archive_unchanged':True,
        'old_234_cases_claimed_at_current_head':False,'required_bytes':RESERVE+PENDING,'free_bytes':free,
        'capacity_admissible':free>=RESERVE+PENDING,'shortfall_bytes':max(0,RESERVE+PENDING-free)}


def make_runner(production,base):
    class ReapRunner(production.make_runner(base)):
        def __init__(self,stage,doc):
            super().__init__(stage,doc)
            self.receipt.update(schema='storage-zombie-reap-v1',causal_cases=[],
                causal_proof_complete=False,full_integration_acceptance=False,
                scope='causal child-reap proof then all 14 original production startup/Service cases',
                submitted_runner_sha256=digest(stage/'zombie-runner.py'))
            self.save()

        def causal(self,mode):
            code='import json;from pathlib import Path;Path('+repr(ROOT+'/mode.json')+').write_text(json.dumps({"mode":'+repr(mode)+'}))'
            self.guest('causal-mode-'+mode,'/usr/bin/python3 -I -B -c '+shlex.quote(code))
            self.shutdown();self.boot_index+=1
            self.start(self.boot_index,True,self.boot,init=INIT,root_rw=True)
            end=time.monotonic()+120;row=None
            while time.monotonic()<end:
                assert self.proc.poll() is None, 'causal init exited'
                row=parse_fixture(self.serial.read_bytes() if self.serial.exists() else b'',mode)
                if row is not None:break
                time.sleep(0.25)
            assert row is not None,'causal kernel result timeout'
            self.receipt['causal_cases'].append({'name':mode,'result':row,'serial_sha256':digest(self.serial)})
            self.save();self.stop();self.start_normal()

        def execute(self):
            self.capacity();self.phase('fresh-synthetic-overlay-no-ambiguous-replay')
            self.original_hashes={n:digest(ORIGIN/'evidence'/n) for n in ('disk.qcow2','secondary.qcow2')}
            prior=json.loads((BASELINE/'evidence/receipt.json').read_text())
            assert self.original_hashes==prior['original_disk_sha256_after']
            self.receipt['original_disk_sha256_before']=self.original_hashes;self.save()
            for n,dest in (('id_ed25519',self.key),('seed.iso',self.evidence/'seed.iso'),
                           ('vmlinuz',self.evidence/'vmlinuz'),('initrd',self.evidence/'initrd')):
                shutil.copyfile(ORIGIN/'evidence'/n,dest);dest.chmod(0o600)
            for n,dest in (('disk.qcow2',self.disk),('secondary.qcow2',self.secondary)):
                self.run('new-overlay-'+n,['qemu-img','create','-f','qcow2','-F','qcow2','-b',ORIGIN/'evidence'/n,dest])
            self.boot=json.loads((ORIGIN/'evidence/boot.json').read_text())
            self.start_normal()
            self.guest('prepare-directory','umask 077\ninstall -d -m700 '+ROOT)
            for n in ('source.tar','source.bundle','shallow','guest.py','service.tar'):
                self.copy(self.stage/n,ROOT+'/'+n)
            self.copy(self.stage/'zombie-init.py',ROOT+'/init.py')
            self.copy(self.stage/'service-members.json','/root/storage-production/service-members.json')
            revision=self.receipt['revision']
            script='umask 022\nmkdir '+ROOT+'/source\ntar --no-same-permissions -xf '+ROOT+'/source.tar -C '+ROOT+'/source\n'
            # The source archive includes the unchanged historical metadata-only
            # observer; do not run its obsolete hash-pinned diagnostic builder.
            check="import importlib.util,json,hashlib;from pathlib import Path;s=importlib.util.spec_from_file_location('state','"+ROOT+"/source/qa/privsep-storage/cold_probe.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);v=m.metadata_snapshot();assert not v['durable_state_requires_review'];assert v['request_matches_fixed_synthetic'];assert v['logical']['exists'] and v['request']['mode']==384;assert hashlib.sha256(Path(m.PRODUCT).read_bytes()).hexdigest()==m.PRODUCT_SHA;assert hashlib.sha256(Path(m.BOOT).read_bytes()).hexdigest()==m.BOOT_SHA;Path('"+ROOT+"/before.json').write_text(json.dumps(v,sort_keys=True))"
            script+='/usr/bin/python3 -I -B -c '+shlex.quote(check)+'\n'
            script+='mv /root/storage-proof/source /root/storage-proof/source-a419-preserved\nmv '+ROOT+'/source /root/storage-proof/source\n'
            script+='mkdir -m700 '+ROOT+'/template\nif [ -s '+ROOT+'/shallow ]; then cp '+ROOT+'/shallow '+ROOT+'/template/shallow; fi\n'
            script+=bundle_clone_command(self.doc)+'\nmv '+ROOT+'/genuine/.git /root/storage-proof/source/.git\n'
            script+='cd /root/storage-proof/source\ntest "$(git rev-parse HEAD)" = '+revision+'\ngit fsck --full\ntest -z "$(git status --porcelain)"\n'
            script+='/usr/bin/python3 -I -B scripts/install-storage-maintenance.py\ninstall -m644 '+ROOT+'/guest.py '+production.GUEST+'\nchmod 755 '+INIT+'\n'
            script+='/usr/bin/python3 -I -B -c '+shlex.quote("import hashlib;from pathlib import Path;assert hashlib.sha256(Path('/usr/local/lib/wizard-storage/protected-agent-storage.py').read_bytes()).hexdigest()=="+repr(self.receipt['product_sha256'])+";assert Path('/usr/local/lib/wizard-storage/maintenance-init').read_bytes()==Path('scripts/storage-maintenance-init.py').read_bytes()")+'\n'
            script+='cp '+ROOT+'/service.tar /root/storage-proof/service.tar\n'
            self.guest('durable-state-exact-source-and-install',script,300)
            self.copy(self.evidence/'before.json',ROOT+'/before.json',True)
            self.receipt['durable_state_before']=json.loads((self.evidence/'before.json').read_text());self.save()
            self.focused_tests()  # changed storage/namespace/entry closure, not inherited 234
            self.guest('actual-nested-kernel-child',NESTED_SCRIPT,60)
            self.copy(self.evidence/'nested.json',ROOT+'/nested.json',True)
            nested=json.loads((self.evidence/'nested.json').read_text());parse_fixture(json.dumps(nested).encode(),'nested')
            self.receipt['causal_cases'].append({'name':CAUSAL[0],'result':nested,'sha256':digest(self.evidence/'nested.json')});self.save()
            self.causal('exited');self.causal('live')
            assert [r['name'] for r in self.receipt['causal_cases']]==list(CAUSAL)
            self.receipt['causal_proof_complete']=True;self.save()
            self.mark('ordinary-and-real-nested-entry-refused',lambda:(
                self.guest('ordinary-and-nested','/usr/bin/python3 -I '+production.GUEST+' online'),self.collect_json('online')))
            # Actual packaged entry/direct protected request, retirement, stock
            # gateway and real compiled backend + source-bound Service HTTP proof.
            self.continue_production()
    return ReapRunner


NESTED_SCRIPT = '''/usr/bin/python3 -I -B - <<'PY'
import json,os,signal,subprocess
from pathlib import Path
p=subprocess.Popen(['unshare','--mount','--pid','--fork','--mount-proc','/usr/bin/python3','-I','-B','/root/storage-zombie-qa/init.py','--nested'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
try:
    try:
        out,err=p.communicate(timeout=10)
        raise AssertionError('nested PID1 exited')
    except subprocess.TimeoutExpired:
        os.killpg(p.pid,signal.SIGKILL);out,err=p.communicate(timeout=10)
    rows=[]
    for line in out.splitlines():
        try: row=json.loads(line)
        except ValueError:continue
        if type(row) is dict and row.get('fixture')=='storage-child-reap-v1':rows.append(row)
    assert len(rows)==1 and rows[0]['passed'] is True
    Path('/root/storage-zombie-qa/nested.json').write_text(json.dumps(rows[0],sort_keys=True))
finally:
    if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=10)
PY
'''


def failure(exc):
    frames=[];tb=exc.__traceback__
    while tb:
        if Path(tb.tb_frame.f_code.co_filename).name in ('zombie-runner.py','runner.py','storage-runner.py'):
            frames.append({'file':Path(tb.tb_frame.f_code.co_filename).name,'function':tb.tb_frame.f_code.co_name,'line':tb.tb_lineno})
        tb=tb.tb_next
    return {'class':type(exc).__name__ if type(exc) in (AssertionError,TimeoutError,ValueError,KeyError,FileNotFoundError,Interrupted) else 'fixed-runner-error','frames':frames[-16:]}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',required=True,type=Path);p.add_argument('--manifest-sha256',required=True)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--preflight',action='store_true');g.add_argument('--execute',action='store_true')
    args=p.parse_args();d,diagnostic,base,production,report=validate(args.stage,args.manifest_sha256)
    if args.preflight: print(json.dumps(report,sort_keys=True));return 0
    base.native_lock()
    # Metadata-only refusal before constructor/disk allocation if capacity changed.
    assert report['capacity_admissible'], 'capacity-refused'
    os.umask(0o077);tmp=args.stage/'tmp';tmp.mkdir(mode=0o700);os.environ['TMPDIR']=str(tmp)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    r=make_runner(production,base)(args.stage,d);r.receipt['admission']=report;r.save()
    def interrupt(sig,frame):raise Interrupted()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):signal.signal(sig,interrupt)
    signal.alarm(17400)
    try:r.execute()
    except BaseException as exc:
        r.receipt['first_error']=failure(exc);r.receipt['passed']=False;r.save()
    finally:
        try:
            if r.proc is not None and r.proc.poll() is None and not r.receipt['boots'][-1]['maintenance']:r.cleanup_guest()
        except BaseException:r.receipt['cleanup_errors'].append('guest-container-cleanup')
        try:r.stop()
        except BaseException:r.receipt['cleanup_errors'].append('stop-guest')
        try:
            diagnostic.process_absence([ORIGIN,BASELINE,args.stage])
            assert all(b['stopped'] and not Path('/proc',str(b['pid'])).exists() for b in r.receipt['boots'])
            r.receipt['all_owned_vms_stopped']=True;r.receipt['all_boot_pids_absent']=True
            if hasattr(r,'original_hashes'):
                after={n:digest(ORIGIN/'evidence'/n) for n in r.original_hashes}
                assert after==r.original_hashes;r.receipt['original_disk_sha256_after']=after
                prior=json.loads((BASELINE/'manifest.json').read_text())
                for n,wanted in prior['origin_disk_stats'].items():
                    st=(ORIGIN/'evidence'/n).lstat();assert {k:getattr(st,k) for k in wanted}==wanted
                r.receipt['original_disks_preserved']=True
            validate(args.stage,args.manifest_sha256)
        except BaseException:r.receipt['cleanup_errors'].append('preservation-readback')
        try:r.diagnostics()
        except BaseException:r.receipt['diagnostic_errors'].append({'operation':'bounded-diagnostics'})
        r.receipt['passed']=bool(r.receipt['passed'] and r.receipt['causal_proof_complete'] and r.receipt.get('original_disks_preserved') and not r.receipt['cleanup_errors'] and not r.receipt['diagnostic_errors'])
        r.write_junit();r.save();signal.alarm(0)
    print(json.dumps({'passed':r.receipt['passed'],'receipt':str(r.evidence/'receipt.json'),'full_integration_acceptance':False},sort_keys=True))
    return 0 if r.receipt['passed'] else 1


if __name__=='__main__':sys.exit(main())
