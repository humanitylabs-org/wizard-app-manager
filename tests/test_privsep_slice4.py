"""Agent-token admission and data-only build staging, without privileged fixtures."""
import base64
import copy
import json
import os
import stat
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import agent_actions as actions, builder, cli, client, host_policy, links, owner
from wam.backup import box as backup_box, cli as backup_cli, restore


@pytest.fixture
def api(tmp_path, monkeypatch):
    cfg = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'rt')
    cfg.home.mkdir()
    monkeypatch.setattr(cli.Handler, 'cfg', cfg, raising=False)
    monkeypatch.setattr(cli.Handler, 'token', 'server-token', raising=False)
    monkeypatch.setattr(cli.Handler, 'agent_token', 'agent-token', raising=False)
    monkeypatch.setattr(cli, 'write_registry', lambda c: {'apps': []})
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def req(path, body, token='agent-token'):
        con = HTTPConnection('127.0.0.1', server.server_port)
        con.request('POST', path, json.dumps(body), {'Authorization': 'Bearer ' + token,
                    'Content-Type': 'application/json', 'Tailscale-User-Login': 'owner@example.test'})
        response = con.getresponse()
        result = response.status, json.loads(response.read())
        con.close()
        return result
    yield cfg, req
    server.shutdown(); server.server_close()


@pytest.mark.parametrize('action', ['run', 'snapshots', 'restore', 'undo'])
def test_agent_token_uses_only_box_backup_repository(api, monkeypatch, action):
    cfg, req = api
    calls = []
    def dispatch(args, box):
        calls.append(args)
        assert Path(box.home) == cfg.home and Path(box.runtipi_root) == cfg.runtipi_root
        return {'ok': True}
    monkeypatch.setattr(backup_cli, '_dispatch', dispatch)
    body = {'snapshot': 'ab12cd34'} if action == 'restore' else {}
    assert req('/api/backup/' + action, body) == (200, {'ok': True})
    assert len(calls) == 1
    if action == 'restore':
        assert calls[0].from_repo == 'ab12cd34' and calls[0].file is None and calls[0].yes
    assert req('/api/backup/' + action, {**body, 'repo': '/etc'})[0] == 400
    assert req('/api/backup/' + action, body, token='invalid')[0] == 401


def test_agent_external_file_import_refused_before_owner_proof(api, monkeypatch):
    _, req = api
    monkeypatch.setattr(owner, 'execute', lambda *a: pytest.fail('agent reached owner import'))
    assert req('/api/backup/import', {'file': '/tmp/backup.wizard'}) == (403, {'error': 'owner_only'})


def test_restore_from_repo_host_mount_returns_approval_before_mutation(api, monkeypatch, tmp_path):
    _, req = api
    from test_privsep_host_policy import test_restored_override_with_a_host_mount_needs_owner
    def dispatch(args, box):
        assert args.cmd == 'import' and args.from_repo == 'latest'
        test_restored_override_with_a_host_mount_needs_owner(tmp_path)
        # Exercise the actual policy exception across HTTP as the repo engine does.
        host_policy.require(box.home, 'audit', ['services: {audit: {image: x, volumes: ["/:/host"]}}'])
    monkeypatch.setattr(backup_cli, '_dispatch', dispatch)
    status, result = req('/api/backup/restore', {'snapshot': 'latest'})
    assert status == 409 and result['error'] == 'needs_owner_approval'


@pytest.mark.parametrize('action', ['publish', 'unpublish'])
def test_agent_links_reach_service_operator_not_client(api, monkeypatch, action):
    cfg, req = api
    monkeypatch.setattr(cli, 'write_registry', lambda c: {'apps': [
        {'id': 'audit', 'status': 'running', 'view': {'url': 'http://127.0.0.1:18799/'}}]})
    monkeypatch.setattr(links, 'tailnet', lambda: {'state': 'NeedsLogin', 'host': ''})
    status, body = req('/api/links/' + action, {'app': 'audit'})
    assert status == 200 and body['operator_uid'] == os.getuid() and body['state'] == 'NeedsLogin'
    assert body['published'] is False
    assert req('/api/links/' + action, {'app': 'missing'})[0] == 400
    assert req('/api/links/' + action, {'app': 'audit', 'port': 22})[0] == 400


def test_private_link_exact_readback_and_unpublish_preserves_owner_route(tmp_path, monkeypatch):
    host = 'box.test'
    config = {'TCP': {'18799': {'HTTPS': True}}, 'Web': {host + ':18799': {'Handlers': {
        '/_approve': {'Proxy': 'http://127.0.0.1:7791'}}}}}
    monkeypatch.setattr(links, 'tailnet', lambda: {'state': 'Running', 'host': host})
    monkeypatch.setattr(links, 'serve_status', lambda: copy.deepcopy(config))
    calls = []
    def run(argv, timeout):
        calls.append(argv)
        assert '--https=18799' in argv and '--set-path=/' in argv
        handlers = config['Web'][host + ':18799']['Handlers']
        if argv[-1] == 'off': handlers.pop('/')
        else: handlers['/'] = {'Proxy': argv[-1]}
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(links, '_run', run)
    reg = {'apps': [{'id': 'audit', 'status': 'running', 'view': {'url': 'http://127.0.0.1:18799/'}}]}
    c = SimpleNamespace(home=tmp_path)
    assert actions.private_link(c, reg, 'audit', True)['published']
    assert not actions.private_link(c, reg, 'audit', False)['published']
    assert config['Web'][host + ':18799']['Handlers'] == {'/_approve': {'Proxy': 'http://127.0.0.1:7791'}}
    assert json.loads((tmp_path / 'links.json').read_text())['unpublished'] == ['audit']
    assert links.publish(reg, tmp_path, apply=False)['links'] == []
    assert len(calls) == 2


