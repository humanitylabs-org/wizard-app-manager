#!/usr/bin/python3
"""Disposable guest only: real stock apps; no mocked policy, identity or rotation.

All app data in these VMs is synthetic. Never run this on an owner's box.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

SOURCE = Path('/opt/privsep-source')
STATE = Path('/var/lib/privsep-compat-qa')
PHASE = 'entry'
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE))
from wam.runtipi import Runtipi, cli_token
from wam.credential_rotation import Rotation


def save(name, value):
    p = STATE / name
    p.write_text(json.dumps(value, indent=2) + '\n')
    p.chmod(0o600)


def event(kind, **fields):
    print(json.dumps({'event': kind, 'phase': PHASE, **fields}), flush=True)


def run(operation, argv, *, data=None, timeout=900, ok=(0,)):
    r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    event('command', operation=operation, returncode=r.returncode)
    if r.returncode not in ok:
        # Fixed signatures only, not raw stderr, argv, environment or tokens.
        combined = r.stdout + r.stderr
        signals = {'host_guard_refusal': b'Wizard host policy',
                   'unsafe_ancestor': b'writable ancestor',
                   'session_bus_missing': b'Failed to connect to bus',
                   'permission_denied': b'Permission denied'}
        event('command-failure', operation=operation,
              signatures=[k for k, v in signals.items() if v in combined])
        raise RuntimeError('Guest operation failed')
    return r


def user(name, operation, *argv, timeout=900):
    p = pwd.getpwnam(name)
    env = {'HOME': p.pw_dir, 'USER': name, 'LOGNAME': name,
           'PATH': p.pw_dir + '/.local/bin:/usr/local/bin:/usr/bin:/bin',
           'XDG_RUNTIME_DIR': '/run/user/' + str(p.pw_uid),
           'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(p.pw_uid) + '/bus'}
    # Owner tools use their normal umask, not the private evidence umask.
    return run(operation, ['runuser', '-u', name, '--', 'env', '-i',
                           *[k + '=' + v for k, v in env.items()],
                           'sh', '-c', 'umask 022; exec "$0" "$@"', *argv], timeout=timeout)


def layout(scenario):
    if scenario == 'fresh': return Path('/home/wizard/runtipi'), 'wizard'
    if scenario == 'legacy': return Path('/home/hermes/runtipi'), 'hermes'
    return Path('/var/lib/wizard-os/runtipi'), 'hermes'


def rt(root):
    return Runtipi.from_install(root)


def installed(root):
    return {x['appInfo']['id']: {'status': x['status'], 'port': x['appInfo']['port']}
            for x in rt(root).installed()}


def wait(root, app, want):
    result = rt(root).wait_status(app + ':wizardapps', {want, 'error'}, timeout=180)
    assert result == want, 'App lifecycle did not reach requested state'


def http(root, app, path, body=None):
    port = int(installed(root)[app]['port'])
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request('http://127.0.0.1:' + str(port) + path,
                                     data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        assert response.status == 200
        raw = response.read()
    return json.loads(raw) if path.startswith('/api/') else raw


def snapshot(path):
    s = path.stat()
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'uid': s.st_uid,
            'gid': s.st_gid, 'mode': s.st_mode & 0o7777}


def seed_and_exercise(scenario, config):
    global PHASE
    root, owner = layout(scenario)
    PHASE = scenario + '-catalog-and-install'
    from wam import index
    for app, pin in config['ordinary_apps'].items():
        rec = index.resolve(index.DEFAULT_INDEX, pin['repo'])
        assert rec['id'] == app and rec['install']['commit'] == pin['commit'], 'Catalog drift: restage explicitly'
        user(owner, 'install-' + app, 'wam', 'install', pin['repo'], timeout=900)
        wait(root, app, 'running')
    PHASE = scenario + '-ordinary-lifecycle'
    for app in config['ordinary_apps']:
        # Never use `wam stop`, which historically stops the whole stack.
        user(owner, 'stop-' + app, 'wam', 'app', 'stop', app)
        wait(root, app, 'stopped')
        user(owner, 'start-' + app, 'wam', 'app', 'start', app)
        wait(root, app, 'running')
    PHASE = scenario + '-calendar-import'
    http(root, 'wizard-calendar', '/api/events')
    # API usability, not just Docker running state. All fixture values are synthetic.
    fixture = {'schemaVersion': 1, 'timezone': 'UTC',
               'calendars': [{'id': 'compat', 'name': 'Synthetic compatibility'}],
               'events': [{'id': 'compat:one', 'cal': 'compat', 'title': 'Synthetic compatibility event',
                           'start': '2026-10-15T10:00:00Z', 'end': '2026-10-15T11:00:00Z', 'allDay': False}]}
    http(root, 'wizard-calendar', '/api/import', fixture)
    exported = http(root, 'wizard-calendar', '/api/export')
    assert any(x.get('id') == 'compat:one' for x in exported['events'])
    save(scenario + '-calendar.json', exported)
    PHASE = scenario + '-files-and-data'
    assert http(root, 'files', '/'), 'Files UI returned no content'
    files = root / 'media/privsep-compat-canary.txt'
    files.write_text('Synthetic owner-file compatibility canary\n')
    os.chown(files, 1000, 1000); files.chmod(0o640)
    calendar = root / 'app-data/wizardapps/wizard-calendar/data/privsep-compat-canary.txt'
    assert calendar.parent.is_dir()
    calendar.write_text('Synthetic Calendar payload canary\n'); calendar.chmod(0o640)
    save(scenario + '-canaries.json', {
        'media/privsep-compat-canary.txt': snapshot(files),
        'app-data/wizardapps/wizard-calendar/data/privsep-compat-canary.txt': snapshot(calendar)})
    save(scenario + '-running.json', installed(root))
    if scenario == 'legacy':
        raw = user('hermes', 'stock-hermes-revision', 'git', '-C', '/home/hermes/hermes-agent', 'rev-parse', 'HEAD').stdout.decode().strip()
        assert raw == config['hermes_commit']
        save('legacy-hermes.json', {'revision': raw})
    event('apps-exercised', apps=list(config['ordinary_apps']), calendar_import=True, files_http=True,
          app_scoped_stop_start=True, synthetic_data_only=True)


def verify_data(scenario):
    root, _ = layout(scenario)
    original = 'fresh' if scenario == 'fresh' else 'legacy'
    before = json.loads((STATE / (original + '-canaries.json')).read_text())
    for app, row in json.loads((STATE / (original + '-running.json')).read_text()).items():
        if row['status'] == 'running': wait(root, app, 'running')
    assert {name: snapshot(root / name) for name in before} == before, 'Synthetic payload metadata changed'
    exported = http(root, 'wizard-calendar', '/api/export')
    assert exported == json.loads((STATE / (original + '-calendar.json')).read_text()), 'Calendar export changed'
    assert http(root, 'files', '/')
    expected = json.loads((STATE / (original + '-running.json')).read_text())
    actual = installed(root)
    assert all(app in actual and actual[app]['status'] == row['status'] for app, row in expected.items())
    event('data-preserved', bytes_uid_gid_mode=True, calendar_export=True,
          files_http=True, all_previously_running_apps=True)


def stopped_copy():
    # All files here were created by this disposable VM, no sealed or personal data.
    before = Path('/var/lib/wizard-os-migration/snapshot/runtipi/app-data')
    after = Path('/var/lib/wizard-os/runtipi/app-data')
    def tree(root):
        out = {}
        for p in sorted(root.rglob('*')):
            s = p.lstat()
            meta = [s.st_uid, s.st_gid, s.st_mode & 0o7777]
            if p.is_symlink(): meta += ['symlink', os.readlink(p)]
            elif p.is_dir(): meta += ['directory']
            elif p.is_file(): meta += ['file', hashlib.sha256(p.read_bytes()).hexdigest()]
            else: meta += ['other']
            out[str(p.relative_to(root))] = meta
        return out
    a, b = tree(before), tree(after)
    assert a and a == b, 'Stopped synthetic app-data copy differs'
    event('stopped-relocation-copy', entries=len(a), bytes_uid_gid_mode=True)


def credentials():
    rotation = Rotation()
    rotation.check_paths(); doc = rotation.load()
    assert doc['phase'] == 'verified'
    _, _, old, new = rotation.values()
    rotation.io.preflight(new, authenticate=False); rotation.io.discover()
    rotation.io.verify(old, new)
    assert rotation.io.bearer(old['JWT_SECRET']) == 401
    assert rotation.io.bearer(new['JWT_SECRET']) == 200
    event('credential-readback', phase_verified=True, old_bearer=401, new_bearer=200,
          old_database_password='rejected', old_queue_password='rejected')



def denied_identity(config):
    import grp
    p = pwd.getpwnam('hermes')
    groups = {grp.getgrgid(g).gr_name for g in os.getgrouplist('hermes', p.pw_gid)}
    assert not groups & {'docker', 'sudo', 'wheel', 'admin'}
    for op, command in [('docker-denied', ['docker', 'ps']), ('sudo-denied', ['sudo', '-n', 'true'])]:
        # These must fail as the real account, not as a mocked UID.
        r = run(op, ['runuser', '-u', 'hermes', '--', *command], timeout=15, ok=(0, 1, 2, 125, 126))
        assert r.returncode != 0
    grant = run('server-access-status', ['/usr/local/libexec/wizard-os/server-access', 'status'], timeout=15)
    assert not json.loads(grant.stdout).get('active')
    for target in ('/var/lib/wizard-os/runtipi/.env', '/var/lib/wizard-os-migration/credentials/before.env'):
        r = run('protected-read-denied', ['runuser', '-u', 'hermes', '--', 'test', '-r', target], ok=(0, 1))
        assert r.returncode == 1
    revision = user('hermes', 'stock-hermes-unchanged', 'git', '-C', '/home/hermes/hermes-agent', 'rev-parse', 'HEAD').stdout.decode().strip()
    assert revision == config['hermes_commit']
    user('hermes', 'thin-client-apps', '/usr/local/bin/wam', 'apps', '--json')
    doctor = json.loads(user('hermes', 'thin-client-doctor', '/usr/local/bin/wam', 'doctor', '--json').stdout)
    assert doctor and all(row['ok'] for row in doctor)
    event('identity-readback', no_docker=True, no_sudo=True, no_migration_grant=True,
          protected_read_denied=True, thin_client=True, doctor=True, hermes_unchanged=True)


def backend(config):
    cid = run('backend-container', ['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=runtipi',
                                    '--filter', 'label=com.docker.compose.service=runtipi']).stdout.decode().split()
    assert len(cid) == 1
    image = run('backend-image', ['docker', 'inspect', '--format', '{{.Image}}', cid[0]]).stdout.decode().strip()
    assert image == config['backend_image']['id'], 'Wrong backend image: no old latest substitution'
    event('backend-identity', image=image, source_revision=config['image_source_revision'])


def failure(error):
    # Trusted filenames/functions/lines only; never payload, locals or exception text.
    frames = []
    tb = error.__traceback__
    while tb:
        f = Path(tb.tb_frame.f_code.co_filename)
        trusted = f.is_relative_to(SOURCE)
        frames.append({'file': str(f.relative_to(SOURCE)) if trusted else '<external>',
                       'function': tb.tb_frame.f_code.co_name if trusted else '<external>', 'line': tb.tb_lineno})
        tb = tb.tb_next
    return {'class': type(error).__name__ if type(error).__module__ == 'builtins' else 'ProductOrExternalError',
            'frames': frames[-16:]}


def main():
    global PHASE
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('seed', 'data', 'copy', 'credentials', 'identity', 'backend'))
    p.add_argument('scenario', choices=('fresh', 'legacy', 'migrated'))
    p.add_argument('config', type=Path)
    a = p.parse_args()
    assert os.getuid() == 0 and Path('/run/privsep-compat-disposable').is_file()
    STATE.mkdir(mode=0o700, exist_ok=True); os.chmod(STATE, 0o700)
    c = json.loads(a.config.read_text())
    PHASE = a.scenario + '-' + a.action
    try:
        {'seed': lambda: seed_and_exercise(a.scenario, c), 'data': lambda: verify_data(a.scenario),
         'copy': stopped_copy, 'credentials': credentials, 'identity': lambda: denied_identity(c),
         'backend': lambda: backend(c)}[a.action]()
        event('completed', action=a.action, scenario=a.scenario)
        return 0
    except Exception as e:
        detail = failure(e)
        save('failure.json', {'phase': PHASE, **detail})
        event('failure', **detail)
        return 1


if __name__ == '__main__':
    sys.exit(main())
