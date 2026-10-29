#!/usr/bin/env python3
"""Slice 6 guest assertions. Synthetic data in a disposable guest ONLY."""
import hashlib
import json
import os
import pwd
from pathlib import Path
import subprocess
import sys
import time

OLD = Path('/home/hermes')
BASE = Path('/var/lib/wizard-os')
CONTROL = Path('/var/lib/wizard-os-migration')
SELECTION = Path('/root/slice6-apps.json')
HELPER = '/usr/local/libexec/wizard-os/server-access'


def select_apps(catalog):
    """Choose only apps observed in this guest's actual index, not remembered IDs."""
    available = {row['id'] for row in catalog}
    # Calendar is the proven data/host-mount fixture; the other lightweight apps
    # may vary with the public catalog. Fail before installing if coverage lacks.
    preferred = ('wizard-calendar', 'wizard-shopping', 'wizard-notes',
                 'wizard-reference', 'wizard-stories', 'wizard-wisdom', 'wizard-profiles',
                 # Public-index ids observed on a clean guest (28 Oct 2026).
                 'profiles', 'wisdom', 'stories', 'notes', 'reference')
    apps = [app for app in preferred if app in available][:3]
    assert 'wizard-calendar' in apps and len(apps) == 3, sorted(available)
    return apps


def select_candidates(catalog):
    """Every preferred indexed app, Calendar first, for install-with-fallback."""
    select_apps(catalog)  # same coverage precondition
    available = {row['id'] for row in catalog}
    preferred = ('wizard-calendar', 'wizard-shopping', 'wizard-notes', 'wizard-reference', 'wizard-stories',
                 'wizard-wisdom', 'wizard-profiles', 'profiles', 'wisdom', 'stories', 'notes', 'reference',
                 'morning-report', 'money')
    return [app for app in preferred if app in available]


def run(argv, *, expected=0, **kwargs):
    p = subprocess.run([str(x) for x in argv], check=False, capture_output=True,text=True,timeout=1200,**kwargs)
    # Never print bearer values or copied integration files.
    print(json.dumps({'argv': [str(x) for x in argv], 'exit':p.returncode}),flush=True)
    if expected is None: assert p.returncode != 0,p.stderr
    else: assert p.returncode == expected,p.stderr[-4000:]
    return p.stdout


def agent(*args, **kwargs):
    uid = pwd.getpwnam('hermes').pw_uid
    # A root SSH session can export its own DBus address. Never reuse it after
    # runuser: the rollback check must query the selected user's actual manager.
    return run(['runuser','-u','hermes','--','env','-i','HOME=/home/hermes',
                f'XDG_RUNTIME_DIR=/run/user/{uid}',
                f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',
                'PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin',*args],**kwargs)


def original_operator():
    expected = json.loads((CONTROL/'journal.json').read_text())['operator']
    actual = json.loads(run(['tailscale','debug','prefs'])).get('OperatorUser') or ''
    assert actual == expected, (actual, expected)


def app_lifecycle(verb, app):
    """Scope lifecycle to an app on both main and the separated thin client.

    Main's top-level `wam start/stop/restart` controls Runtipi itself; an extra
    app argument does not make it an app action and may still exit zero.
    """
    assert verb in ('start', 'stop', 'restart'), verb
    result = json.loads(agent('wam', 'app', verb, app))
    expected = 'stopped' if verb == 'stop' else 'running'
    assert result.get('id') == app and result.get('status') == expected and not result.get('error'), result
    return result


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


def host_canary():
    cid = run(['docker', 'ps', '-q', '--filter',
               'label=com.docker.compose.service=wizard-calendar']).strip()
    assert cid and len(cid.split()) == 1
    mounts = json.loads(run(['docker', 'inspect', '--format', '{{json .Mounts}}', cid]))
    assert any(m['Source'] == '/srv/slice6-host' and m['Destination'] == '/preexisting'
               and not m['RW'] for m in mounts), mounts
    assert run(['docker', 'exec', cid, 'cat', '/preexisting/canary']) == 'grandfathered host access\n'


def wait_service():
    import urllib.request
    st=json.loads((BASE/'.wizard-os/alongside.json').read_text())
    port=int((st.get('service') or {}).get('port') or 8923)
    assert 1 <= port <= 65535, port
    origin=f'http://127.0.0.1:{port}'
    end=time.monotonic()+180
    while time.monotonic()<end:
        try:
            value=json.load(urllib.request.urlopen(origin+'/wizard/health',timeout=10))
            if value.get('status')=='ok' and value.get('upstream_reachable'): break
        except Exception: pass
        time.sleep(2)
    else: raise AssertionError('Agent Service did not become healthy')
    cid=run(['docker','ps','-q','--filter','label=com.docker.compose.service=wizard-agent-service']).strip()
    assert cid
    assert run(['docker','exec',cid,'id','-u']).strip()==run(['id','-u','hermes']).strip()


