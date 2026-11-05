#!/usr/bin/env python3
"""Finite approved API-only source + exact owner-gated route; CAS rollback."""
import datetime, fcntl, hashlib, io, json, os, signal, subprocess, sys, tarfile, time, urllib.request
from pathlib import Path
R=Path(__file__).resolve().parent
H=Path('/home/telos')
REPO=H/'workspace/wam-about-readonly-gateway-20261105'
OLD='c4b6f847b8b5bf8a0337928dfbadffdc99b565c9'
SHA='319650ad9976644c63790d505e06ce923a84b3f6'
SOURCE=H/'.local/share/wam-private-test/releases'/SHA
UNIT='wizard-app-manager-api.service'
GW='private-gateway-https.service'
CONTROL={'api_dropin':H/'.config/systemd/user/wizard-app-manager-api.service.d/connection-fork-registry.conf','runtime_pins':H/'.local/share/wam-private-test/connection-forks/wam-runtime-pins.json','wrapper':H/'.local/bin/wam','caddy':H/'.config/private-gateway/Caddyfile','route_map':H/'worktrees/private-portfolio/private-routes.json','route_adapter':H/'worktrees/private-portfolio/private-route-adapter.mjs','box_source':H/'.local/share/wizard-ux/current/web/core/box-source.js','snippet':R/'about-compare.Caddyfile'}
def sh(argv): return subprocess.check_output(argv,text=True,timeout=60).strip()
def sha(b): return hashlib.sha256(b).hexdigest()
def val(u,k): return sh(['systemctl','--user','show',u,'-p',k,'--value'])
def save(name,d): (R/name).write_text(json.dumps(d,indent=2)+'\n')
def atomic(p,b,mode):
 t=p.with_name(p.name+'.about-'+str(os.getpid()))
 with t.open('xb') as f: f.write(b); f.flush(); os.fsync(f.fileno())
 os.chmod(t,mode); os.replace(t,p)
def apps():
 with urllib.request.urlopen('http://127.0.0.1:7790/api/apps',timeout=25) as r:
  return sorted([{k:a.get(k,'') for k in ['id','status','version','source']} for a in json.load(r)['apps']],key=lambda a:a['id'])
def runtimes():
 names=['runtipi','messenger_wizardapps-messenger-1','notifications_wizardapps-notifications-1']
 return {n:sh(['docker','inspect','--format','{{.Id}}|{{.Image}}|{{.State.StartedAt}}|{{.State.Status}}',n]) for n in names}
