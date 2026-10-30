#!/usr/bin/python3
"""QA-only read observers around the REAL separated root installer, no substitutes.

get.sh's selected root helper is executed unchanged with its exact argv. A Python
profile hook observes call/return of its upstream subprocess; it never replaces a
command or changes a return. Full get.sh discovery/prepare_server is NOT claimed.
Only the admitted synthetic committed guest is supported. Never run on a host.
"""
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import threading
import time

ROOT = Path('/root/storage-contract-a')
TREE = Path('/opt/storage-contract-a-source')
BASE = Path('/var/lib/wizard-os')
STATE = BASE/'.wizard-os'
DEP = Path('/etc/systemd/system/tailscaled.service.d/50-wizard-owner-approval.conf')
CHECKS = ('fresh-synthetic-signedout-inputs', 'actual-none-dependency-upstream-metadata',
          'native-socket-before-dependency', 'same-socket-worker-restart',
          'forged-owner-origin-action-refusal', 'native-daemon-dependency-restart')
report = {'schema':'storage-contract-a-guest-v1', 'passed':False, 'checks':[],
          'first_error':None, 'commands':[], 'cleanup_errors':[]}


def save():
    (ROOT/'native.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    (ROOT/'native.json').chmod(0o600)


def sha(raw): return hashlib.sha256(raw).hexdigest()


def run(label, args, timeout=60, required=True):
    # Native installer output can contain owner URLs. Hash, never export it.
    p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, env={**os.environ, 'LANG':'C'})
    try: out, err = p.communicate(timeout=timeout)
    except BaseException:
        try: os.killpg(p.pid, __import__('signal').SIGKILL)
        except ProcessLookupError: pass
        p.communicate(timeout=15); raise
    report['commands'].append({'operation':label, 'exit':p.returncode,
        'stdout_sha256':sha(out), 'stderr_sha256':sha(err),
        'stdout_bytes':len(out), 'stderr_bytes':len(err)})
    save()
    if required: assert p.returncode == 0, label
    return subprocess.CompletedProcess(args,p.returncode,out,err)


def check(name, operation):
    operation(); report['checks'].append({'name':name,'passed':True}); save()


def module(name, path):
    spec = importlib.util.spec_from_file_location(name,path); assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


def custody():
    sys.path.insert(0,str(TREE))
    from wam import approval_custody
    return approval_custody


def signed_snapshot(label):
    # Reuse exact official native inspector, not the synthetic CLI regressions.
    s = module('contract_native_signedout_inspector',TREE/'qa/privsep-storage/custody_signedout_guest.py')
    s.ROOT = ROOT; s.report = report; s.save = save
    return s.snapshot(label)


def show(unit):
    raw = run('show-'+unit,['systemctl','show',unit]).stdout.decode()
    d = dict(r.split('=',1) for r in raw.splitlines() if '=' in r)
    return {k:d.get(k) for k in ('LoadState','ActiveState','SubState','MainPID','Requires','After',
                                'User','ActiveEnterTimestampMonotonic')}


def inode():
    rows = []
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        a = line.split()
        if a[1] == '0100007F:1E70' and a[3] == '0A': rows.append(int(a[9]))
    assert len(rows) <= 1
    return rows[0] if rows else None


def metadata(label):
    paths = (Path('/'),Path('/var'),Path('/var/lib'),BASE,BASE/'runtipi',BASE/'runtipi/user-config',
             BASE/'runtipi/user-config/.wizard-host-authority.lock',
             BASE/'runtipi/user-config/wizard-host-approvals.json')
    result = {}
    for p in paths:
        if not os.path.lexists(p): result[str(p)] = {'absent':True}; continue
        s = p.lstat(); assert not stat.S_ISLNK(s.st_mode)
        acl = subprocess.check_output(['getfacl','-cpn',str(p)],stderr=subprocess.PIPE,timeout=15).decode()
        result[str(p)] = {k:getattr(s,k) for k in ('st_dev','st_ino','st_uid','st_gid')}
        result[str(p)].update(mode=stat.S_IMODE(s.st_mode), kind='directory' if p.is_dir() else 'file',acl=acl)
    report.setdefault('root_control_metadata',{})[label] = result; save(); return result


