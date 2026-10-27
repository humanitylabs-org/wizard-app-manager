"""David's privilege-separation audit: executable recipes never confer consent."""
import io
import json
import tarfile
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from wam import cli, host_policy as policy, owner, store_install
from test_network import gate_env, update_env


def compose(**fields):
    return yaml.safe_dump({'services': {'audit': {'image': 'example/audit', **fields}}})


def test_recipe_mounting_host_root_needs_owner(tmp_path):
    text = compose(volumes=['/:/host'])
    with pytest.raises(policy.NeedsOwnerApproval) as exc:
        policy.require(tmp_path, 'audit', [text])
    assert exc.value.payload['error'] == 'needs_owner_approval'
    assert 'outside app-data' in exc.value.payload['risks'][0]
    assert exc.value.payload['command'] == f'wam approve audit {policy.digest([text])}'


@pytest.mark.parametrize('fields,reason', [
    ({'privileged': True}, 'privileged'),
    ({'volumes': ['/var/run/docker.sock:/sock']}, 'runtime socket'),
    ({'build': {'context': '.', 'secrets': ['key']}}, 'build.secrets'),
    ({'build': {'context': '.', 'ssh': ['default']}}, 'build.ssh'),
    ({'build': {'context': '/'}}, 'build context'),
    ({'build': {'context': '.', 'dockerfile': '/etc/server.Dockerfile'}}, 'Dockerfile'),
    ({'env_file': '/etc/server.env'}, 'env_file'),
    ({'pid': 'host'}, 'pid'), ({'ipc': 'host'}, 'ipc'),
    ({'userns_mode': 'host'}, 'userns_mode'), ({'network_mode': 'host'}, 'network_mode'),
    ({'cap_add': ['NET_ADMIN']}, 'cap_add'), ({'devices': ['/dev/sda']}, 'devices'),
    ({'security_opt': ['seccomp=unconfined']}, 'security_opt'),
    ({'sysctls': {'net.ipv4.ip_forward': 1}}, 'sysctls'),
    ({'extends': {'file': '/etc/compose.yml', 'service': 'x'}}, 'not allowlisted'),
])
def test_david_privileged_docker_sock_build_secrets_env_file(fields, reason):
    assert any(reason in r for r in policy.risks(compose(**fields), 'audit'))


@pytest.mark.parametrize('kind', ['secrets', 'configs'])
def test_file_backed_definitions_need_owner(kind):
    doc = {'services': {'audit': {'image': 'x', kind: ['key']}}, kind: {'key': {'file': '/etc/key'}}}
    assert policy.risks(doc, 'audit')


@pytest.mark.parametrize('mount', ['${APP_DATA_DIR}/../../../../:/host', '${ROOT_FOLDER_HOST}:/host',
                                  '../../../:/host', {'type': 'bind', 'source': '/', 'target': '/host'}])
def test_mount_expansion_long_form_and_parent_escape_need_owner(mount):
    assert policy.risks(compose(volumes=[mount]), 'audit')


def test_own_app_data_files_named_volumes_and_resolved_relative_mounts_pass(tmp_path):
    root = tmp_path / 'runtipi'
    data = root / 'app-data/wizardapps/audit'
    data.mkdir(parents=True)
    base = root / 'apps/wizardapps/audit'
    mounts = ['${APP_DATA_DIR}/data:/data', '${ROOT_FOLDER_HOST}/media:/media', 'private:/named',
              '../../../app-data/wizardapps/audit/data:/relative',
              {'type': 'bind', 'source': '${APP_DATA_DIR}/db', 'target': '/db'}]
    assert not policy.risks(compose(volumes=mounts), 'audit', root=root, base=base)
    (data / 'escape').symlink_to('/')
    assert policy.risks(compose(volumes=['${APP_DATA_DIR}/escape:/host']), 'audit', root=root)


def test_allow_host_access_flag_no_longer_approves(tmp_path, monkeypatch):
    c, installs, steps = gate_env(tmp_path, monkeypatch, compose(volumes=['/:/host']))
    with pytest.raises(policy.NeedsOwnerApproval):
        c.install(c.Config(), 'x', allow_host_access=True)
    assert not installs and not steps
    assert policy._read(c.Config().home)['approvals'] == {}
    code = c.main(['install', 'x', '--allow-host-access'])
    assert code == policy.EXIT_APPROVAL


def test_approval_invalidated_by_recipe_change(tmp_path, monkeypatch):
    c, installs, steps = gate_env(tmp_path, monkeypatch, compose(privileged=True))
    with pytest.raises(policy.NeedsOwnerApproval) as e:
        c.install(c.Config(), 'x')
    sha = e.value.payload['approve_url'].split('/')[-1]
    policy.approve(c.Config().home, 'x', sha)
    assert policy._read(c.Config().home)['approvals']['x'] == sha
    path = c.Config().runtipi_root / 'repos/wizardapps/apps/x/docker-compose.yml'
    path.write_text(path.read_text() + '# changed recipe\n')
    with pytest.raises(policy.NeedsOwnerApproval):
        c.install(c.Config(), 'x', allow_host_access=True)
    assert installs == []
    with pytest.raises(ValueError):
        policy.approve(c.Config().home, 'x', sha)


