"""Slice 5 owner boundary, fixed helper, root expiry/drift and source return."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from wam import developer, updates, cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def root_helper(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader('server_access_test', str(ROOT / 'scripts/root-helpers/server-access'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec); loader.exec_module(module)
    for key in ('STATE', 'SUDOERS', 'HELPERS'):
        path = tmp_path / key; path.mkdir(); monkeypatch.setattr(module, key, path)
    (module.SUDOERS / 'existing').write_text('# baseline\n')
    (module.HELPERS / 'server-access').write_text('protected helper\n')
    manager = tmp_path / 'manager'; manager.mkdir()
    developer.save(manager, {'enabled': True})
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    monkeypatch.setattr(module, 'secure', lambda *a: None)  # fixture paths; production tests run in VM
    monkeypatch.setattr(module.syslog, 'syslog', lambda *a: None)
    calls = []
    real = subprocess.run
    def run(argv, **kw):
        calls.append(argv)
        # Validate the real candidate syntax when visudo is available. Full host
        # policy/timer must never be touched on a unit-test host.
        if argv[:2] == ['/usr/sbin/visudo', '-cf'] and Path(argv[0]).exists():
            return real(argv, **kw)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    return module, {'agent': 'hermes', 'manager_home': str(manager)}, calls


def test_helper_sudoers_expiry_drift_and_revoke(root_helper, monkeypatch):
    mod, cfg, calls = root_helper
    now = [1800000000.0]; monkeypatch.setattr(mod.time, 'time', lambda: now[0])
    row = mod.grant(cfg, {'seconds': 120})
    assert row['active'] and row['severity'] == 'red' and not row['drift']
    policy = (mod.SUDOERS / mod.FILE).read_text()
    assert 'hermes ALL=(ALL:ALL) NOTAFTER=' in policy and 'NOPASSWD: ALL' in policy
    assert 'Defaults:hermes logfile="/var/log/wizard-os-agent-sudo.log"' in policy
    assert any(argv[:2] == ['/usr/sbin/visudo', '-cf'] for argv in calls)
    assert calls.count(['/usr/sbin/visudo', '-c']) == 2
    (mod.HELPERS / 'server-access').write_text('changed helper')
    (mod.SUDOERS / 'other').write_text('# new root policy')
    assert len(mod.status()['drift']) == 2
    mod.grant(cfg, {'seconds': 60})
    assert len(mod.status()['drift']) == 2  # extension cannot launder drift
    now[0] += 61
    assert not mod.status()['active']
    assert not (mod.SUDOERS / mod.FILE).exists()
    assert not (mod.STATE / 'grant.json').exists()


def test_validation_failure_never_installs_sudoers(root_helper, monkeypatch):
    mod, cfg, _ = root_helper
    def fail(*a, **kw): raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(mod.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError): mod.grant(cfg, {})
    assert not (mod.SUDOERS / mod.FILE).exists()


@pytest.mark.parametrize('argv', [[], ['grant', 'extra'], ['revoke', '--user', 'root'], ['status', '/etc'], ['anything']])
def test_helper_refuses_every_nonfinite_argument_list(argv):
    result = subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'scripts/root-helpers/server-access'), *argv], capture_output=True, text=True)
    assert result.returncode == 2 and 'no other arguments' in result.stderr


def test_helper_defends_dev_off_and_fresh_container(root_helper):
    mod, cfg, _ = root_helper
    developer.save(cfg['manager_home'], {'enabled': False})
    with pytest.raises(ValueError, match='developer mode'): mod.grant(cfg, {})
    with pytest.raises(ValueError, match='unsupported for fresh'): mod.grant({**cfg, 'agent': None}, {})


def test_migration_flag_is_refused_and_only_developer_mode_grants(root_helper, monkeypatch):
    mod, cfg, _ = root_helper
    developer.save(cfg['manager_home'], {'enabled': False})
    monkeypatch.setattr(mod.os, 'getgrouplist', lambda *a: [1000])
    monkeypatch.setattr(mod.grp, 'getgrgid', lambda *a: SimpleNamespace(gr_name='docker'))
    # Old automatic seven-day migration handoff: refused even for a docker-group agent.
    with pytest.raises(ValueError): mod.grant(cfg, {'seconds': 604800, 'migration': True})
    with pytest.raises(ValueError, match='developer mode'): mod.grant(cfg, {})
    assert not (mod.SUDOERS / mod.FILE).exists()
    developer.save(cfg['manager_home'], {'enabled': True})
    doc = mod.grant(cfg, {})
    assert doc['active'] and doc['migration'] is False


def test_manager_migration_hook_never_grants(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(developer, 'helper', lambda *a: calls.append(a) or {'active': True})
    assert not developer.migration_grant(tmp_path, False)['active']
    assert not developer.migration_grant(tmp_path, True)['active']
    assert calls == []


def test_developer_off_revokes_before_saving_and_restores_source(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(developer, 'helper', lambda *a: calls.append(a) or {'active': False})
    doc = developer.configure(tmp_path, {'enabled': True, 'repo': developer.RELEASE_REPO, 'ref': 'feature/personal'})
    assert doc['banner'] and 'personal builds' in doc['banner']
    assert updates.apply_if_auto(tmp_path)['skipped'] == 'developer-mode'
    assert updates.apply(tmp_path)['skipped'] == 'developer-mode'
    developer.access(tmp_path, {'action': 'grant', 'seconds': 120})
    doc = developer.configure(tmp_path, {'enabled': False})
    assert ('revoke',) in calls and not doc['enabled']
    assert doc['source']['ref'] == 'releases/stable'
    with pytest.raises(ValueError, match='developer mode'): developer.access(tmp_path, {'action': 'grant'})


def test_off_fails_closed_if_revoke_unavailable(tmp_path, monkeypatch):
    developer.save(tmp_path, {'enabled': True, 'source': {'repo': developer.RELEASE_REPO, 'ref': 'main'}})
    monkeypatch.setattr(developer, 'helper', lambda *a: (_ for _ in ()).throw(ValueError('helper failed')))
    with pytest.raises(ValueError): developer.configure(tmp_path, {'enabled': False})
    assert developer.read(tmp_path)['enabled']


@pytest.mark.parametrize('body', [{'enabled': True, 'repo': 'file:///root/repo', 'ref': 'main'},
                                  {'enabled': True, 'repo': developer.RELEASE_REPO, 'ref': '-c hook'},
                                  {'enabled': 'true'}, {'enabled': False, 'extra': 'unrecognized'}])
def test_invalid_source_and_unknown_fields_refused(tmp_path, body):
    with pytest.raises(ValueError): developer.configure(tmp_path, body)


def test_red_doctor_row_and_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(developer, 'status', lambda home: {'banner': None, 'server_access': {
        'active': True, 'expires_at': '2026-11-01T00:00:00+00:00', 'drift': ['/etc/sudoers.d/changed']}})
    rows = developer.health(tmp_path)
    assert rows[0][0] == 'Agent has full server access until 2026-11-01T00:00:00+00:00'
    assert rows[0][1] is False and 'RED' in rows[0][2]
    assert 'changed' in rows[1][2] and rows[1][1] is False


def test_owner_local_cli_refuses_agent_uid(tmp_path, monkeypatch):
    monkeypatch.setattr(developer.os, 'getuid', lambda: 1000)
    assert developer.main(['on', '--ref', 'main'], tmp_path) == 1
    assert not (tmp_path / 'developer.json').exists()


def test_source_application_and_release_return_use_installer(tmp_path, monkeypatch):
    developer.save(tmp_path, {'enabled': True, 'source': {'repo': developer.RELEASE_REPO, 'ref': 'my/branch'}})
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs)); return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert developer.apply_source(tmp_path, runner)['started']
    assert calls[0][0][-4:] == ['remote', 'set-url', 'origin', developer.RELEASE_REPO]
    assert calls[1][1]['env']['WAM_REF'] == 'my/branch'
    assert calls[1][0][-2:] == [str(ROOT / 'get.sh'), 'start']
    developer.save(tmp_path, {'enabled': False})
    monkeypatch.setattr(updates, 'status', lambda *a, **k: {'retry_target': None, 'checked': True, 'installing': False,
        'unpinned': False, 'update_available': False, 'latest': '0.3.25'})
    assert developer.apply_source(tmp_path, runner)['started']
    command = calls[-1][0][-1]
    assert 'WAM_REF= ' in command and 'WAM_REPO=' + developer.RELEASE_REPO in command
    assert 'WIZARD_RELEASE=0.3.25' in command


def test_root_owner_never_runs_personal_source_as_root(tmp_path, monkeypatch):
    developer.save(tmp_path, {'enabled': True, 'source': {'repo': developer.RELEASE_REPO, 'ref': 'personal'}})
    (tmp_path / 'privsep.json').write_text('{}')
    real_stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda path, *a, **kw: SimpleNamespace(st_uid=1000) if path == tmp_path else real_stat(path, *a, **kw))
    monkeypatch.setattr(developer.os, 'getuid', lambda: 0)
    monkeypatch.setattr(developer.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_name='wizard-system', pw_dir='/var/lib/wizard-os'))
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs)); return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert developer.apply_source(tmp_path, runner)['started']
    for argv, kwargs in calls:
        assert argv[:4] == ['runuser', '-u', 'wizard-system', '--']
        assert kwargs['env']['HOME'] == '/var/lib/wizard-os'
        assert kwargs['env']['WIZARD_SEPARATED'] == '1'
        assert kwargs['env']['WAM_DIR'] == str(ROOT)
    assert calls[-1][0][-2:] == [str(ROOT / 'get.sh'), 'start']


def test_separated_installer_never_reenters_agent_discovery():
    source = (ROOT / 'get.sh').read_text()
    # The root-selected service identity + protected marker, not a caller flag,
    # authorizes the prepared manager path without an agent home or user bus.
    fn = source.split('separated_manager() {', 1)[1].split('\n}', 1)[0]
    assert 'id -un' in fn and 'wizard-system' in fn and '/var/lib/wizard-os/.wizard-os/privsep.json' in fn
    assert 'WIZARD_SEPARATED' not in fn
    assert 'if [ "$MODE" != fresh ] && ! separated_manager; then' in source
    assert 'separated_manager && return 0' in source


def test_developer_approve_page_requires_same_root_peer_owner_and_origin(tmp_path, monkeypatch):
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    from test_privsep_host_policy import request_handler
    from wam import store_install
    request_handler(tmp_path, monkeypatch)
    monkeypatch.setattr(developer, 'helper', lambda *a: {'active': False})
    monkeypatch.setattr(developer, 'apply_source', lambda *a: {'started': True})
    class Handler(cli.StoreHandler): pass
    Handler.cfg = SimpleNamespace(home=tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def req(method, body=None, extra=None):
        headers = {'Tailscale-User-Login': 'owner@example.test', 'Origin': 'https://box.test',
                   'Content-Type': 'application/json', 'X-Wizard-Store': '1', **(extra or {})}
        c = HTTPConnection('127.0.0.1', server.server_port)
        c.request(method, '/_approve/developer', None if body is None else json.dumps(body), headers)
        r = c.getresponse(); data = r.read().decode(); code = r.status; c.close()
        return code, data
    try:
        # Shell origin: no approval site configured -> back-out-only page, risk-raising refused.
        code, page = req('GET')
        assert code == 200 and 'Revoke server access now' in page and 'Give the agent full server access' not in page
        assert req('POST', {'action': 'configure', 'enabled': True,
                            'repo': developer.RELEASE_REPO, 'ref': 'my/branch'})[0] == 403
        assert not developer.read(tmp_path).get('enabled')
        from wam import owner_confirm
        monkeypatch.setattr(owner_confirm, 'verified_origin', lambda *a, **k: 'https://box.test:8446')
        # With one configured, the shell origin still only creates an intent (machine POSTs never execute).
        code, data = req('POST', {'action': 'configure', 'enabled': True, 'repo': developer.RELEASE_REPO, 'ref': 'my/branch'})
        assert code == 409 and json.loads(data)['confirm_url'].startswith('https://box.test:8446/confirm/')
        assert not developer.read(tmp_path).get('enabled')
        assert req('POST', {'action': 'grant'}, {'Origin': 'https://evil.test'})[0] == 403
        monkeypatch.setattr(store_install, 'peer_uid', lambda *a: 1000)
        assert req('POST', {'action': 'grant'})[0] == 403
        assert req('GET')[0] == 403
        monkeypatch.setattr(store_install, 'peer_uid', lambda *a: 0)
        # Backing out stays one click on the shell origin.
        developer.save(tmp_path, {'enabled': True, 'source': {'repo': developer.RELEASE_REPO, 'ref': 'my/branch'}})
        assert req('POST', {'action': 'configure', 'enabled': False})[0] == 200
        assert not developer.read(tmp_path)['enabled']
        assert req('POST', {'action': 'revoke'})[0] == 200
    finally: server.shutdown(); thread.join(); server.server_close()


def test_owner_mode_never_captured_or_replaced_by_backup():
    from wam.backup.box import PER_MACHINE, NEVER_CAPTURE
    for item in ('developer.json', 'developer.lock', 'developer.tmp'):
        assert item in PER_MACHINE and item in NEVER_CAPTURE


def test_api_doctor_has_red_severity(tmp_path, monkeypatch):
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(cli.Handler, 'cfg', SimpleNamespace(home=tmp_path), raising=False)
    monkeypatch.setattr(cli, 'doctor', lambda *a: [('Agent has full server access until tomorrow', False, 'root')])
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection('127.0.0.1', server.server_port); conn.request('GET', '/api/doctor')
        response = conn.getresponse(); body = json.loads(response.read()); conn.close()
        assert body[0]['severity'] == 'red'
    finally: server.shutdown(); server.server_close()