def fetched_source_closure(revision):
    # The real root helper has already chowned its clone to wizard-system.
    # Respect Git's ownership guard; root still independently hashes ALL bytes.
    checkout = BASE/'wizard-app-manager'
    assert subprocess.check_output(['runuser','-u','wizard-system','--','git','-C',str(checkout),
                                    'rev-parse','HEAD'],timeout=30).decode().strip()==revision
    for row in json.loads((ROOT/'source-members.json').read_text()):
        p=checkout/row['path']
        raw=os.fsencode(os.readlink(p)) if row['kind']=='symlink' else p.read_bytes()
        assert sha(raw)==row['sha256']
    report['root_fetched_complete_source_closure']=True


def no_manager_writer():
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal() or int(p.name)==os.getpid():continue
        try: args=(p/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError,PermissionError,ProcessLookupError):continue
        assert not (b'-m' in args and b'wam' in args), 'concurrent manager writer before installer'


def fresh():
    assert os.geteuid()==0 and Path('/run/wizard-storage-production-synthetic').is_file()
    assert not BASE.exists() and not Path('/usr/bin/tailscale').exists()
    assert not Path('/var/lib/tailscale').exists() and not DEP.exists() and inode() is None
    no_manager_writer()
    s=module('contract_source_inspector',TREE/'qa/privsep-storage/custody_signedout_guest.py')
    s.ROOT=ROOT; s.TREE=TREE; s.report=report
    try:s.source_closure()
    except AssertionError:
        # Preserve the strict checker and first failure. Export only bounded
        # relative membership metadata, never file bodies or installer output.
        import tarfile
        with tarfile.open(ROOT/'source.tar') as archive:
            expected={m.name.rstrip('/') for m in archive}
        actual={str(p.relative_to(TREE)) for p in TREE.rglob('*')}
        missing=sorted(expected-actual);extra=sorted(actual-expected)
        report['source_closure_membership']={
            'expected_count':len(expected),'actual_count':len(actual),
            'missing_count':len(missing),'extra_count':len(extra),
            'missing':[p[:256] for p in missing[:64]],
            'extra':[p[:256] for p in extra[:64]],
            'path_limit':64,'path_character_limit':256,
            'truncated':any(len(v)>64 or any(len(p)>256 for p in v) for v in (missing,extra))}
        save();raise
    report['exact_root_helper_argv']=['prepare-alongside.py','hermes','/home/hermes/.hermes',sys.argv[2]]
    report['bootstrap_discovery_exercised']=False