def test_same_risk_on_changed_update_needs_fresh_owner(tmp_path, monkeypatch):
    plain = compose(privileged=True)
    c, updates, steps = update_env(tmp_path, monkeypatch, plain, plain + '# v2\n')
    with pytest.raises(policy.NeedsOwnerApproval):
        c.app_update(c.Config(), 'x', allow_host_access=True)
    assert updates == steps == []


def test_restored_override_with_a_host_mount_needs_owner(tmp_path):
    from wam.backup import restore
    stage = tmp_path / 'stage'
    (stage / 'package').mkdir(parents=True)
    (stage / 'package/docker-compose.yml').write_text(compose())
    override = compose(volumes=['/:/host']).encode()
    with tarfile.open(stage / 'data.tar.gz', 'w:gz') as tar:
        member = tarfile.TarInfo('user-config/docker-compose.yml')
        member.size = len(override)
        tar.addfile(member, io.BytesIO(override))
    box = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'rt')
    rec = {'id': 'audit', 'store': 'wizardapps', 'urn': 'audit:wizardapps'}
    with pytest.raises(policy.NeedsOwnerApproval):
        restore._check_restore_recipe(box, rec, stage)


def request_handler(tmp_path, monkeypatch, uid=0, login='owner@example.test'):
    monkeypatch.setattr(store_install, 'peer_uid', lambda *a: uid)
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'owner@example.test')
    monkeypatch.setattr(store_install, 'served_origins', lambda host: {'https://box.test'})
    (tmp_path / 'links.json').write_text('{"host":"box.test"}')
    return SimpleNamespace(cfg=SimpleNamespace(home=tmp_path), client_address=('127.0.0.1', 12345),
        server=SimpleNamespace(server_address=('127.0.0.1', 7791)),
        headers={'Tailscale-User-Login': login, 'Origin': 'https://box.test',
                 'X-Wizard-Store': '1', 'Content-Type': 'application/json'})


def test_forged_owner_header_from_non_root_peer_refused(tmp_path, monkeypatch):
    assert owner.check(request_handler(tmp_path, monkeypatch, uid=1000), post=True)


def test_root_peer_with_wrong_login_refused(tmp_path, monkeypatch):
    assert owner.check(request_handler(tmp_path, monkeypatch, login='attacker@example.test'), post=True)


def test_tagged_node_refuses_owner_even_with_user_id(monkeypatch):
    monkeypatch.setattr(store_install.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps({
        'Self': {'UserID': 1, 'Tags': ['tag:server']}, 'User': {'1': {'LoginName': 'owner@example.test'}}})))
    assert store_install.owner_login() == ''


def test_owner_pin_change_fails_closed(tmp_path, monkeypatch):
    handler = request_handler(tmp_path, monkeypatch)
    (tmp_path / 'owner-login').write_text('previous@example.test\n')
    assert 'changed' in owner.check(handler)


