#!/usr/bin/env python3
"""Slice 6 guest assertions. Synthetic data in a disposable guest ONLY."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

OLD = Path('/home/hermes')
BASE = Path('/var/lib/wizard-os')
CONTROL = Path('/var/lib/wizard-os-migration')
APPS = ('wizard-calendar','wizard-quest','wizard-shopping')
HELPER = '/usr/local/libexec/wizard-os/server-access'
PHASE, SHA = sys.argv[1:3]
assert os.getuid() == 0


def run(argv, *, expected=0, **kwargs):
    p = subprocess.run([str(x) for x in argv], check=False, capture_output=True,text=True,timeout=1200,**kwargs)
    # Never print bearer values or copied integration files.
    print(json.dumps({'argv': [str(x) for x in argv], 'exit':p.returncode}),flush=True)
    if expected is None: assert p.returncode != 0,p.stderr
    else: assert p.returncode == expected,p.stderr[-4000:]
    return p.stdout


def agent(*args, **kwargs):
    return run(['runuser','-u','hermes','--','env','HOME=/home/hermes',
                'PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin',*args],**kwargs)


def hashes(root):
    out={}
    for directory, dirs, files in os.walk(root,followlinks=False):
        for name in sorted(files):
            p=Path(directory)/name
            if p.is_symlink(): out[str(p.relative_to(root))]={'link':os.readlink(p)}
            else:
                st=p.stat()
                out[str(p.relative_to(root))]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'uid':st.st_uid,'gid':st.st_gid,'mode':st.st_mode & 0o7777}
    return out


def canaries(base):
    for app in APPS:
        p=base/'runtipi/app-data/wizardapps'/app/'data/slice6-canary'
        assert p.read_bytes()==('slice6:'+app+'\n').encode(),app
        assert p.stat().st_uid==1000,app


def wait_service():
    import urllib.request
    end=time.monotonic()+180
    while time.monotonic()<end:
        try:
            value=json.load(urllib.request.urlopen('http://127.0.0.1:8923/wizard/health',timeout=10))
            if value.get('status')=='ok' and value.get('upstream_reachable'): break
        except Exception: pass
        time.sleep(2)
    else: raise AssertionError('Agent Service did not become healthy')
    cid=run(['docker','ps','-q','--filter','label=com.docker.compose.service=wizard-agent-service']).strip()
    assert cid
    assert run(['docker','exec',cid,'id','-u']).strip()==run(['id','-u','hermes']).strip()


if PHASE=='legacy':
    for app in APPS: agent('wam','install',app)
    Path('/srv/slice6-host').mkdir(exist_ok=True)
    Path('/srv/slice6-host/canary').write_text('grandfathered host access\n')
    p=OLD/'runtipi/user-config/wizardapps/wizard-calendar/docker-compose.yml'
    # Merge an actual already-running store app's override, not a fake recipe.
    from importlib.machinery import SourceFileLoader
    sys.path.insert(0,str(OLD/'wizard-app-manager'))
    from wam.network import load_yaml,dump_yaml
    doc=load_yaml(p.read_text()) if p.exists() else {}
    doc=doc or {}; doc.setdefault('services',{}).setdefault('wizard-calendar',{}).setdefault('volumes',[]).append('/srv/slice6-host:/preexisting:ro')
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(dump_yaml(doc))
    run(['chown','-R','hermes:hermes',p.parent])
    agent('wam','stop','wizard-calendar'); agent('wam','start','wizard-calendar')
    for app in APPS:
        p=OLD/'runtipi/app-data/wizardapps'/app/'data/slice6-canary'
        p.parent.mkdir(parents=True,exist_ok=True); p.write_text('slice6:'+app+'\n'); os.chown(p,1000,1000)
    # A real daemon with no tailnet login; no fake Tailscale implementation.
    run(['tailscale','set','--operator=hermes'])
    canaries(OLD)
    saved={'groups':run(['id','-nG','hermes']).split(),
           'api_token_hash':hashlib.sha256((OLD/'.wizard-os/api-token').read_bytes()).hexdigest()}
    Path('/root/slice6-before.json').write_text(json.dumps(saved))
    assert 'docker' in saved['groups']
    agent('sudo','-n','true',expected=None)
    print('PASS real legacy install, store apps, host mount and UID-1000 data')
elif PHASE=='checkpoint':
    # Frozen full-tree hashes, before applications have a chance to update their
    # own databases or the manager regenerates its derived registry projection.
    before=hashes(CONTROL/'snapshot/runtipi/app-data')
    after=hashes(BASE/'runtipi/app-data')
    assert before and before==after
    Path('/root/slice6-all-data-hashes.json').write_text(json.dumps(before,sort_keys=True))
    canaries(BASE)
    print(json.dumps({'all_app_data_files_identical_at_relocation':len(before)}))
elif PHASE in ('migrated','again'):
    saved=json.loads(Path('/root/slice6-before.json').read_text())
    assert not set(run(['id','-nG','hermes']).split()) & {'docker','sudo','wheel','admin'}
    if PHASE=='migrated':
        access=json.loads(run([HELPER,'status']))
        assert access['active'] and access['migration'] and access['severity']=='red'
        grant=json.loads(Path('/var/lib/wizard-os-server-access/grant.json').read_text())
        assert 604700 < grant['expires_epoch']-time.time() <=604800
        assert run(['systemctl','is-active','wizard-agent-access-expiry.timer']).strip()=='active'
        agent('sudo','-n','true')
        assert 'COMMAND=' in Path('/var/log/wizard-os-agent-sudo.log').read_text()
        run([HELPER,'revoke'])
    else:
        assert not json.loads(run([HELPER,'status']))['active'],'rollback must not renew the migration grant'
    agent('sudo','-n','true',expected=None)
    agent('docker','ps',expected=None)
    agent('tailscale','set','--accept-dns=false',expected=None)
    agent('wam','apps','--json')
    for verb in ('stop','start','restart'): agent('wam',verb,'wizard-quest')
    rows=json.loads(agent('wam','doctor','--json'))
    assert rows and all(row['ok'] for row in rows),rows
    wait_service(); canaries(BASE)
    approvals=json.loads((BASE/'.wizard-os/host-approvals.json').read_text()) if (BASE/'.wizard-os/host-approvals.json').exists() else None
    if approvals is None:
        sys.path.insert(0,str(BASE/'wizard-app-manager'))
        from wam.host_policy import _read
        approvals=_read(BASE/'.wizard-os')
    assert any('wizard-calendar' in k and v['source']=='grandfathered at upgrade' and v['risks'] for k,v in approvals['approvals'].items())
    assert hashlib.sha256((BASE/'.wizard-os/api-token').read_bytes()).hexdigest()!=saved['api_token_hash']
    assert json.loads(run(['tailscale','debug','prefs']))['OperatorUser']=='wizard-system'
    Path('/root/slice6-doctor.json').write_text(json.dumps(rows))
    print('PASS separated lifecycle, service UID, tokens, approvals, kernel denials and doctor')
elif PHASE=='rollback':
    saved=json.loads(Path('/root/slice6-before.json').read_text())
    assert set(run(['id','-nG','hermes']).split())==set(saved['groups'])
    assert hashlib.sha256((OLD/'.wizard-os/api-token').read_bytes()).hexdigest()==saved['api_token_hash']
    assert json.loads(run(['tailscale','debug','prefs']))['OperatorUser']=='hermes'
    assert agent('systemctl','--user','is-active','wizard-app-manager-api.service').strip()=='active'
    canaries(OLD)
    expected=json.loads(Path('/root/slice6-all-data-hashes.json').read_text())
    # Every byte in the immutable snapshot is the same as at first relocation.
    assert hashes(CONTROL/'snapshot/runtipi/app-data')==expected
    agent('wam','apps','--json')
    for app in APPS:
        assert run(['docker','ps','-q','--filter','label=runtipi.appurn='+app+':wizardapps']).strip()
    print('PASS original layout, groups, user unit, token, operator and running app data restored')
else: raise AssertionError('unknown phase')
print(json.dumps({'phase':PHASE,'passed':True}))