def manifest(commit):
 data=subprocess.check_output(['git','-C',str(REPO),'archive',commit])
 with tarfile.open(fileobj=io.BytesIO(data)) as tar:
  return {m.name:sha(tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()}
def check_source(path,files):
 for n,h in files.items(): assert sha((path/n).read_bytes())==h, 'source drift '+n

def freeze():
 assert not (R/'live-freeze.json').exists(), 'freeze exists: do not overwrite original custody'
 assert val(UNIT,'WorkingDirectory')==str(SOURCE.parent/OLD)
 assert Path('/proc',val(UNIT,'MainPID'),'cwd').resolve()==SOURCE.parent/OLD
 assert sh(['git','-C',str(REPO),'rev-parse','HEAD'])==SHA
 assert not sh(['git','-C',str(REPO),'status','--porcelain'])
 assert sh(['git','-C',str(REPO),'ls-remote','origin','refs/heads/fix/about-readonly-gateway-20261105']).split()[0]==SHA
 receipt=json.loads((R/'fixture-results/receipt.json').read_text())
 assert receipt['accepted'] and receipt['candidate']==SHA and receipt['same_pid_reload'] and receipt['all_fixture_processes_stopped'] and len(receipt['checks'])==receipt['check_count']==41
 assert sha((R/'fixture-results/caddy.log').read_bytes())==receipt['caddy_log_sha256']
 assert CONTROL['caddy'].read_bytes()==(R/'Caddyfile.before').read_bytes()
 oldfiles=manifest(OLD); newfiles=manifest(SHA)
 check_source(SOURCE.parent/OLD,oldfiles)
 if not SOURCE.exists():
  SOURCE.mkdir(mode=0o755)
  data=subprocess.check_output(['git','-C',str(REPO),'archive',SHA])
  tarfile.open(fileobj=io.BytesIO(data)).extractall(SOURCE,filter='data')
 check_source(SOURCE,newfiles)
 # The only source modification in the runtime is the tested ten-line additive API route.
 changed=[n for n in oldfiles if oldfiles[n]!=newfiles.get(n)]
 assert changed==['wam/cli.py'],changed
 assert newfiles['wam/main_compare.py']==oldfiles['wam/main_compare.py']
 d={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'baseline':OLD,'candidate':SHA,'old_source':str(SOURCE.parent/OLD),'source':str(SOURCE),'old_files':oldfiles,'new_files':newfiles,'api_pid':int(val(UNIT,'MainPID')),'gateway_pid':int(val(GW,'MainPID')),'gateway_exec':val(GW,'ExecStart'),'gateway_cmdline':Path('/proc',val(GW,'MainPID'),'cmdline').read_bytes().decode().split('\0'),'controls':{},'apps':apps(),'runtimes':runtimes()}
 assert len(d['apps'])==29 and next(a for a in d['apps'] if a['id']=='wizard-recorder')['status']=='stopped'
 (R/'live-rollback').mkdir(mode=0o700)
 for name,p in CONTROL.items():
  b=p.read_bytes(); (R/'live-rollback'/name).write_bytes(b)
  d['controls'][name]={'path':str(p),'sha256':sha(b),'mode':p.stat().st_mode&0o777}
 # Freeze import dependencies without expanding any environment/file proof placeholders.
 import glob,re
 deps={}
 for token in re.findall(r'^\s*import\s+([^\s]+)',CONTROL['caddy'].read_text(),re.M):
  if token.startswith('/'):
   for n in glob.glob(token):
    p=Path(n)
    if p.is_file(): deps[n]=sha(p.read_bytes())
 d['caddy_import_dependencies']=deps
 save('live-freeze.json',d)
 print(json.dumps({'frozen':True,'candidate':SHA,'runtime_only_changed':['wam/cli.py'],'apps':len(d['apps']),'gateway_pid':d['gateway_pid']}))

def outputs(d):
 before={n:(R/'live-rollback'/n).read_bytes() for n in CONTROL}
 for n,b in before.items(): assert sha(b)==d['controls'][n]['sha256']
 old=d['old_source'].encode(); new=str(SOURCE).encode()
 assert before['api_dropin'].count(old)==1
 out={'api_dropin':before['api_dropin'].replace(old,new)}
 pins=json.loads(before['runtime_pins']); pins['api_source']=str(SOURCE)
 # Nested effective pointers are truthful; historical unrelated fields remain untouched.
 pins['api']={'sha':SHA,'path':str(SOURCE)}
 pins['cli_source']=d['old_source']; pins['cli']={'sha':OLD,'path':d['old_source']}
 out['runtime_pins']=(json.dumps(pins,indent=2)+'\n').encode()
 out['caddy']=(R/'Caddyfile.candidate').read_bytes()
 line=('    import '+str(CONTROL['snippet'])+'\n').encode()
 assert out['caddy'].count(line)==1 and out['caddy'].replace(line,b'',1)==before['caddy']
 return before,out

def api_reload(expected):
 subprocess.run(['systemctl','--user','daemon-reload'],check=True)
 subprocess.run(['systemctl','--user','restart',UNIT],check=True)
 for _ in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:7790/healthz',timeout=2) as r: assert json.load(r)['ok']
   assert val(UNIT,'WorkingDirectory')==expected
   assert str(Path('/proc',val(UNIT,'MainPID'),'cwd').resolve())==expected
   return
  except Exception: time.sleep(.3)
 raise RuntimeError('API health/source verification failed')

def gateway_reload(d,label,expected):
 # Read active unit, process argv, PID and exact on-disk config immediately before SIGUSR1.
 assert val(GW,'ExecStart')==d['gateway_exec']
 assert val(GW,'MainPID')==str(d['gateway_pid'])
 assert Path('/proc',str(d['gateway_pid']),'cmdline').read_bytes().decode().split('\0')==d['gateway_cmdline']
 assert CONTROL['caddy'].read_bytes()==expected
 for p,h in d['caddy_import_dependencies'].items(): assert sha(Path(p).read_bytes())==h,'import drift '+p
 since=str(time.time())
 os.kill(d['gateway_pid'],signal.SIGUSR1)
 positive=[]
 for _ in range(30):
  assert val(GW,'MainPID')==str(d['gateway_pid']),'gateway PID changed'
  raw=sh(['journalctl','--user','-u',GW,'--since','@'+since,'--no-pager','-o','cat'])
  # Persist only reload event names, no journal bodies, remote headers or secrets.
  for line in raw.splitlines():
   try:
    e=json.loads(line); msg=e.get('msg','')
    if msg in ['config is unchanged','config reloaded','Reload succeeded','reload complete','reloaded config','successfully loaded new config'] or ('reload' in msg.lower() and 'success' in msg.lower()): positive.append({'msg':msg,'logger':e.get('logger'),'level':e.get('level'),'ts':e.get('ts')})
   except ValueError: pass
  if positive: break
  time.sleep(.2)
 # A positive running-server reload event, not just signal receipt, is required.
 save('gateway-'+label+'-reload.json',{'pid_before':d['gateway_pid'],'pid_after':int(val(GW,'MainPID')),'same_pid':True,'config_sha256':sha(expected),'positive_reload_events':positive,'journal_window_sha256':sha(raw.encode())})
 assert positive,'No positive Caddy reload journal event'

def baseline(d):
 assert (R/'live-parity.log').read_text().startswith('PASS:')
 manifest_inputs=json.loads((R/'manifest.json').read_text())['inputs']
 assert sha(Path('/home/telos/.local/bin/caddy-private-gateway').read_bytes())==manifest_inputs['caddy-private-gateway']
 assert sha(CONTROL['snippet'].read_bytes())==manifest_inputs['about-compare.Caddyfile']
 assert val(UNIT,'WorkingDirectory')==d['old_source']
 for n,p in CONTROL.items(): assert sha(p.read_bytes())==d['controls'][n]['sha256'],n+' drift'
 for p,h in d['caddy_import_dependencies'].items(): assert sha(Path(p).read_bytes())==h,p+' drift'
 check_source(Path(d['old_source']),d['old_files']);check_source(SOURCE,d['new_files'])
 assert apps()==d['apps'] and runtimes()==d['runtimes']
 assert val(GW,'MainPID')==str(d['gateway_pid'])

def revert(d,before,out):
 # CAS each task-owned control; do not undo a sibling change.
 for n in out: assert CONTROL[n].read_bytes() in [before[n],out[n]],'rollback CAS conflict '+n
 gwchanged=CONTROL['caddy'].read_bytes()==out['caddy']
 for n in out: atomic(CONTROL[n],before[n],d['controls'][n]['mode'])
 if gwchanged: gateway_reload(d,'rollback',before['caddy'])
 api_reload(d['old_source'])
 baseline(d)
 save('rollback-result.json',{'restored_exact_pretask_controls':True,'gateway_same_pid':True,'api_source':d['old_source'],'apps_and_runtimes_unchanged':True})

def apply():
 d=json.loads((R/'live-freeze.json').read_text());before,out=outputs(d)
 lock=H/'.local/state/wizard-app-promotions/wam.lock'
 lock.parent.mkdir(parents=True,exist_ok=True)
 with lock.open('a') as f:
  fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  baseline(d)
  try:
   for n in ['api_dropin','runtime_pins']: atomic(CONTROL[n],out[n],d['controls'][n]['mode'])
   api_reload(str(SOURCE))
   assert apps()==d['apps'] and runtimes()==d['runtimes']
   # Re-CAS all untouched controls before gateway activation.
   for n,p in CONTROL.items(): assert p.read_bytes()==(out[n] if n in ['api_dropin','runtime_pins'] else before[n]),n+' changed'
   # Current Caddy bytes are baseline until this exact atomic replacement.
   atomic(CONTROL['caddy'],out['caddy'],d['controls']['caddy']['mode'])
   gateway_reload(d,'activation',out['caddy'])
   subprocess.run([sys.executable,str(R/'verify_about.py')],check=True,timeout=240)
   save('activation-result.json',{'accepted':True,'candidate':SHA,'api_source':str(SOURCE),'cli_source':d['old_source'],'gateway_pid':int(val(GW,'MainPID')),'gateway_same_pid':True,'control_hashes':{n:sha(p.read_bytes()) for n,p in CONTROL.items()},'verification':'live-verification.json','rollback_command':'python3 '+str(R/'activate_about.py')+' --rollback'})
   print(json.dumps(json.loads((R/'activation-result.json').read_text())))
  except BaseException as e:
   save('activation-failure.json',{'error_type':type(e).__name__,'error':str(e)})
   revert(d,before,out)
   raise
if __name__=='__main__':
 if sys.argv[1:]==['--freeze']: freeze()
 elif sys.argv[1:]==['--apply']: apply()
 elif sys.argv[1:]==['--rollback']:
  d=json.loads((R/'live-freeze.json').read_text());b,o=outputs(d);revert(d,b,o)
 else: raise SystemExit('use --freeze, --apply or --rollback')