@pytest.fixture
def source(tmp_path):
    return builder.new_app('slice4-app', tmp_path / 'source', 'Slice four secure source')


@pytest.mark.parametrize('action', ['install-local', 'update-local', 'check'])
def test_agent_build_upload_uses_service_owned_snapshot(api, source, monkeypatch, action):
    cfg, req = api
    calls = []
    def check(path, **kw):
        path = Path(path)
        calls.append(path)
        assert path != source and cfg.home / 'build-work' in path.parents
        assert path.stat().st_uid == os.getuid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert (path / 'wizard.md').read_bytes() == (source / 'wizard.md').read_bytes()
        assert not any(p.is_symlink() for p in path.rglob('*'))
        return {'ok': True}
    monkeypatch.setattr(builder, 'install_local', check)
    monkeypatch.setattr(builder, 'update_local', check)
    monkeypatch.setattr(builder, 'check_app', check)
    assert req('/api/builder/' + action, {'files': client.source_bundle(source)}) == (200, {'ok': True})
    assert not calls[0].exists()  # temporary upload removed after the builder has copied it


@pytest.mark.parametrize('target', ['/', '/etc'])
def test_source_symlink_escape_refused_before_request(source, target):
    (source / 'escape').symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        client.source_bundle(source)


@pytest.mark.parametrize('name', ['../escape', '/etc/passwd', 'a/../../escape', 'a//b', './a', 'a\\b'])
def test_raw_upload_cannot_escape_snapshot(api, name):
    _, req = api
    assert req('/api/builder/install-local', {'files': [{'path': name, 'data': 'eA=='}]})[0] == 400


def test_upload_link_metadata_and_oversize_refused(api, source, monkeypatch):
    _, req = api
    assert req('/api/builder/install-local', {'files': [{'path': 'escape', 'symlink': '/'}]})[0] == 400
    monkeypatch.setattr(actions, 'SOURCE_LIMIT', 1)
    assert req('/api/builder/install-local', {'files': client.source_bundle(source)})[0] == 400


def test_uploaded_compose_mounting_host_root_needs_owner(api, source, monkeypatch):
    _, req = api
    (source / 'compose.yml').write_text('services: {slice4-app: {image: x, volumes: ["/:/host"]}}')
    monkeypatch.setattr(builder, 'install_local', lambda *a, **kw: pytest.fail('unsafe source reached builder'))
    status, body = req('/api/builder/install-local', {'files': client.source_bundle(source), 'run': True})
    assert status == 409 and body['error'] == 'needs_owner_approval'


def test_real_builder_checks_generated_output_before_any_docker(api, source, monkeypatch):
    cfg, req = api
    calls = []
    real_run = builder.subprocess.run
    def run(argv, **kw):
        if argv[0] == 'docker':
            calls.append(argv)
            return SimpleNamespace(returncode=0)
        return real_run(argv, **kw)
    monkeypatch.setattr(builder.subprocess, 'run', run)
    status, body = req('/api/builder/install-local', {'files': client.source_bundle(source), 'run': True})
    assert status == 200, body
    stage = cfg.home / 'staged-apps' / 'slice4-app'
    assert stage.is_dir() and Path(body['compose']).is_file() and calls
    assert stage.stat().st_uid == os.getuid() and not stage.stat().st_mode & 0o022
    assert all(not p.is_symlink() for p in stage.rglob('*'))


def test_separated_backup_never_executes_agent_writable_hermes(tmp_path, monkeypatch):
    (tmp_path / 'privsep.json').write_text('{}')
    monkeypatch.setattr(backup_box.alongside, 'load', lambda home: {'agent': {'hermes_home': '/agent-owned'}})
    box = backup_box.Box(home=tmp_path, runtipi_root=tmp_path / 'rt')
    assert not box.agents({})
    with pytest.raises(backup_box.HermesError, match='never executed'):
        box.runner.host_binary()
    assert {'agent-token', 'privsep.json', 'client.json'} <= backup_box.PER_MACHINE
    assert 'build-work' in backup_box.NEVER_CAPTURE


def test_thin_client_hermes_update_check_is_local(monkeypatch):
    calls = []
    monkeypatch.setattr(client.subprocess, 'run', lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(client, 'request', lambda *a: pytest.fail('own Hermes must never run on server'))
    assert client.main(['hermes', 'update', '--check']) == 0
    assert calls == [['hermes', '--version']]
