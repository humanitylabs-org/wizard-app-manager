#!/usr/bin/python3
"""Parent-admitted contract A: focused root fixtures then two real Service cases.

--preflight is hash/AST/archive/metadata only. --execute requires native forum
worker lock and fresh 851 thin overlays. No backend/Service build, storage replay,
old failed guest boot, mutable-image proof, positive owner auth or release claim.
"""
import argparse
import ast
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import traceback
import xml.etree.ElementTree as ET

PRIOR=Path('/srv/work/privsep/storage-zombie-reap/851c2e6c6ec7c905d1a95ac1aad40f9ad41070d7')
OLD=Path('/srv/work/privsep/storage-service-continuation/474414caf8199c1912fd3343abcc796ec0d34973')
SIGNED=Path('/srv/work/privsep/custody-signedout/439e70d4b102e9fe971b8f0200717d99c1aabc83')
BASE_REV='439e70d4b102e9fe971b8f0200717d99c1aabc83'
RESERVE=217179869184
ORIGINAL_REMAINING=66220945408
OLD_ALLOCATED=2949689344
SIGNED_ALLOCATED=207671296
REMAINING=ORIGINAL_REMAINING-OLD_ALLOCATED-SIGNED_ALLOCATED
MAX_NEW=8589934592
FILE_CAP=6442450944
ROOT='/root/storage-contract-a'
STATE_ROOT='/root/storage-service-continuation'
TREE='/opt/storage-contract-a-source'
SERVICE_SHA='7b6e3206023a46c2b35bfd5d853fee005f814c06d6d636ccb1b8c757f323f05c'
CASES=('installed-compiled-backend-physical-service-consumer','stale-and-unavailable-mapping-before-update-restart')
CHANGED=('scripts/prepare-named-user.sh','scripts/prepare-alongside.py',
         'qa/privsep-storage/production_guest.py','tests/test_storage_cleanup_api.py',
         'tests/test_alongside_install_boundaries.py')
ADDED=('qa/privsep-storage/contract_a_run.py','qa/privsep-storage/contract_a_guest.py',
       'qa/privsep-storage/contract_a.md','tests/test_custody_prerequisite.py')