def main(argv):
    global APPS
    phase, sha = argv
    assert os.getuid() == 0
    if phase == "legacy":
        catalog = json.loads(agent("wam", "search", "--json"))
        # Legacy setup runs main's wam: a store app main cannot install is not
        # this branch's concern, so try the next indexed candidate instead.
        candidates = select_candidates(catalog)
        APPS, skipped = [], {}
        for app in candidates:
            if len(APPS) == 3: break
            try:
                agent('wam', 'install', app)
                APPS.append(app)
            except AssertionError as e:
                if app == 'wizard-calendar': raise
                skipped[app] = str(e).strip()[-300:]
        assert 'wizard-calendar' in APPS and len(APPS) == 3, (APPS, skipped)
        SELECTION.write_text(json.dumps(APPS))
        print(json.dumps({"catalog_ids": sorted({row["id"] for row in catalog}), "selected_apps": APPS,
                          "skipped_main_install_failures": skipped}), flush=True)
    else:
        APPS = json.loads(SELECTION.read_text())
    assert len(APPS) >= 3 and len(set(APPS)) == len(APPS)
    if phase=='legacy':
        Path('/srv/slice6-host').mkdir(exist_ok=True)
        Path('/srv/slice6-host/canary').write_text('grandfathered host access\n')
        p=OLD/'runtipi/user-config/wizardapps/wizard-calendar/docker-compose.yml'
        # Merge an actual already-running store app's override, not a fake recipe.
        sys.path.insert(0,str(OLD/'wizard-app-manager'))
        from wam.network import load_yaml,dump_yaml
        doc=load_yaml(p.read_text()) if p.exists() else {}
        doc=doc or {}; doc.setdefault('services',{}).setdefault('wizard-calendar',{}).setdefault('volumes',[]).append('/srv/slice6-host:/preexisting:ro')
        p.parent.mkdir(parents=True,exist_ok=True); p.write_text(dump_yaml(doc))
        run(['chown','-R','hermes:hermes',p.parent])
        app_lifecycle('stop', 'wizard-calendar'); app_lifecycle('start', 'wizard-calendar')
        host_canary()
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
    elif phase=='checkpoint':
        # Frozen full-tree hashes, before applications have a chance to update their
        # own databases or the manager regenerates its derived registry projection.
        before=hashes(CONTROL/'snapshot/runtipi/app-data')
        after=hashes(BASE/'runtipi/app-data')
        assert before and before==after
        Path('/root/slice6-all-data-hashes.json').write_text(json.dumps(before,sort_keys=True))
        canaries(BASE)
        print(json.dumps({'all_app_data_files_identical_at_relocation':len(before)}))
    elif phase=='fail-before-recover':
        # Use real production steps up to and including protected-unit preparation.
        # Replace only start() once; rollback must recover this interrupted layout.
        sys.path.insert(0,str(BASE/'wizard-app-manager'))
        from wam.privilege_migration import Migration
        migration=Migration()
        def fail(): raise RuntimeError('slice6 injected pre-recover failure')
        migration.start=fail
        try: migration.migrate()
        except RuntimeError as e: assert str(e)=='slice6 injected pre-recover failure'
        else: raise AssertionError('failure was not injected')
        journal=json.loads((CONTROL/'journal.json').read_text())
        assert 'prepare' in journal['done'] and 'start' not in journal['done']
        print('PASS injected real mid-migration failure before recover')
    elif phase in ('migrated','again'):
        saved=json.loads(Path('/root/slice6-before.json').read_text())
        assert not set(run(['id','-nG','hermes']).split()) & {'docker','sudo','wheel','admin'}
        # Audit A3: no automatic migration sudo, immediately and after resume/remigration.
        access=json.loads(run([HELPER,'status']))
        assert not access['active'],('migration must not grant server access',access)
        assert not Path('/etc/sudoers.d/91-wizard-agent-access').exists()
        run([HELPER,'grant'],input='{"seconds":60,"migration":true}',expected=None)
        assert not json.loads(run([HELPER,'status']))['active'],'helper must refuse a migration grant'
        agent('sudo','-n','true',expected=None)
        agent('docker','ps',expected=None)
        agent('tailscale','set','--accept-dns=false',expected=None)
        agent('wam','apps','--json')
        for verb in ('stop', 'start', 'restart'): app_lifecycle(verb, APPS[1])
        rows=json.loads(agent('wam','doctor','--json'))
        assert rows and all(row['ok'] for row in rows),rows
        wait_service(); canaries(BASE); host_canary()
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
    elif phase=='rollback':
        saved=json.loads(Path('/root/slice6-before.json').read_text())
        assert set(run(['id','-nG','hermes']).split())==set(saved['groups'])
        assert hashlib.sha256((OLD/'.wizard-os/api-token').read_bytes()).hexdigest()==saved['api_token_hash']
        original_operator()
        assert agent('systemctl','--user','is-active','wizard-app-manager-api.service').strip()=='active'
        canaries(OLD); host_canary()
        expected=json.loads(Path('/root/slice6-all-data-hashes.json').read_text())
        # Every byte in the immutable snapshot is the same as at first relocation.
        assert hashes(CONTROL/'snapshot/runtipi/app-data')==expected
        agent('wam','apps','--json')
        for app in APPS:
            assert run(['docker','ps','-q','--filter','label=runtipi.appurn='+app+':wizardapps']).strip()
        print('PASS original layout, groups, user unit, token, operator and running app data restored')
    else: raise AssertionError('unknown phase')
    print(json.dumps({'phase':phase,'passed':True}))


if __name__ == "__main__":
    main(sys.argv[1:])
