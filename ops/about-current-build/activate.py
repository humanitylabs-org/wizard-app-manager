#!/usr/bin/env python3
"""CAS metadata carrier + runtime-log redaction only. No core restart or credential rotation."""
import argparse,fcntl,hashlib,json,os,signal,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def sha(b):return hashlib.sha256(b).hexdigest()
def save(n,d):(ROOT/n).write_text(json.dumps(d,indent=2)+'\n')
def val(unit,k):return subprocess.check_output(['systemctl','--user','show',unit,'-p',k,'--value'],text=True,timeout=10).strip()
def atomic(p,b,mode):
    t=p.with_name(p.name+'.about-build-'+str(os.getpid()))
    with t.open('xb') as f:f.write(b);f.flush();os.fsync(f.fileno())
    os.chmod(t,mode);os.replace(t,p)
def inputs():
    m=json.loads((ROOT/'manifest.json').read_text())
    for n,h in m['inputs'].items():assert sha((ROOT/n).read_bytes())==h,'sealed input drift '+n
    return m,json.loads((ROOT/'provenance.json').read_text())
def state(d):
    home=Path(d['source_repo']).parents[1]
    assert (home/'.local/share/wizard-ux/current').resolve()==Path(d['current']),'served shell changed; restage exact new source'
    assert (Path(d['current'])/'RELEASE_SHA').read_text().strip()==d['commit']
    assert int(val('wizard-mobile.service','MainPID'))==d['service_pid'],'core identity changed'
    assert int(val('private-gateway-https.service','MainPID'))==d['gateway_pid'],'gateway identity changed'
    assert sha((home/'.local/bin/caddy-private-gateway').read_bytes())==d['gateway_binary_sha256']
    shell=json.loads(subprocess.check_output(['docker','inspect','wizard-interface-candidate'],timeout=20))[0]
    assert {k:shell[k] for k in ['Id','Image']}==d['shell_container'],'serving shell changed'
    assert all(sha((Path(d['current'])/'web'/n).read_bytes())==h for n,h in d['web_files'].items()),'immutable served bytes changed'
    return home

def validate(home,p,pid):
    env=dict(s.split('=',1) for s in Path('/proc',str(pid),'environ').read_bytes().decode().split('\0') if '=' in s)
    r=subprocess.run([str(home/'.local/bin/caddy-private-gateway'),'adapt','--config',str(p),'--adapter','caddyfile','--validate'],env=env,capture_output=True,timeout=30)
    check={'returncode':r.returncode,'stdout_sha256':sha(r.stdout),'stderr_sha256':sha(r.stderr)}
    assert r.returncode==0,'Caddy validate failed; raw expanded config deliberately not projected'
    return check

def reload_gateway(d,label):
    pid=d['gateway_pid'];start=time.time();os.kill(pid,signal.SIGUSR1)
    events=[];p=None
    accepted={'config reloaded','Reload succeeded','reload complete','reloaded config','successfully loaded new config'}
    for _ in range(50):
        assert int(val('private-gateway-https.service','MainPID'))==pid,'gateway restart refused'
        # Raw journals stay inside the bounded model-blind process; only fixed event names and hashes leave it.
        p=subprocess.run(['journalctl','--user','-u','private-gateway-https.service','--since','@'+str(start),'--no-pager','-o','cat'],capture_output=True,timeout=10)
        for line in p.stdout.splitlines():
            try:x=json.loads(line)
            except ValueError:continue
            if x.get('msg') in accepted:events.append({'event':x['msg'],'timestamp':x.get('ts')})
        if events:break
        time.sleep(.2)
    assert p is not None
    result={'pid_before':pid,'pid_after':int(val('private-gateway-https.service','MainPID')),'same_pid':True,'positive_events':events,'journal_sha256':sha(p.stdout),'raw_journal_projected':False}
    save('reload-'+label+'.json',result);assert events,'No verified positive reload event'
    return result

def main():
    a=argparse.ArgumentParser();a.add_argument('action',choices=['preflight','apply','rollback']);args=a.parse_args()
    m,d=inputs();home=state(d)
    lock=home/'.local/state/wizard-app-promotions/wam.lock'
    with lock.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state(d)  # repeat under the established live configuration lock
        caddy=Path(d['controls']['caddy']['path'])
        for k,entry in d['controls'].items():
            if k=='caddy':continue
            assert sha(Path(entry['path']).read_bytes())==entry['sha256'],'sibling control drift '+k
        before=(ROOT/'Caddyfile.before').read_bytes();candidate=(ROOT/'Caddyfile.candidate').read_bytes()
        if args.action!='rollback':assert caddy.read_bytes()==before,'live Caddy changed; rebase under lock, never overwrite siblings'
        assert (ROOT/'carrier/build.json').read_bytes()==('{"commit":"'+d['commit']+'"}\n').encode()
        validation=validate(home,ROOT/('Caddyfile.before' if args.action=='rollback' else 'Caddyfile.candidate'),d['gateway_pid'])
        if args.action=='preflight':
            print(json.dumps({'preflight':True,'commit':d['commit'],'live_unmodified':True,'core_unmodified':True,'validation':validation}));return
        if args.action=='apply':
            receipt=json.loads((ROOT/'candidate-results/receipt.json').read_text())
            assert receipt['accepted'] and receipt['phase']=='candidate' and receipt['commit']==d['commit']
            assert receipt['manifest_sha256']==sha((ROOT/'manifest.json').read_bytes())
            assert receipt['fixture']['accepted'] and receipt['fixture']['all_processes_stopped']
            assert receipt['browser']['accepted'] and receipt['browser']['all_browser_processes_stopped']
            assert receipt['browser']['source_commit']==d['commit']
            target=candidate
        else:
            assert caddy.read_bytes()==candidate,'rollback CAS refusal: later sibling changes exist'
            target=before
        atomic(caddy,target,d['controls']['caddy']['mode'])
        try:
            reload_gateway(d,args.action)
            assert caddy.read_bytes()==target
            assert int(val('wizard-mobile.service','MainPID'))==d['service_pid']
        except Exception:
            atomic(caddy,before,d['controls']['caddy']['mode']);reload_gateway(d,'compensated');raise
        result={'applied':args.action=='apply','rollback':args.action=='rollback','commit':d['commit'],'metadata_only':True,'same_gateway_pid':True,'core_identity_unchanged':True,'readback_config_sha256':sha(caddy.read_bytes()),'rotation_performed':False,'live_owner_render_acceptance_pending':args.action=='apply'}
        save('activation-'+args.action+'.json',result);print(json.dumps(result))
if __name__=='__main__':main()
