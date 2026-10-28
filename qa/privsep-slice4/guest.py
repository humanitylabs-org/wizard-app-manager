#!/usr/bin/env python3
"""Run at the stock Hermes uid in a disposable guest. Never imports privileged wam."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

assert os.getuid() != 0
phase = sys.argv[1]
home = Path.home()
source = home / 'slice4-source' / 'slice4-app'
results = []


def wam(*argv, code: int | tuple[int, ...] = 0):
    p = subprocess.run(['wam', *argv], capture_output=True, text=True, timeout=1800)
    row = {'argv': list(argv), 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    results.append(row)
    print(json.dumps(row), flush=True)
    assert p.returncode in ((code,) if isinstance(code, int) else code), row
    return json.loads(p.stdout) if p.stdout.strip().startswith(('{', '[')) else p.stdout


def wait_private_link(verb, deadline):
    """Retry only the exact running+loopback admission, never tailnet/errors.

    Runtipi's asynchronous health transition used to outlive successful Undo.
    Keep a bounded readiness guard without weakening the publication assertion.
    """
    attempts = 0
    while True:
        got = wam('links', verb, 'wizard-calendar', code=(0, 1))
        attempts += 1
        row = results[-1]
        if row['exit'] == 0:
            print(json.dumps({'link_readiness': 'running app with loopback page',
                              'verb': verb, 'attempts': attempts}), flush=True)
            return got
        error = json.loads(row['stderr']).get('error')
        assert error == 'installed running app with a loopback page required', row
        print(json.dumps({'link_readiness_wait': error, 'verb': verb}), flush=True)
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'running+loopback page did not become ready within 90 seconds'
        time.sleep(min(1, remaining))


def health(updated=False):
    end = time.monotonic() + 90
    while time.monotonic() < end:
        try:
            with urlopen('http://127.0.0.1:18799/healthz', timeout=5) as r:
                doc = json.load(r)
            if doc.get('ok') and doc.get('app') == 'slice4-app' and (not updated or doc.get('slice4') == 'updated'):
                print(json.dumps({'health': doc}), flush=True)
                return doc
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError('candidate did not answer real loopback health')


if phase == 'backup':
    marker = Path('/var/lib/wizard-os/runtipi/media/slice4-files.txt')
    marker.write_text('snapshot\n')
    capture = wam('backup', 'run')
    assert capture['ok'] and capture['verified'] and capture['snapshot']
    assert not any(v.startswith('wizard-calendar:') for v in capture.get('apps_without_data', []))
    snapshots = wam('backup', 'snapshots')
    assert any(row['id'] == capture['snapshot'] for row in snapshots['snapshots'])
    (home / 'slice4-snapshot.json').write_text(json.dumps({'snapshot': capture['snapshot']}))
    marker.write_text('before-restore\n')
    print(json.dumps({'backup_run_list': 'passed'}))
elif phase == 'restore':
    snap = json.loads((home / 'slice4-snapshot.json').read_text())['snapshot']
    got = wam('backup', 'import', '--from-repo', snap, '--yes')
    assert got['ok'] and not got['partial']
    assert any(a['id'] == 'wizard-calendar' for a in got['manifest']['apps'])
    assert Path('/var/lib/wizard-os/runtipi/media/slice4-files.txt').read_text() == 'snapshot\n'
    assert not got['agents']  # never run the linked host's writable Hermes as wizard-system
    print(json.dumps({'restore_from_own_repo': 'passed'}))
elif phase == 'undo':
    got = wam('backup', 'undo')
    assert got['ok']
    assert Path('/var/lib/wizard-os/runtipi/media/slice4-files.txt').read_text() == 'before-restore\n'
    print(json.dumps({'undo': 'passed'}))
elif phase == 'links':
    deadline = time.monotonic() + 90
    for verb in ('publish', 'unpublish'):
        got = wait_private_link(verb, deadline)
        assert got['operator_uid'] == int(sys.argv[2]) and got['operator_uid'] != os.getuid()
        assert got['state'] != 'Running' and not got['published']
        print(json.dumps({'tailnet_dependent': True, 'operator_path': 'wizard-system', 'result': got}))
elif phase == 'build':
    assert source.stat().st_uid == os.getuid()
    assert wam('builder', 'check', str(source))['ok']
    for target in ('/etc', '/'):
        escape = source / 'escape'
        escape.symlink_to(target)
        try:
            wam('builder', 'install-local', str(source), '--stage-only', '--run', code=1)
            assert 'symlink' in results[-1]['stderr']
        finally:
            escape.unlink()
    compose = source / 'compose.yml'
    compose.write_text('services: {slice4-app: {image: alpine, volumes: ["/:/host"]}}\n')
    try:
        wam('builder', 'install-local', str(source), '--stage-only', '--run', code=77)
        assert 'needs_owner_approval' in results[-1]['stderr']
    finally:
        compose.unlink()
    rec = wam('builder', 'install-local', str(source), '--stage-only', '--run', '--port', '18799')
    assert not os.access(rec['source'], os.W_OK) and not os.access(rec['compose'], os.W_OK)
    assert not os.access('/var/lib/wizard-os/.wizard-os/build-work', os.W_OK)
    health()
    server = source / 'app/server.py'
    text = server.read_text()
    assert "{'ok': True, 'app': APP_ID, 'version': VERSION}" in text
    server.write_text(text.replace("{'ok': True, 'app': APP_ID, 'version': VERSION}",
                                   "{'ok': True, 'app': APP_ID, 'version': VERSION, 'slice4': 'updated'}"))
    update = wam('builder', 'update-local', str(source), '--run')
    assert update['update'] == 'updated' and update['source_sha256'] != rec['source_sha256']
    health(updated=True)
    print(json.dumps({'build_update': 'passed', 'build_directory_agent_writable': False}))
elif phase == 'hermes':
    got = wam('hermes', 'update', '--check')
    assert got
    # wizard link has already run as this account during stock Hermes acceptance.
    doc = json.loads((Path('/var/lib/wizard-agent') / str(os.getuid()) / 'link.json').read_text())
    assert doc['wam_url'] == json.loads((home / '.wizard-os/client.json').read_text())['url']
    assert doc['wam_token'] == (home / '.wizard-os/agent-token').read_text().strip()
    assert doc['wam_token'] and doc['hermes_key']
    print(json.dumps({'own_hermes_check': 'passed', 'wam_handoff': 'matches agent-scoped client'}))
elif phase == 'denials':
    assert not {'docker', 'sudo', 'wheel'} & set(subprocess.check_output(['id', '-nG'], text=True).split())
    for argv in (['sudo', '-n', 'true'], ['docker', 'info'],
                 ['docker', 'run', '--rm', '-v', '/:/host', 'alpine', 'true'], ['tailscale', 'down']):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        assert p.returncode != 0, argv
    for path in ('/run/docker.sock', '/var/run/docker.sock', '/run/containerd/containerd.sock'):
        assert not os.access(path, os.R_OK | os.W_OK), path
    print(json.dumps({'final_privilege_denials': 'passed', 'uid': os.getuid()}))
else:
    raise AssertionError('unknown phase')
