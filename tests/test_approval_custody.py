"""Source-level/synthetic negatives; not authenticated tailnet acceptance."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from wam import approval_custody as ac, owner_confirm as oc, client

HOST = 'box.example.ts.net'
ORIGIN = f'https://{HOST}:8446'
SITE = {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:7792'}}}


@pytest.fixture
def system(monkeypatch):
    props = {
        ac.SOCKET: {'FragmentPath': str(ac.UNITS / ac.SOCKET), 'DropInPaths': '',
                    'ActiveState': 'active', 'SubState': 'listening', 'UnitFileState': 'enabled',
                    'NeedDaemonReload': 'no', 'RefuseManualStop': 'yes', 'Accept': 'no', 'TriggerLimitIntervalUSec': '0',
                    'Listen': '127.0.0.1:7792 (Stream)'},
        'tailscaled.service': {'DropInPaths': str(ac.UNITS / 'tailscaled.service.d/50-wizard-owner-approval.conf'),
                              'Requires': ac.SOCKET, 'After': ac.SOCKET, 'NeedDaemonReload': 'no'},
        ac.SERVICE: {'MainPID': str(os.getpid()), 'TriggeredBy': ac.SOCKET, 'StartLimitIntervalUSec': '0', 'NeedDaemonReload': 'no'},
    }
    monkeypatch.setattr(ac, '_trusted_file', lambda path, text: True)
    monkeypatch.setattr(ac, '_show', lambda unit: props[unit])
    monkeypatch.setattr(ac, 'listener_row', lambda: 123)
    monkeypatch.setattr(ac.os, 'getppid', lambda: 1)
    for k, v in {'LISTEN_PID': str(os.getpid()), 'LISTEN_FDS': '1', 'LISTEN_FDNAMES': ac.NAME}.items():
        monkeypatch.setenv(k, v)
    return props


@pytest.mark.parametrize('key,value', [('ActiveState', 'failed'), ('UnitFileState', 'disabled'),
    ('NeedDaemonReload', 'yes'), ('Listen', '0.0.0.0:7792 (Stream)'), ('DropInPaths', '/rogue.conf'),
    ('RefuseManualStop', 'no'), ('Accept', 'yes'), ('FragmentPath', '/other.socket')])
def test_wrong_loaded_socket_identity_refuses(system, key, value):
    system[ac.SOCKET][key] = value
    with pytest.raises(ValueError):
        ac.require_custody()


def test_worker_start_limit_must_not_be_able_to_close_socket(system):
    system[ac.SERVICE]['StartLimitIntervalUSec'] = '10s'
    with pytest.raises(ValueError, match='start-limit'): ac.require_custody()


def test_root_files_and_boot_order_required(system, monkeypatch):
    monkeypatch.setattr(ac, '_trusted_file', lambda p, t: False)
    with pytest.raises(ValueError): ac.require_custody()
    monkeypatch.setattr(ac, '_trusted_file', lambda p, t: True)
    system['tailscaled.service']['After'] = ''
    with pytest.raises(ValueError): ac.require_custody()


@pytest.mark.parametrize('key,value', [('LISTEN_PID', '1'), ('LISTEN_FDS', '2'),
    ('LISTEN_FDS', '0'), ('LISTEN_FDNAMES', 'wrong'), ('LISTEN_FDNAMES', ac.NAME + ':' + ac.NAME)])
def test_environment_and_duplicate_descriptor_negatives(system, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError): ac.inherited_listener()
    assert 'LISTEN_FDS' not in os.environ


def test_environment_cannot_claim_system_service_identity(system):
    system[ac.SERVICE]['MainPID'] = '1'
    with pytest.raises(RuntimeError): ac.inherited_listener()


def test_wrong_inode_descriptor_refused(system, monkeypatch):
    monkeypatch.setattr(ac.os, 'readlink', lambda p: 'socket:[456]')
    with pytest.raises(RuntimeError, match='not the custody socket'): ac.inherited_listener()


@pytest.mark.parametrize('uid', [0, 999999])
def test_root_and_agent_uid_cannot_restart_protected_worker(system, monkeypatch, uid):
    monkeypatch.setattr(ac.os, 'getuid', lambda: uid)
    monkeypatch.setattr(ac.os, 'pidfd_open', lambda *a: pytest.fail('wrong principal reached process handle'), raising=False)
    with pytest.raises(ValueError, match='protected manager account'):
        ac.restart_worker()


def test_no_root_staging_under_writable_ancestor(tmp_path):
    p = tmp_path / 'unit'; p.write_text(ac.SOCKET_TEXT)
    assert not ac._trusted_file(p, ac.SOCKET_TEXT)


@pytest.fixture
def route(tmp_path, monkeypatch):
    web = {}
    calls = []
    monkeypatch.setattr(oc, 'listener_owned', lambda h: True)
    monkeypatch.setattr(oc, '_serve_status', lambda: {'Web': web})
    def run(argv):
        calls.append(argv)
        authority = HOST + ':' + next(v.split('=', 1)[1] for v in argv if v.startswith('--https='))
        if argv[-1] == 'off': web.pop(authority, None)
        else: web[authority] = SITE
        return SimpleNamespace(returncode=0, stderr='', stdout='')
    return tmp_path, web, calls, run


def test_publication_waits_for_shared_root_removal_lock(route):
    import threading
    home, web, calls, run = route
    ready, done = threading.Event(), threading.Event()
    errors = []
    def publish():
        ready.set()
        try: oc.enable(home, HOST, run=run)
        except BaseException as exc: errors.append(exc)
        finally: done.set()
    with ac.publication_lock(home):
        t = threading.Thread(target=publish)
        t.start()
        assert ready.wait(1) and not done.wait(.05)
        assert not calls
    t.join(timeout=2)
    assert done.is_set() and not errors and len(calls) == 1


def test_publish_readback_and_idempotent_retry(route):
    home, web, calls, run = route
    assert oc.enable(home, HOST, run=run)['origin'] == ORIGIN
    assert oc.enable(home, HOST, run=run)['origin'] == ORIGIN
    assert len(calls) == 1
    assert oc.verified_origin(home) == ORIGIN


def test_existing_imposter_route_even_matching_target_is_not_adopted(route):
    home, web, calls, run = route
    web[HOST + ':8446'] = SITE
    with pytest.raises(ValueError, match='no continuous custody'): oc.enable(home, HOST, run=run)
    assert not calls and not (home / oc.CONFIG).exists()


def test_legacy_origin_never_becomes_safe_by_listener_replacement(route):
    home, web, calls, run = route
    (home / oc.CONFIG).write_text(json.dumps({'origin': ORIGIN}))
    assert oc.configured(home) is None
    with pytest.raises(ValueError, match='legacy'): oc.enable(home, HOST, run=run)
    assert not calls


def test_route_removal_ack_before_retirement_and_no_port_reuse(route):
    home, web, calls, run = route
    oc.enable(home, HOST, run=run)
    doc = (home / oc.CONFIG).read_bytes()
    with pytest.raises(ValueError, match='not acknowledged'):
        oc.disable(home, run=lambda a: SimpleNamespace(returncode=0))
    assert (home / oc.CONFIG).read_bytes() == doc
    assert oc.disable(home, run=run) == {'disabled': True, 'custody_retained': True}
    assert HOST + ':8446' not in web
    with pytest.raises(ValueError, match='retired'): oc.enable(home, HOST, run=run)
    assert oc.enable(home, HOST, 8447, run=run)['origin'].endswith(':8447')
    assert all(a[0] == 'tailscale' for a in calls)  # no socket stop, no elevation


def test_failed_publication_preserves_custody_record(route):
    home, web, calls, run = route
    with pytest.raises(ValueError, match='read back'):
        oc.enable(home, HOST, run=lambda a: SimpleNamespace(returncode=0))
    assert json.loads((home / oc.CONFIG).read_text())['custody'] == 1


def test_agent_client_never_gains_setup_or_system_unit_authority(monkeypatch):
    monkeypatch.setattr(client, 'request', lambda *a: pytest.fail('setup reached agent API'))
    for verb in ('enable', 'disable', 'status'):
        assert client.main(['approval-origin', verb]) == 2


def test_migration_refuses_legacy_routed_listener_before_quiescence(monkeypatch):
    from wam import privilege_migration as pm
    m = object.__new__(pm.Migration)
    monkeypatch.setattr(m, 'user', lambda *a, **k: pytest.fail('legacy worker stopped before custody'))
    monkeypatch.setattr(pm, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps({'Web': {HOST + ':8446': SITE}})))
    monkeypatch.setattr(ac, 'require_custody', lambda: (_ for _ in ()).throw(ValueError('custody missing')))
    with pytest.raises(ValueError, match='custody missing'):
        m.quiesce()


def test_worker_has_no_bind_fallback_and_fails_before_generic_listener():
    source = (Path(__file__).parents[1] / 'wam/cli.py').read_text()
    serve = source[source.index('elif args.cmd == "serve":'):]
    assert serve.index('approval_custody.http_server') < serve.index('ThreadingHTTPServer')
    assert 'ThreadingHTTPServer(("127.0.0.1", owner_confirm.PORT)' not in serve
    assert 'owner approval listener not started' not in serve
    assert 'PartOf=' not in ac.SOCKET_TEXT and 'BindsTo=' not in ac.SOCKET_TEXT
    assert 'RefuseManualStop=yes' in ac.SOCKET_TEXT
