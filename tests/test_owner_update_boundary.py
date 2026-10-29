"""Status observes; only explicit mutation verifies; Manual and queue admission serialize."""
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import app_updates as au, cli, private, source_integrity as si, updates, network
from test_legacy_source_inventory import legacy
from test_app_updates import row


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('record', ['missing', 'corrupt', 'valid', 'changed'])
def test_status_offline_never_fetches_or_writes(tmp_path, monkeypatch, record):
    root, live, source, _, _ = legacy(tmp_path, monkeypatch)
    if record != 'missing':
        si.record(source)
    if record == 'corrupt':
        si.record_path(source).write_text('{')
    if record == 'changed':
        (source / 'code.py').write_text('owner')
    monkeypatch.setattr(private, 'fetch', lambda *a: pytest.fail('status fetched upstream'))
    monkeypatch.setattr(private, 'token', lambda: pytest.fail('status requested credentials'))
    monkeypatch.setattr(au, '_release_managed', lambda: set())
    home = tmp_path / 'home'
    c = SimpleNamespace(home=home, runtipi_root=root,
                        runtipi=lambda: SimpleNamespace(installed=lambda: [row('demo', 1, 2)]))
    before = snapshot(tmp_path)
    result = au.status(c)
    expected = None if record == 'valid' else 'owner-source' if record == 'changed' else 'source-unverified'
    assert (result['due'] or result['skipped'])[0]['skip'] == expected
    assert result['chosen'] is False and not home.exists()
    # Exercise the actual Settings GET aggregation without replacing app status.
    monkeypatch.setattr(updates, 'status', lambda: {'mode': 'auto', 'update_available': False})
    handler = cli.StoreHandler.__new__(cli.StoreHandler)
    handler.cfg = c
    sent = []
    handler._json = lambda code, body: sent.append((code, body))
    handler._updates('GET')
    assert sent == [(200, {'mode': 'auto', 'update_available': False, 'apps': result})]
    assert snapshot(tmp_path) == before


def _manual(home, units):
    au.set_mode('ask', Path(home), Path(units), systemctl=False)


def test_manual_saved_after_final_batch_read_prevents_queue(tmp_path, monkeypatch):
    """Pause the automatic runner inside the real per-app path, before its queue admission."""
    home = tmp_path / 'home'
    home.mkdir()
    queued = []
    class Rt:
        def installed(self): return [row('demo', 1, 2)]
        def app(self, urn): return {'app': {'status': 'running', 'version': 1}}
        def update(self, *a, **kw): queued.append(a)
    rt = Rt()
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: rt)
    monkeypatch.setattr(updates, 'installing', lambda h: False)
    monkeypatch.setattr(au, '_release_managed', lambda: set())
    monkeypatch.setattr(au, '_containers', lambda *a: [])
    monkeypatch.setattr(au, 'baseline', lambda *a: None)
    monkeypatch.setattr(cli.alongside, 'load', lambda *a: {})
    monkeypatch.setattr(cli, 'host_access_risks', lambda *a: [])
    # Pre-approved synthetic fixture; production recipe approval gate stays intact.
    monkeypatch.setattr(cli.host_policy, 'require_app', lambda *a, **kw: cli.host_policy.risk_digest('demo', [], 'wizardapps'))
    monkeypatch.setattr(cli, 'write_registry', lambda *a: {})
    monkeypatch.setattr(network, 'enabled', lambda *a: False)
    paused = False
    def prepare(*a, **kw):
        nonlocal paused
        if not paused:
            paused = True
            # Independent process saves Manual while this automatic per-app runner is paused.
            p = multiprocessing.get_context('fork').Process(target=_manual, args=(str(home), str(tmp_path / 'units')))
            p.start(); p.join(5)
            assert p.exitcode == 0, 'Manual save blocked behind the long update'
        return {}
    monkeypatch.setattr(cli, 'network_step', prepare)
    monkeypatch.setattr(cli.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(au, 'verify', lambda *a: None)
    result = au.apply(c, automatic=True, notify=False)
    assert au.mode(home) == 'ask'
    assert queued == [], 'automatic queue admission crossed an already saved Manual choice'
    assert result['updated'] == []


def test_policy_lock_covers_read_and_queue_not_update_wait(tmp_path, monkeypatch):
    """Manual cannot complete inside admission, but can complete while the admitted app finishes."""
    home = tmp_path / 'home'; home.mkdir()
    child = None
    class Rt:
        def app(self, urn): return {'app': {'status': 'stopped', 'version': 2}}
        def update(self, *a, **kw):
            nonlocal child
            # fork must not inherit the held flock descriptor: subprocess exec instead.
            import subprocess, sys
            child = subprocess.Popen([sys.executable, '-c',
                'from pathlib import Path; from wam import app_updates as a; import sys; '
                'print("ready",flush=True); '
                'a.set_mode("ask",Path(sys.argv[1]),Path(sys.argv[2]),systemctl=False)',
                str(home), str(tmp_path / 'units')], stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'ready'
            with pytest.raises(subprocess.TimeoutExpired): child.wait(timeout=0.2)
        def wait_status(self, *a):
            child.wait(timeout=5)
            assert au.mode(home) == 'ask', 'lock must not cover update completion'
            return 'stopped'
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: Rt())
    monkeypatch.setattr(cli, 'host_access_risks', lambda *a: [])
    # Pre-approved synthetic fixture; production recipe approval gate stays intact.
    monkeypatch.setattr(cli.host_policy, 'require_app', lambda *a, **kw: cli.host_policy.risk_digest('demo', [], 'wizardapps'))
    monkeypatch.setattr(cli, 'network_step', lambda *a, **kw: {})
    monkeypatch.setattr(cli.time, 'sleep', lambda *a: None)
    try:
        result = cli.app_update(c, 'demo', backup=False, automatic=True)
        assert result['status'] == 'stopped'
    finally:
        if child is not None and child.poll() is None: child.kill(); child.wait()


def test_explicit_one_shot_still_queues_in_manual(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    au.set_mode('ask', home, tmp_path / 'units', systemctl=False)
    queued = []
    class Rt:
        def app(self, urn): return {'app': {'status': 'stopped', 'version': 2}}
        def update(self, *a, **kw): queued.append(a)
        def wait_status(self, *a): return 'stopped'
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: Rt())
    monkeypatch.setattr(cli, 'host_access_risks', lambda *a: [])
    # Pre-approved synthetic fixture; production recipe approval gate stays intact.
    monkeypatch.setattr(cli.host_policy, 'require_app', lambda *a, **kw: cli.host_policy.risk_digest('demo', [], 'wizardapps'))
    monkeypatch.setattr(cli, 'network_step', lambda *a, **kw: {})
    monkeypatch.setattr(cli.time, 'sleep', lambda *a: None)
    assert cli.app_update(c, 'demo', backup=False)['status'] == 'stopped'
    assert queued == [('demo:wizardapps',)] and au.mode(home) == 'ask'


def test_batch_single_flight_shared_by_owner_and_timer(tmp_path, monkeypatch):
    import fcntl
    home = tmp_path / 'home'; home.mkdir()
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: pytest.fail('loser mutated'))
    with (home / au.LOCK).open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for automatic in (True, False):
            assert au.apply(c, automatic=automatic, notify=False)['skipped'] == 'running'