def install():
    revision, backend, service = sys.argv[2:5]
    assert re.fullmatch('[0-9a-f]{40}',revision)
    assert all(re.fullmatch('sha256:[0-9a-f]{64}',v) for v in (backend,service))
    helper=TREE/'scripts/prepare-alongside.py'
    watch_stop=threading.Event(); observations=[]; errors=[]; watcher=None
    def observe():
        try:
            while not watch_stop.is_set():
                i=inode(); dep=DEP.exists()
                if i or dep:
                    row={'monotonic_ns':time.monotonic_ns(),'inode':i,'dependency_exists':dep}
                    if not observations or (row['inode'],dep)!=(observations[-1]['inode'],observations[-1]['dependency_exists']):observations.append(row)
                watch_stop.wait(0.001)
        except BaseException as e:errors.append(type(e).__name__)
    def profile(frame,event,arg):
        nonlocal watcher
        if frame.f_code.co_filename!=str(helper) or frame.f_code.co_name!='run': return
        argv=[str(a) for a in frame.f_locals.get('argv',[])]
        if event=='return' and arg is None:
            # Python profiles exceptional exits as return(None). Preserve the
            # real subprocess exception; never mask it with an observer oracle.
            if any(a.endswith('/scripts/install-approval-custody.py') for a in argv):
                watch_stop.set()
                if watcher:watcher.join(timeout=10)
                report['socket_dependency_observations']=observations
            report['profiled_command_exception']=True;save();return
        if any(a.endswith('/scripts/prepare-named-user.sh') for a in argv):
            if event=='call':
                no_manager_writer(); metadata('before-upstream')
                fetched_source_closure(revision)
            elif event=='return': metadata('after-upstream-before-normalization')
        if any(a.endswith('/scripts/install-approval-custody.py') for a in argv):
            if event=='call':
                metadata('before-custody'); signed_snapshot('after_actual_dependency')
                assert not DEP.exists() and inode() is None
                watcher=threading.Thread(target=observe,daemon=True); watcher.start()
            elif event=='return':
                watch_stop.set(); watcher.join(timeout=10); assert not watcher.is_alive()
                report['socket_dependency_observations']=observations; report['observer_errors']=errors; save()
                assert not errors and observations
                bound=next(r for r in observations if r['inode'] is not None)
                committed=next(r for r in observations if r['dependency_exists'])
                assert bound['monotonic_ns']<committed['monotonic_ns'], 'no native before-dependency observation'
                assert bound['inode']==committed['inode']==custody().require_custody()
                report['custody_inode']=bound['inode']
                check('native-socket-before-dependency',lambda:None)
        if event=='call' and any(a.endswith('/install.sh') for a in argv):
            assert custody().require_custody()==report['custody_inode']
            clean=dict(v.split('=',1) for v in argv if '=' in v)
            assert clean['WAM_IMAGE']==backend and clean['WIZARD_AGENT_SERVICE_IMAGE']==service
            assert clean['WIZARD_PRIVATE_ACCESS']=='none'
            report['immutable_image_pins_survived_actual_manager_handoff']=True; save()
    os.environ.update(WIZARD_PRIVATE_ACCESS='none',WAM_IMAGE=backend,WIZARD_AGENT_SERVICE_IMAGE=service,
                      WIZARD_RELEASE='none',WIZARD_GITHUB='0',WIZARD_AGENT_SAFE='0',PYTHONDONTWRITEBYTECODE='1')
    old=sys.argv[:]; sys.argv=[str(helper),'hermes','/home/hermes/.hermes',revision]
    # Actual production main, actual subprocess.run, no success mocks or source edits.
    sys.setprofile(profile)
    try: runpy.run_path(str(helper),run_name='__main__')
    finally:
        sys.setprofile(None); sys.argv=old; watch_stop.set()
        if watcher: watcher.join(timeout=10)
    before=report['root_control_metadata']['before-upstream']
    after=report['root_control_metadata']['after-upstream-before-normalization']
    final=metadata('after-full-installer')
    for p in (str(BASE/'runtipi'),str(BASE/'runtipi/user-config')):
        assert (before[p]['st_dev'],before[p]['st_ino'])==(after[p]['st_dev'],after[p]['st_ino'])==(final[p]['st_dev'],final[p]['st_ino'])
        assert final[p]['mode']&0o022==0
    for p in (str(BASE/'runtipi/user-config/.wizard-host-authority.lock'),str(BASE/'runtipi/user-config/wizard-host-approvals.json')):
        assert final[p]['st_uid'] in (0,__import__('pwd').getpwnam('wizard-system').pw_uid) and final[p]['mode']&0o022==0
    assert report['immutable_image_pins_survived_actual_manager_handoff']
    signed_snapshot('after_actual_none_install')