def test_owner_approval_http_and_readback(tmp_path, monkeypatch):
    text = compose(privileged=True)
    with pytest.raises(policy.NeedsOwnerApproval) as e:
        policy.require(tmp_path, 'audit', [text])
    sha = policy.digest([text])
    request_handler(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.StoreHandler, 'cfg', SimpleNamespace(home=tmp_path), raising=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.StoreHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def req(method, tail='', extra=None):
        c = HTTPConnection('127.0.0.1', server.server_port)
        headers = {'Tailscale-User-Login': 'owner@example.test', 'Origin': 'https://box.test',
                   'Content-Type': 'application/json', 'X-Wizard-Store': '1', **(extra or {})}
        c.request(method, '/_approve/audit/' + sha + tail, '{}' if method == 'POST' else None, headers)
        res = c.getresponse(); data = res.read(); c.close()
        return res.status, data
    try:
        status, page = req('GET')
        assert status == 200 and b'Approve this recipe' in page and sha.encode() in page
        assert policy._read(tmp_path)['approvals'] == {}
        assert req('POST', extra={'Origin': 'https://evil.test'})[0] == 403
        assert req('POST')[0] == 200
        assert policy._read(tmp_path)['approvals'] == {'audit': sha}
        assert policy.require(tmp_path, 'audit', [text]) == sha
        monkeypatch.setattr(store_install, 'peer_uid', lambda *a: 1000)
        assert req('POST')[0] == 403
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.parametrize('path', sorted(owner.PATHS))
def test_agent_api_cannot_use_owner_verbs(tmp_path, monkeypatch, path):
    request_handler(tmp_path, monkeypatch, uid=1000)
    monkeypatch.setattr(cli.Handler, 'cfg', SimpleNamespace(home=tmp_path), raising=False)
    monkeypatch.setattr(cli.Handler, 'token', 'agent-test-token', raising=False)
    monkeypatch.setattr(owner, 'execute', lambda *a: pytest.fail('agent reached owner verb'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = HTTPConnection('127.0.0.1', server.server_port)
        c.request('POST', path, '{}', {'Authorization': 'Bearer agent-test-token', 'Content-Type': 'application/json',
                                    'Tailscale-User-Login': 'owner@example.test'})
        res = c.getresponse(); assert res.status == 403; res.read(); c.close()
    finally:
        server.shutdown(); server.server_close()


def test_builder_output_checked_before_compose_up(tmp_path, monkeypatch):
    from wam import builder
    source = builder.new_app('audit-app', tmp_path / 'source', 'Audit builder admission')
    dump = builder.yaml.safe_dump
    def compromised_output(doc, *a, **kw):
        if 'services' in doc and 'audit-app' in doc['services']:
            doc['services']['audit-app']['privileged'] = True
        return dump(doc, *a, **kw)
    monkeypatch.setattr(builder.yaml, 'safe_dump', compromised_output)
    calls = []
    real_run = builder.subprocess.run
    def no_docker(argv, **kw):
        if argv[0] == 'docker':
            calls.append(argv)
            pytest.fail('unsafe builder output reached Docker')
        return real_run(argv, **kw)
    monkeypatch.setattr(builder.subprocess, 'run', no_docker)
    with pytest.raises(policy.NeedsOwnerApproval):
        builder.install_local(source, os_home=tmp_path / 'home', run=True, stage_only=True)
    assert calls == []


def test_api_returns_409_shape_for_risky_recipe(tmp_path, monkeypatch):
    c, installs, steps = gate_env(tmp_path, monkeypatch, compose(volumes=['/:/host']))
    monkeypatch.setattr(cli.Handler, 'cfg', c.Config(), raising=False)
    monkeypatch.setattr(cli.Handler, 'token', 'agent-test-token', raising=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection('127.0.0.1', server.server_port)
        conn.request('POST', '/api/install', json.dumps({'app': 'x', 'allow_host_access': True}),
                     {'Authorization': 'Bearer agent-test-token', 'Content-Type': 'application/json'})
        result = conn.getresponse(); body = json.loads(result.read()); conn.close()
        assert result.status == 409
        assert body['error'] == 'needs_owner_approval' and body['app'] == 'x'
        assert body['risks'] and body['approve_url'].startswith(policy.PREFIX)
        assert installs == steps == []
    finally:
        server.shutdown(); server.server_close()


def test_changed_mount_variable_needs_fresh_approval(tmp_path):
    text = compose(volumes=['${HOST_DATA:-/etc}:/host'])
    with pytest.raises(policy.NeedsOwnerApproval) as exc:
        policy.require(tmp_path, 'audit', [text], variables={'HOST_DATA': '/etc'})
    policy.approve(tmp_path, 'audit', exc.value.payload['approve_url'].split('/')[-1])
    with pytest.raises(policy.NeedsOwnerApproval):
        policy.require(tmp_path, 'audit', [text], variables={'HOST_DATA': '/'})


def test_forged_network_marker_does_not_exempt_foreign_network(tmp_path):
    text = """services:
  audit: {networks: [foreign]}
networks:
  foreign: {name: wizard-core, external: true}
x-wizard-networks:
  services: [audit]
  networks: [foreign]
"""
    c = SimpleNamespace(home=tmp_path, runtipi_root=tmp_path / 'rt')
    doc = policy.normalized_override(c, 'audit', 'wizardapps', text)
    assert policy.risks(doc, 'audit')


def test_async_store_refusal_preserves_409_payload(monkeypatch):
    exc = policy.NeedsOwnerApproval('audit', 'a' * 64, ['host root'])
    monkeypatch.setitem(store_install._jobs, 'audit', {'state': 'needs_owner_approval', **exc.payload})
    code, body = store_install.status('audit')
    assert code == 409 and body['error'] == 'needs_owner_approval'
    assert body['approve_url'] == exc.payload['approve_url']


def test_store_audit_all_default_recipes_pass():
    root = Path(__file__).resolve().parents[1] / 'qa/privsep-slice2'
    rows = json.loads((root / 'sources.json').read_text())
    for row in rows:
        if row['path']:
            found = policy.risks((root / 'recipes' / (row['key'] + '.yml')).read_text(), row['app'])
            assert not found, (row['app'], found)
    assert sum(row['key'].startswith('store-') for row in rows) == 8
