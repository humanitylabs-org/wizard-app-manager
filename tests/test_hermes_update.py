"""Hermes updates use only disposable homes, git repositories and loopback servers."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wam import cli, updates


def test_updates_status_has_hermes_without_a_pin(tmp_path):
    (tmp_path / 'stable').write_text('1.0.0')
    assert updates.status(tmp_path, tmp_path.as_uri())['hermes']['state'] == 'no_pin'


@pytest.fixture
def box(tmp_path, monkeypatch):
    # Import inside the fixture: the integration test above also fails on main.
    from wam import hermes_update as hu
    h, hh = tmp_path / 'wizard', tmp_path / 'hermes'
    h.mkdir(); hh.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('WIZARD_OS_HOME', str(h))
    monkeypatch.setenv('HERMES_HOME', str(hh))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    repo = hh / 'hermes-agent'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
    (repo / '.gitignore').write_text('venv/\ninstall-stamp.json\n')
    commits = []
    for v in ('0.0.1', '0.0.2'):
        (repo / 'version').write_text(v)
        git('add', '.'); git('commit', '-m', v); git('tag', 'v' + v)
        commits.append(git('rev-parse', 'HEAD'))
    remote = tmp_path / 'upstream.git'
    subprocess.run(['git', 'clone', '--bare', str(repo), str(remote)], check=True, capture_output=True)
    git('remote', 'add', 'origin', str(remote)); git('reset', '--hard', commits[0])
    bin_dir = repo / 'venv' / 'bin'; bin_dir.mkdir(parents=True)
    # No real package installation, imports, gateway or service controller can run.
    fake = '#!' + sys.executable + '\nimport sys\nif "freeze" in sys.argv: print("sample==1.0")\n'
    for name in ('python', 'pip'):
        p = bin_dir / name; p.write_text(fake); p.chmod(0o755)
    monkeypatch.setattr(hu.shutil, 'which', lambda name: None)
    with sqlite3.connect(hh / 'state.db') as db:
        db.execute('CREATE TABLE sessions (id TEXT)')
        db.execute("INSERT INTO sessions VALUES ('preserve')")
    (hh / 'config.yaml').write_text('original config\n')
    (hh / '.env').write_text('FIXTURE_VALUE=not-a-secret\n')
    (repo / 'install-stamp.json').write_text(json.dumps({'baseVersion': '0.0.1', 'updateMechanism': 'external'}))
    queue = {'active': 0, 'queued': 0}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value = {'version': (repo / 'version').read_text(), 'status': 'ok'} if self.path == '/health' else {'queue': queue}
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    port = server.server_port
    alongside = {'mode': 'alongside', 'agent': {'hermes_home': str(hh), 'origin': f'http://127.0.0.1:{port}',
                                              'uid': os.getuid()}, 'service': {'port': port}}
    (h / 'alongside.json').write_text(json.dumps(alongside))
    (h / 'release.json').write_text(json.dumps({'release': '1.0.0'}))
    releases = tmp_path / 'releases'; releases.mkdir()
    (releases / 'stable').write_text('9.0.0')  # never selects a newer release than the box follows
    pin = {'tag': 'v0.0.2', 'commit': commits[1], 'image': None, 'test_record': 'fixture',
           'rollback': {'tag': 'v0.0.1', 'commit': commits[0]}}
    def set_pin(value):
        (releases / '1.0.0.json').write_text(json.dumps({'hermes': value} if value is not None else {}))
    set_pin(pin)
    monkeypatch.setenv('WIZARD_RELEASE_BASE', releases.as_uri())
    route = {'scope': 'user', 'unit': 'hermes-gateway-fixture.service', 'pid': 123456, 'usr1': False}
    monkeypatch.setattr(hu, 'supervisor', lambda *a: route)
    restarts = []
    monkeypatch.setattr(hu, 'restart', lambda *a: restarts.append((repo / 'version').read_text()))
    yield dict(hu=hu, h=h, hh=hh, repo=repo, git=git, commits=commits, queue=queue, pin=pin,
               set_pin=set_pin, restarts=restarts, releases=releases, route=route)
    server.shutdown(); server.server_close(); thread.join()


def test_status_states(box):
    b = box; hu = b['hu']
    st = hu.status(b['h'])
    assert st['install_type'] == 'git' and st['state'] == 'update_available'
    assert st['running_version'] == '0.0.1' and st['target_tag'] == 'v0.0.2'
    assert st['update_available'] and not st['downgrade'] and not st['dirty']
    b['git']('checkout', 'v0.0.2')
    assert hu.status(b['h'])['state'] == 'up_to_date'
    b['set_pin'](None)
    assert hu.status(b['h'])['state'] == 'no_pin'
    assert hu.update(b['h'])['state'] == 'no_pin'
    b['set_pin'](b['pin'])
    (b['h'] / 'alongside.json').unlink()
    assert hu.status(b['h'])['state'] == 'container'
    assert hu.update(b['h'])['state'] == 'container'


def test_manual_installs_are_never_touched(box):
    b = box
    (b['repo'] / 'venv' / 'bin' / 'python').unlink()
    assert b['hu'].status(b['h'])['state'] == 'manual'
    assert b['hu'].update(b['h'])['state'] == 'manual'
    assert 'update this Hermes by hand' in b['hu'].status(b['h'])['reason']
    assert b['git']('rev-parse', 'HEAD') == b['commits'][0]


@pytest.mark.parametrize('case', ['downgrade', 'dirty', 'mismatch', 'disk', 'no_supervisor', 'wrong_user'])
def test_refusals_leave_checkout_unchanged(box, monkeypatch, case):
    b = box; hu = b['hu']
    if case == 'downgrade':
        b['git']('checkout', 'v0.0.2')
        b['set_pin']({**b['pin'], 'tag': 'v0.0.1', 'commit': b['commits'][0]})
        assert hu.status(b['h'])['downgrade']
    elif case == 'dirty':
        (b['repo'] / 'version').write_text('dirty')
        assert hu.status(b['h'])['dirty']
    elif case == 'mismatch':
        b['set_pin']({**b['pin'], 'commit': b['commits'][0]})
    elif case == 'disk':
        monkeypatch.setattr(hu.shutil, 'disk_usage', lambda p: type('Disk', (), {'free': 0})())
    elif case == 'no_supervisor':
        monkeypatch.setattr(hu, 'supervisor', lambda *a: None)
    else:
        state = json.loads((b['h'] / 'alongside.json').read_text())
        state['agent']['uid'] = os.getuid() + 1
        (b['h'] / 'alongside.json').write_text(json.dumps(state))
    before = b['git']('rev-parse', 'HEAD')
    out = hu.update(b['h'], wait_quiet=0)
    assert out['state'] == 'refused'
    if case == 'mismatch':
        assert "release pin doesn't match upstream" in out['reason']
    assert b['git']('rev-parse', 'HEAD') == before and not b['restarts']


def test_update_with_online_backup_and_receipt(box):
    b = box; hu = b['hu']
    out = hu.update(b['h'], wait_quiet=0, health_timeout=0)
    assert out['state'] == 'updated', out
    assert b['git']('rev-parse', 'HEAD') == b['commits'][1]
    assert hu.status(b['h'])['running_version'] == '0.0.2'
    saved = json.loads((b['h'] / 'hermes-update.json').read_text())
    assert saved['state'] == 'updated' and saved['from'] == '0.0.1' and saved['to'] == 'v0.0.2'
    assert saved['pid'] == os.getpid() and saved['boot_id'] and saved['timings']
    backup = Path(saved['backup'])
    with sqlite3.connect(backup / 'state.db') as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT id FROM sessions').fetchall() == [('preserve',)]
    assert (backup / '.env').read_text() == (b['hh'] / '.env').read_text()
    assert (backup / 'freeze.txt').read_text() == 'sample==1.0\n'
    assert json.loads((backup / 'snapshot.json').read_text())['commit'] == b['commits'][0]
    assert backup.stat().st_mode & 0o777 == 0o700
    assert 'web_dist' in ' '.join(saved['notes'])
    assert json.loads((b['repo'] / 'install-stamp.json').read_text())['baseVersion'] == '0.0.2'


def test_busy_deadline_does_not_mutate_install(box):
    b = box; b['queue']['active'] = 1
    out = b['hu'].update(b['h'], wait_quiet=0)
    assert out['state'] == 'busy' and b['git']('rev-parse', 'HEAD') == b['commits'][0]
    assert not b['restarts']


@pytest.mark.parametrize('surface', ['gateway', 'leases', 'delegations', 'cron', 'bad_gateway', 'bad_db', 'bad_schema'])
def test_work_census_fails_closed(box, surface):
    b = box; hh = b['hh']
    if surface == 'gateway':
        (hh / 'gateway_state.json').write_text(json.dumps({'active_work': [{'kind': 'turn'}]}))
    elif surface == 'bad_gateway':
        (hh / 'gateway_state.json').write_text('{')
    elif surface == 'bad_db':
        (hh / 'state.db').write_bytes(b'not a database')
    elif surface in ('leases', 'delegations', 'bad_schema'):
        with sqlite3.connect(hh / 'state.db') as db:
            if surface == 'leases':
                db.execute('CREATE TABLE session_turn_leases (expires_at REAL)')
                db.execute('INSERT INTO session_turn_leases VALUES (?)', (time.time() + 60,))
            elif surface == 'delegations':
                db.execute('CREATE TABLE async_delegations (state TEXT)')
                db.execute("INSERT INTO async_delegations VALUES ('running')")
            else:
                db.execute('CREATE TABLE session_turn_leases (unexpected TEXT)')
    else:
        (hh / 'cron').mkdir()
        with sqlite3.connect(hh / 'cron' / 'executions.db') as db:
            db.execute('CREATE TABLE executions (status TEXT, finished_at TEXT)')
            db.execute("INSERT INTO executions VALUES ('running', NULL)")
    assert b['hu'].busy(b['h'], hh)


def test_missing_tables_and_expired_leases_are_idle(box):
    b = box
    with sqlite3.connect(b['hh'] / 'state.db') as db:
        db.execute('CREATE TABLE session_turn_leases (expires_at REAL)')
        db.execute('INSERT INTO session_turn_leases VALUES (0)')
    assert b['hu'].busy(b['h'], b['hh']) == []
    (b['hh'] / 'state.db').unlink()
    assert b['hu'].busy(b['h'], b['hh']) == []


def test_restart_failure_rolls_back_and_restores_files(box, monkeypatch):
    b = box; hu = b['hu']; calls = []
    original = {p: p.read_bytes() for p in [b['hh'] / 'config.yaml', b['hh'] / '.env', b['repo'] / 'install-stamp.json']}
    def fail_once(*args):
        calls.append(1)
        if len(calls) == 1:
            (b['hh'] / 'config.yaml').write_text('changed')
            (b['hh'] / '.env').write_text('changed')
            raise RuntimeError('fake restart failed')
    monkeypatch.setattr(hu, 'restart', fail_once)
    out = hu.update(b['h'], wait_quiet=0, health_timeout=0)
    assert out['state'] == 'rolled_back', out
    assert b['git']('rev-parse', 'HEAD') == b['commits'][0]
    assert hu.status(b['h'])['running_version'] == '0.0.1' and len(calls) == 2
    assert all(p.read_bytes() == data for p, data in original.items())


def test_rollback_failure_exposes_backup(box, monkeypatch):
    b = box
    def fail(*a):
        raise RuntimeError('fake permanent restart failure')
    monkeypatch.setattr(b['hu'], 'restart', fail)
    out = b['hu'].update(b['h'], wait_quiet=0, health_timeout=0)
    assert out['state'] == 'rollback_failed' and Path(out['backup']).is_dir()


def test_interrupted_receipt_after_reboot(box, monkeypatch):
    b = box
    (b['h'] / 'hermes-update.json').write_text(json.dumps({'state': 'running', 'boot_id': 'before', 'pid': os.getpid()}))
    monkeypatch.setattr(updates, 'boot_id', lambda *a: 'after')
    assert b['hu'].status(b['h'])['receipt']['state'] == 'interrupted'
    assert b['hu'].update(b['h'])['state'] == 'refused'
    assert b['git']('rev-parse', 'HEAD') == b['commits'][0]


def test_concurrent_update_respects_lock_and_receipt(box):
    b = box; p = b['h'] / 'hermes-update.json'; p.write_text('{"state":"running"}')
    with (b['h'] / 'hermes-update.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert b['hu'].update(b['h'])['state'] == 'busy'
        assert p.read_text() == '{"state":"running"}'
    assert b['git']('rev-parse', 'HEAD') == b['commits'][0]


def test_automatic_updates_never_call_host_update(box, monkeypatch):
    b = box
    def forbidden(*a, **kw):
        pytest.fail('automatic host Hermes update')
    monkeypatch.setattr(b['hu'], 'update', forbidden)
    (b['releases'] / 'stable').write_text('1.1.0')
    calls = []
    def run(cmd, **kw):
        calls.append(cmd); return subprocess.CompletedProcess(cmd, 0, '', '')
    assert updates.apply_if_auto(b['h'], runner=run, is_busy=lambda: False)['started']
    assert len(calls) == 1
    assert b['git']('rev-parse', 'HEAD') == b['commits'][0]


def test_cli_status_and_dry_run(box, capsys):
    b = box
    assert cli.main(['hermes', 'status', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['target_tag'] == 'v0.0.2'
    assert cli.main(['hermes', 'update', '--json', '--dry-run', '--wait-quiet', '0']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'dry_run'
    assert b['git']('rev-parse', 'HEAD') == b['commits'][0]
    assert not (b['h'] / 'hermes-update.json').exists() and not b['restarts']