def digest(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def load(name,p):
    s=importlib.util.spec_from_file_location(name,p);assert s and s.loader
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


def put(p,v):p.write_text(json.dumps(v,sort_keys=True,indent=2)+'\n');p.chmod(0o600)


def literal(n):
    if isinstance(n,(ast.List,ast.Tuple)):
        values=[literal(v) for v in n.elts]
        return tuple(values) if isinstance(n,ast.Tuple) else values
    if isinstance(n,ast.BinOp) and isinstance(n.op,(ast.Add,ast.Mult)):
        a,b=literal(n.left),literal(n.right)
        return a+b if isinstance(n.op,ast.Add) else a*b
    return ast.literal_eval(n)


def inventory_tests(sources,modules):
    """No imports/collection. Literal parameter products and unittest methods.

    skipif only allows the existing root-UID fixture gate, which execution
    requires root to remove. Dynamic parameters/ids are admission refusals.
    """
    result=[]
    for path in modules:
        tree=ast.parse(sources[path])
        for parent in [tree]+[n for n in tree.body if isinstance(n,ast.ClassDef)]:
            for n in parent.body:
                if not isinstance(n,ast.FunctionDef) or not n.name.startswith('test_'):continue
                suffixes=['']
                for d in n.decorator_list:
                    assert isinstance(d,ast.Call)
                    if ast.unparse(d.func)=='pytest.mark.skipif':
                        assert ast.unparse(d.args[0])=='os.geteuid() != 0';continue
                    assert ast.unparse(d.func)=='pytest.mark.parametrize'
                    arg=literal(d.args[0]);assert ',' not in arg
                    values=literal(d.args[1]);kw={k.arg:literal(k.value) for k in d.keywords}
                    assert set(kw)<={'ids'}
                    ids=kw.get('ids',[str(v).encode('unicode_escape').decode('ascii') if isinstance(v,(str,int,bool)) else arg+str(i) for i,v in enumerate(values)])
                    assert len(ids)==len(values)==len(set(ids))
                    # Pytest's innermost decorator contributes the first ID.
                    suffixes=[(v+'-'+s if s else v) for s,v in itertools.product(suffixes,ids)]
                prefix=path+'::'+(parent.name+'::' if isinstance(parent,ast.ClassDef) else '')+n.name
                result += [prefix+('['+s+']' if n.decorator_list and any(isinstance(d,ast.Call) and ast.unparse(d.func)=='pytest.mark.parametrize' for d in n.decorator_list) else '') for s in suffixes]
    assert len(result)==len(set(result));return sorted(result)


def validate(stage,expected):
    assert re.fullmatch('[0-9a-f]{64}',expected) and digest(stage/'manifest.json')==expected
    d=json.loads((stage/'manifest.json').read_text())
    assert stage==Path('/srv/work/privsep/storage-contract-a')/d['revision'] and Path.cwd()==stage
    assert d['schema']=='storage-contract-a-v1' and d['review_required'] and not d['full_acceptance']
    assert d['reserve_bytes']==RESERVE and d['campaign_remaining_bytes']==REMAINING
    assert not (stage/'evidence').exists(),'one-shot stage already entered'
    assert {p.name for p in stage.iterdir()}==set(d['files'])|{'manifest.json'},'unsealed stage member'
    for p in [stage,stage/'manifest.json',*(stage/n for n in d['files'])]:
        s=p.lstat();assert not stat.S_ISLNK(s.st_mode) and s.st_uid==os.getuid() and not s.st_mode&0o022
    for n,h in d['files'].items():assert '/' not in n and digest(stage/n)==h
    assert digest(__file__)==d['files']['runner.py']
    helpers=load('contract_inherited_static',stage/'custody-static.py')
    rows=helpers.inventory(stage/'source.tar');assert rows==json.loads((stage/'source-members.json').read_text())
    oldrows=helpers.inventory(SIGNED/'source.tar')
    a,b=({r['path']:r for r in v} for v in (oldrows,rows))
    assert set(b)-set(a)==set(ADDED) and not set(a)-set(b)
    assert {n for n in a if a[n]!=b[n]}==set(CHANGED)
    assert all(b[n]==r for r in json.loads((PRIOR/'backend-inputs.json').read_text()) for n in [r['path']])
    assert len(json.loads((PRIOR/'backend-inputs.json').read_text()))==622
    assert helpers.inventory(PRIOR/'service.tar')==json.loads((stage/'service-members.json').read_text())
    assert digest(PRIOR/'service.tar')==d['service_source_archive_sha256']
    assert digest(PRIOR/'service-members.json')==d['files']['service-members.json']
    assert json.loads((stage/'service-source-closure.json').read_text())['independent_git_archive_members_equal'] is True
    with tarfile.open(stage/'source.tar') as t:
        sources={n:t.extractfile(n).read().decode() for n in d['test_modules']}
        for src,dst in (('qa/privsep-storage/contract_a_run.py','runner.py'),('qa/privsep-storage/contract_a_guest.py','guest.py'),
            ('qa/privsep-storage/service_state.py','state.py'),('qa/privsep-storage/service_identity.py','identity.py'),
            ('qa/privsep-storage/custody_signedout_run.py','custody-static.py')):
            assert hashlib.sha256(t.extractfile(src).read()).hexdigest()==d['files'][dst]
    computed=inventory_tests(sources,d['test_modules'])
    # Complete affected modules plus six selected old seams; don't collect/rerun
    # unchanged storage/identity suites merely to obtain more green tests.
    assert sorted(set(computed+d['affected_existing_cases']))==d['expected_pytest_cases']
    for n in ('runner.py','guest.py','state.py','identity.py','custody-static.py'):ast.parse((stage/n).read_text())
    for n in ('get.sh','install.sh','scripts/prepare-named-user.sh'):
        with tarfile.open(stage/'source.tar') as t:subprocess.run(['bash','-n'],input=t.extractfile(n).read(),check=True,timeout=30)
    with (stage/'source.tar').open('rb') as f:
        assert subprocess.check_output(['git','get-tar-commit-id'],stdin=f).decode().strip()==d['revision']
    heads=subprocess.check_output(['git','bundle','list-heads',str(stage/'source.bundle')],text=True)
    assert heads.splitlines()==[d['revision']+' refs/heads/'+d['branch']]
    assert digest(OLD/'evidence/service-image.tar.gz')==SERVICE_SHA
    identity=load('contract_static_image_identity',stage/'identity.py')
    accepted=json.loads((stage/'service.json').read_text())
    with (OLD/'evidence/service-image.tar.gz').open('rb') as f:actual=identity.archive_identity(f)
    assert actual==accepted
    backend=json.loads((stage/'backend.json').read_text())
    prior=load('contract_old_continuation',OLD/'continuation.py')
    production=load('contract_old_production',PRIOR/'runner.py')
    with production.BACKEND_ARCHIVE.open('rb') as f:actual=identity.archive_identity(f)
    assert digest(production.BACKEND_ARCHIVE)==production.BACKEND_SHA==backend['archive_sha256']
    identity.bind_archive(actual,backend,tag=backend['manifest'][0]['RepoTags'][0])
    snapshot=json.loads((stage/'inherited.json').read_text())
    closure=prior.inherited(stage,snapshot,production)
    # Exact old identity proof: don't turn the later failure into full acceptance.
    oldreceipt=json.loads((OLD/'evidence/receipt.json').read_text())
    assert digest(OLD/'evidence/receipt.json')==d['old_failed_receipt_sha256']
    xml=ET.parse(OLD/'evidence/new.xml');assert len(xml.findall('.//testcase'))==27
    assert not any(xml.findall('.//'+v) for v in ('failure','error','skipped'))
    assert digest(OLD/'evidence/new.xml')==oldreceipt['new_python']['xml_sha256']
    for n in ('qa/privsep-storage/service_identity.py','tests/test_storage_service_identity.py'):
        assert a[n]==b[n]
    signed=json.loads((SIGNED/'evidence/receipt.json').read_text())
    assert digest(SIGNED/'evidence/receipt.json')=='2ba970fdea7e782780f1e074d7b1bacd5f336449d72a3d367412f14483315eee'
    assert signed['passed'] and signed['guest_result']['passed'] and len(signed['guest_result']['checks'])==6
    assert digest(SIGNED/'evidence/result.json')=='cebb368b9eb566e0f24e682733a08743bc6e824adbcc3d228f471166e7cf6f8f'
    for p,n,amount in ((OLD/'evidence','old-allocation.json',OLD_ALLOCATED),(SIGNED,'signed-allocation.json',SIGNED_ALLOCATED)):
        observed=helpers.allocated(p);assert observed==json.loads((stage/n).read_text()) and observed['allocated_bytes']==amount
    helpers.absence([PRIOR,OLD,SIGNED,stage])
    for n,row in snapshot['disks'].items():assert helpers.disk_binding(PRIOR/'evidence'/n)==row
    amount=helpers.allocated(stage)['allocated_bytes'];free=shutil.disk_usage(stage).free
    report={'static_only':True,'tests_executed':False,'heavy_executed':False,'submitted':False,'review_required':True,
        'source_revision':d['revision'],'complete_source_members':len(rows),'backend_inputs_verified':622,
        'service_archive_sha256':SERVICE_SHA,'service_source_revision':d['service_revision'],
        'fresh_pytest_count':len(d['expected_pytest_cases']),'expected_native_checks':d['expected_native_checks'],
        'expected_service_cases':list(CASES),'inherited':closure,'inherited_identity_cases':27,'inherited_signedout_checks':6,
        'reserve_bytes':RESERVE,'old_allocation_credited_once':OLD_ALLOCATED,'signed_allocation_credited_once':SIGNED_ALLOCATED,
        'campaign_remaining_bytes':REMAINING,'new_stage_allocated_bytes':amount,'free_bytes':free,
        'required_bytes':RESERVE+max(0,REMAINING-amount),'capacity_admissible':amount<MAX_NEW and free>=RESERVE+max(0,REMAINING-amount),
        'old_474_container_absence':'UNPROVEN: old guest is never booted','full_acceptance':False}
    return d,snapshot,helpers,production,report


def make_runner(base,helpers):
    class ContractRunner(base.Runner):
        def capacity(self):
            amount=helpers.allocated(self.stage)['allocated_bytes'];free=shutil.disk_usage(self.stage).free
            self.receipt['capacity']={'allocated_bytes':amount,'free_bytes':free,'remaining_pending_bytes':max(0,REMAINING-amount),'reserve_bytes':RESERVE}
            self.save();assert amount<=MAX_NEW and free>=RESERVE+max(0,REMAINING-amount)

        def start(self,*args,**kw):
            old=resource.getrlimit(resource.RLIMIT_FSIZE);resource.setrlimit(resource.RLIMIT_FSIZE,(FILE_CAP,old[1]))
            try:return super().start(*args,**kw)
            finally:resource.setrlimit(resource.RLIMIT_FSIZE,old)

        def run(self,*a,**kw):
            self.capacity();rc=super().run(*a,**kw);self.capacity();return rc

        def download(self,name,path=None,required=True):
            rc=self.copy(self.evidence/name,path or ROOT+'/'+name,True,required=required)
            if rc==0 and name.endswith('.json'):return json.loads((self.evidence/name).read_text())

        def regressions(self):
            expected=self.doc['expected_pytest_cases']
            selectors=self.doc['test_modules']+self.doc['affected_existing_cases']
            script='cd '+TREE+'\nmkdir -m700 '+ROOT+'/test-tmp\nexport TMPDIR='+ROOT+'/test-tmp PYTHONDONTWRITEBYTECODE=1\ntest "$(id -u)" = 0\n/usr/bin/python3 -m pytest -q '+shlex.join(selectors)+' --basetemp='+ROOT+'/test-tmp/pytest --junitxml='+ROOT+'/focused.xml\n'
            rc=self.guest('focused-root-regressions',script,900,False)
            self.download('focused.xml')
            xml=ET.parse(self.evidence/'focused.xml');got=[]
            mapping={('.'.join(v[0][:-3].split('/'))+('.'+v[1] if len(v)==3 else ''),v[-1]):s for s in expected for v in [s.split('::')]}
            for c in xml.findall('.//testcase'):got.append(mapping[(c.get('classname'),c.get('name'))])
            self.receipt['focused_pytest']={'exit':rc,'cases':sorted(got),'xml_sha256':digest(self.evidence/'focused.xml')};self.save()
            assert rc==0 and sorted(got)==expected and len(got)==len(set(got))
            assert not any(xml.findall('.//'+v) for v in ('failure','error','skipped'))

        def attest(self,kind,when):
            # Locator from exact save/load, not a mutable tag as evidence. Both
            # before AND after installer, export complete config/layers again.
            script="import importlib.util,json,subprocess,sys;from pathlib import Path\ns=importlib.util.spec_from_file_location('identity',"+repr(ROOT+'/identity.py')+");m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\na=json.loads(Path("+repr(ROOT+'/'+kind+'.json')+").read_text());tag="+repr('privsep-wam:595523fb' if kind=='backend' else 'privsep-service:e1ba0d0564f1')+"\nv=m.attest_loaded(a,tag);Path("+repr(ROOT+'/'+kind+'-attestation-'+when+'.json')+").write_text(json.dumps(v,sort_keys=True,indent=2)+'\\n')\n"
            self.guest('attest-'+kind+'-'+when,'/usr/bin/python3 -I -B -c '+shlex.quote(script),300)
            return self.download(kind+'-attestation-'+when+'.json')

        def load_image(self,kind,path,tag):
            target=ROOT+'/'+kind+'.tar.gz';self.copy(path,target)
            script="import json,re,subprocess;from pathlib import Path\np=subprocess.run(['docker','load','-i',"+repr(target)+"],capture_output=True,text=True,check=True,timeout=300)\nids=re.findall(r'^Loaded image ID: (sha256:[0-9a-f]{64})$',p.stdout,re.M)\nif ids:\n assert len(ids)==1;locator=ids[0]\nelse:\n assert "+repr(kind)+"=='backend';a=json.loads(Path("+repr(ROOT+'/backend.json')+").read_text());tags=a['manifest'][0]['RepoTags'];assert len(tags)==1 and p.stdout.strip()=='Loaded image: '+tags[0];locator=tags[0]\nsubprocess.run(['docker','tag',locator,"+repr(tag)+"],check=True,timeout=30)\n"
            self.guest('load-'+kind,'/usr/bin/python3 -I -B -c '+shlex.quote(script),360)
            # Bound locator+content; no rebuild, no arbitrary-label acceptance.
            return self.attest(kind,'before')

        def perform(self,production):
            for n,dst in (('id_ed25519',self.key),('seed.iso',self.evidence/'seed.iso')):
                shutil.copyfile(PRIOR/'evidence'/n,dst);dst.chmod(0o600)
            for n,dst in (('disk.qcow2',self.disk),('secondary.qcow2',self.secondary)):
                self.run('thin-'+n,['qemu-img','create','-f','qcow2','-F','qcow2','-b',PRIOR/'evidence'/n,dst])
            self.start(1)
            self.guest('new-synthetic-marker','touch /run/wizard-storage-production-synthetic\numask 077\nmkdir -m700 '+ROOT+' '+STATE_ROOT+'\n')
            for n in ('source.tar','source-members.json','guest.py','identity.py','backend.json','service.json'):
                self.copy(self.stage/n,ROOT+'/'+n)
            for n in ('state.py','binding.json'):self.copy(self.stage/n,STATE_ROOT+'/'+n)
            self.guest('committed-before','/usr/bin/python3 -I -B '+STATE_ROOT+'/state.py before')
            self.download('committed-before.json',STATE_ROOT+'/committed-before.json')
            self.guest('exact-source','umask 022\nmkdir -m755 '+TREE+'\ntar --no-same-permissions -xf '+ROOT+'/source.tar -C '+TREE+'\n')
            self.regressions()  # first checkpoint; abort before images/install if red
            self.phase('focused-regressions-passed')
            self.guest('docker-real-prerequisite','test -S /var/run/docker.sock\ndocker info >/dev/null\n')
            before={}
            before['backend']=self.load_image('backend',production.BACKEND_ARCHIVE,'privsep-wam:595523fb')
            before['service']=self.load_image('service',OLD/'evidence/service-image.tar.gz','privsep-service:e1ba0d0564f1')
            self.receipt['image_identity_before']=before;self.save()
            # The root helper is precisely get.sh's selected protected entry. Its
            # profile observer changes no callable/return/unit/source/environment.
            self.phase('actual-none-root-installer')
            args=[self.doc['revision'],before['backend']['engine_image_id'],before['service']['engine_image_id']]
            rc=self.guest('actual-none-install','/usr/bin/python3 -I -B '+ROOT+'/guest.py install '+shlex.join(args),7200,False)
            self.receipt['native']=self.download('native.json');self.save();assert rc==0
            self.copy(PRIOR/'service-members.json','/root/storage-production/service-members.json')
            # Run exact QA assertions from current committed bytes, never old cleanup.
            guest=TREE+'/qa/privsep-storage/production_guest.py'
            for name,command in ((CASES[0],'installed '+shlex.join(args[1:])),(CASES[1],'stale-mapping')):
                self.guest(name,'/usr/bin/python3 -I -B '+guest+' '+command,300)
                self.receipt.setdefault('actual_cases',[]).append({'name':name,'passed':True});self.save()
            for n in ('installed','stale-mapping'):self.download(n+'.json','/root/storage-production/'+n+'.json')
            self.guest('native-security','/usr/bin/python3 -I -B '+ROOT+'/guest.py security',300)
            self.receipt['native']=self.download('native.json');assert self.receipt['native']['passed'];self.save()
            after={k:self.attest(k,'after') for k in ('backend','service')}
            assert after==before;self.receipt['image_identity_after']=after;self.save()
            self.guest('committed-after','/usr/bin/python3 -I -B '+STATE_ROOT+'/state.py after')
            self.download('committed-after.json',STATE_ROOT+'/committed-after.json')
            assert (self.evidence/'committed-before.json').read_bytes()==(self.evidence/'committed-after.json').read_bytes()
            self.receipt['committed_storage_metadata_unchanged']=True;self.save()
            self.guest('stop-native-services','/usr/bin/python3 -I -B '+ROOT+'/guest.py cleanup',120)
            self.guest('exact-cleanup-with-negative','/usr/bin/python3 -I -B '+ROOT+'/guest.py cleanup-containers',240)
            self.receipt['native']=self.download('native.json')
            for n in ('container-cleanup.json','cleanup-negative.json'):self.download(n)
            self.receipt['new_guest_container_absence_proven']=True;self.receipt['passed']=True;self.save()

        def finish(self):
            # Preserve first failure/progress. Exclusive guest native lock
            # refuses cleanup while an interrupted installer is still writing.
            self.download('native.json',required=False)
            if not self.receipt.get('passed') and 'actual-none-root-installer' in self.receipt['reached_phases']:
                rc=self.guest('bounded-failed-install-cleanup','/usr/bin/python3 -I -B '+ROOT+'/guest.py cleanup-failed',180,False)
                self.download('container-cleanup.json',required=False)
                self.receipt['failed_cleanup_exit']=rc
                if rc!=0:self.receipt['cleanup_errors'].append('failed-install-exact-cleanup-unproven')
            if self.receipt.get('passed'):
                self.guest('one-poweroff','sync\nsystemctl poweroff',60,False);self.proc.wait(timeout=90)

    return ContractRunner


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',required=True,type=Path);p.add_argument('--manifest-sha256',required=True)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--preflight',action='store_true');g.add_argument('--execute',action='store_true')
    args=p.parse_args();d,snapshot,helpers,production,report=validate(args.stage,args.manifest_sha256)
    if args.preflight:print(json.dumps(report,sort_keys=True));return 0
    base=load('contract_vm_transport',args.stage/'storage-runner.py');base.native_lock()
    assert report['capacity_admissible'],'reserve plus original campaign peak unavailable'
    os.umask(0o077);tmp=args.stage/'tmp';tmp.mkdir(mode=0o700);os.environ['TMPDIR']=str(tmp);os.environ['PYTHONDONTWRITEBYTECODE']='1'
    r=make_runner(base,helpers)(args.stage,d)
    r.receipt.update(scope='contract A focused root fixtures plus two real Service continuations',full_acceptance=False,
        admission=report,old_474_container_absence='UNPROVEN',owner_identity_positive_proven=False)
    r.save()
    def interrupt(sig,frame):raise TimeoutError('bounded contract A native deadline')
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):signal.signal(sig,interrupt)
    signal.alarm(10800)
    try:r.perform(production)
    except BaseException as e:
        r.receipt['first_error']={'type':type(e).__name__,'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(e.__traceback__)]}
        r.receipt['passed']=False;r.save()
    finally:
        signal.alarm(300)
        try:
            if r.proc is not None and r.proc.poll() is None:r.finish()
        except BaseException as e:r.receipt['cleanup_errors'].append('guest-finalize-'+type(e).__name__)
        try:r.stop()
        except BaseException as e:r.receipt['cleanup_errors'].append('stop-'+type(e).__name__)
        try:
            helpers.absence([PRIOR,OLD,SIGNED,args.stage])
            assert len(r.receipt['boots'])<=1 and all(b['stopped'] and not Path('/proc',str(b['pid'])).exists() for b in r.receipt['boots'])
            for n,row in snapshot['disks'].items():assert helpers.disk_binding(PRIOR/'evidence'/n)==row
            for n,row in snapshot['files'].items():assert digest(PRIOR/n)==row['sha256']
            assert helpers.allocated(OLD/'evidence')==json.loads((args.stage/'old-allocation.json').read_text())
            assert helpers.allocated(SIGNED)==json.loads((args.stage/'signed-allocation.json').read_text())
            r.receipt['original_disks_and_old_evidence_unchanged']=True;r.receipt['all_boot_pids_absent']=True;r.capacity()
        except BaseException as e:r.receipt['cleanup_errors'].append('preservation-'+type(e).__name__)
        r.receipt['passed']=bool(r.receipt['passed'] and not r.receipt['cleanup_errors'] and r.receipt.get('new_guest_container_absence_proven') and r.receipt.get('all_boot_pids_absent'))
        r.save();signal.alarm(0)
    print(json.dumps({'passed':r.receipt['passed'],'receipt':str(r.evidence/'receipt.json'),'full_acceptance':False}));return 0 if r.receipt['passed'] else 1


if __name__=='__main__':sys.exit(main())