def action_state():
    # Finite authority records only, not tokens, private agent/app data or logs.
    names=('owner-login','owner-intents','approval-origin.json','runtipi-operator.json','developer.json')
    result={}
    for n in names:
        p=STATE/n
        if not os.path.lexists(p):result[n]={'absent':True};continue
        assert p.is_file() and not p.is_symlink()
        s=p.lstat();result[n]={'sha256':sha(p.read_bytes()),'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}
    return result


def refuse(headers):
    rows=[]
    for method,path,body in (('GET','/developer',None),('POST','/intent','{"action":"grant","seconds":60}')):
        c=http.client.HTTPConnection('127.0.0.1',7792,timeout=30)
        try:
            c.request(method,path,body=body,headers={**headers,**({'Content-Type':'application/json'} if body else {})})
            r=c.getresponse();raw=r.read(8193)
            row={'method':method,'path':path,'status':r.status,'owner_only':raw and json.loads(raw)=={'error':'owner only'}}
            rows.append(row); assert r.status==403 and row['owner_only']
        finally:c.close()
    return rows


def restart_worker():
    c=custody();i=c.require_custody();before=show(c.SERVICE)
    run('restart-actual-manager-worker',['systemctl','restart',c.SERVICE])
    after=show(c.SERVICE)
    assert before['MainPID']!=after['MainPID'] and after['ActiveState']=='active'
    assert int(after['MainPID'])>1 and c.require_custody()==i==report['custody_inode']
    report['same_socket_restart']={'inode':i,'before':before,'after':after}


def forgery():
    before=action_state(); rows=[]
    for name,headers in (('anonymous',{}),('forged-owner',{'Tailscale-User-Login':'synthetic-owner@example.invalid','Tailscale-User-Name':'Synthetic Owner'}),
        ('forged-origin',{'Origin':'https://synthetic-forged.example.invalid:8446','Referer':'https://synthetic-forged.example.invalid:8446/developer'}),
        ('forged-owner-and-origin',{'Tailscale-User-Login':'synthetic-owner@example.invalid','Origin':'https://synthetic-forged.example.invalid:8446','X-Forwarded-Host':'synthetic-forged.example.invalid:8446','X-Forwarded-Proto':'https'})):
        rows.append({'control':name,'responses':refuse(headers)})
        assert action_state()==before
    assert custody().require_custody()==report['custody_inode']
    report['forged_refusals']=rows;report['authority_state_before']=before;report['authority_state_after']=action_state()
    signed_snapshot('after_forged_requests')


def restart_daemon():
    c=custody(); i=c.require_custody()
    run('stop-native-daemon',['systemctl','stop','tailscaled.service'])
    assert show('tailscaled.service')['ActiveState']=='inactive' and c.require_custody()==i
    report['socket_before_daemon_start']={'inode':i,'socket':show(c.SOCKET)}
    run('start-native-daemon-with-loaded-dependency',['systemctl','start','tailscaled.service'])
    ts=show('tailscaled.service');so=show(c.SOCKET)
    assert c.SOCKET in ts['After'].split() and c.SOCKET in ts['Requires'].split()
    assert int(so['ActiveEnterTimestampMonotonic'])<int(ts['ActiveEnterTimestampMonotonic'])
    assert c.require_custody()==i
    signed_snapshot('after_native_daemon_restart')


def cleanup_containers():
    # A valid unrelated app project is a real negative control, owned solely by
    # this QA fixture. Cleanup must leave it untouched while removing campaign IDs.
    m=module('contract_actual_cleanup',TREE/'qa/privsep-storage/production_guest.py');m.ROOT=ROOT
    service=json.loads((ROOT/'service-attestation-before.json').read_text())['engine_image_id']
    status,info=m.daemon_request('GET','/info');assert status==200
    engine=info['ID']
    raw=run('create-unowned-control',['docker','create','--label','com.docker.compose.project=notes_wizardapps',
        '--entrypoint=/bin/sh',service,'-c','exit 0']).stdout.decode().strip()
    assert re.fullmatch('[0-9a-f]{64}',raw)
    (ROOT/'negative-control-id').write_text(raw+'\n')
    before_status,before=m.daemon_request('GET','/containers/'+raw+'/json');assert before_status==200 and before['Id']==raw
    try:m.cleanup_containers()
    except AssertionError:pass
    else:raise AssertionError('unowned negative was silently removed or ignored')
    first=json.loads((ROOT/'container-cleanup.json').read_text())
    (ROOT/'cleanup-negative.json').write_text(json.dumps(first,sort_keys=True,indent=2)+'\n')
    assert first['unowned_exact_ids']==[raw] and not first['passed']
    assert first['first_error_type']=='AssertionError' and first['absent_exact_ids']==first['attempted_exact_ids']
    assert len(first['attempted_exact_ids'])>=3
    status,after=m.daemon_request('GET','/containers/'+raw+'/json');assert status==200 and after==before
    status,info=m.daemon_request('GET','/info');assert status==200 and info['ID']==engine
    # Exact QA-owned ID only; unrelated fixture is preserved until THIS removal.
    status,_=m.daemon_request('DELETE','/containers/'+raw+'?force=true');assert status==204
    status,_=m.daemon_request('GET','/containers/'+raw+'/json');assert status==404
    m.cleanup_containers()
    final=json.loads((ROOT/'container-cleanup.json').read_text())
    status,info=m.daemon_request('GET','/info');assert status==200 and info['ID']==engine
    assert final['passed'] and final['readback_absent']
    report['new_exact_cleanup']={'negative_id':raw,'negative_survived':True,'negative_fixture_owned_removal_verified':True,
        'engine_id':engine,'first_report_sha256':sha((ROOT/'cleanup-negative.json').read_bytes()),
        'final_report_sha256':sha((ROOT/'container-cleanup.json').read_bytes())}
    save()


def cleanup():
    # Called only once no foreground installer is active (controller owns its
    # bounded process group); preserve socket until VM poweroff, no retirement.
    if Path('/usr/sbin/tailscaled').exists():
        run('cleanup-stop-native-daemon',['systemctl','stop','tailscaled.service'])
        assert show('tailscaled.service')['ActiveState']=='inactive'
        report['native_daemon_stopped']=True
    for u in ('wizard-app-manager-api.service','wizard-runtipi.service'):
        p=run('cleanup-show-'+u,['systemctl','show',u,'-p','LoadState','--value'],required=False)
        if p.stdout.strip()==b'loaded':run('cleanup-stop-'+u,['systemctl','stop',u])
    save()


def main():
    global report
    assert sys.argv[1] in ('fresh','install','security','cleanup','cleanup-containers','cleanup-failed')
    if (ROOT/'native.json').exists():report=json.loads((ROOT/'native.json').read_text())
    lock=os.open(ROOT/'native.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        if sys.argv[1]=='fresh':
            assert re.fullmatch('[0-9a-f]{40}',sys.argv[2])
            assert not report['checks']
            check(CHECKS[0],fresh)
        elif sys.argv[1]=='install':
            # The one fresh/source check ran before image loading, with the
            # same revision. Never call fresh again after native installation.
            assert report['checks']==[{'name':CHECKS[0],'passed':True}]
            assert report['exact_root_helper_argv']==['prepare-alongside.py','hermes','/home/hermes/.hermes',sys.argv[2]]
            # Installer output stays inside VM; controller receives only fixed
            # booleans/digests in native.json. Root source may print owner URLs.
            with (ROOT/'installer-private.log').open('wb') as out:
                oldout=os.dup(1);olderr=os.dup(2);os.dup2(out.fileno(),1);os.dup2(out.fileno(),2)
                try:install()
                finally:os.dup2(oldout,1);os.dup2(olderr,2);os.close(oldout);os.close(olderr)
            report['checks'].append({'name':CHECKS[1],'passed':True})
        elif sys.argv[1]=='security':
            check(CHECKS[3],restart_worker);check(CHECKS[4],forgery);check(CHECKS[5],restart_daemon)
            assert sorted(r['name'] for r in report['checks'])==sorted(CHECKS)
            report['passed']=True
        elif sys.argv[1]=='cleanup-containers':cleanup_containers()
        elif sys.argv[1]=='cleanup-failed':
            # The exclusive native lock plus orphan-child inventory refuses any
            # still-running installer. A killed Python parent cannot hide its
            # non-inherited-lock subprocesses. No cleanup changes acceptance.
            for p in Path('/proc').iterdir():
                if not p.name.isdecimal() or int(p.name)==os.getpid():continue
                try:args=[os.fsdecode(a) for a in (p/'cmdline').read_bytes().split(b'\0') if a]
                except (FileNotFoundError,PermissionError,ProcessLookupError):continue
                assert not any(a.endswith(('/prepare-alongside.py','/prepare-named-user.sh','/install.sh','/install-approval-custody.py')) for a in args), 'orphan installer writer'
            cleanup()
            m=module('contract_failed_cleanup',TREE/'qa/privsep-storage/production_guest.py');m.ROOT=ROOT
            if (ROOT/'negative-control-id').exists():
                cid=(ROOT/'negative-control-id').read_text().strip();assert re.fullmatch('[0-9a-f]{64}',cid)
                status,row=m.daemon_request('GET','/containers/'+cid+'/json')
                if status==200:
                    assert row['Id']==cid and row['Config']['Labels']['com.docker.compose.project']=='notes_wizardapps'
                    status,_=m.daemon_request('DELETE','/containers/'+cid+'?force=true');assert status==204
                status,_=m.daemon_request('GET','/containers/'+cid+'/json');assert status==404
            m.cleanup_containers();report['failed_run_exact_cleanup_readback']=True
        else:cleanup()
    except BaseException as e:
        if report['first_error'] is None:report['first_error']={'type':type(e).__name__,'phase':sys.argv[1]}
        report['passed']=False;raise
    finally:save();os.close(lock)


if __name__=='__main__':main()
