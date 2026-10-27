"""David's no-sudo account: finite API authority and no local fallbacks."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from wam import cli, client, privsep, alongside


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = SimpleNamespace(home=tmp_path)
    calls = []
    class Handler(cli.Handler):
        pass
    Handler.cfg = cfg
    Handler.token = 's' * 43
    Handler.agent_token = 'a' * 43
    monkeypatch.setattr(cli, 'write_registry', lambda c: {'apps': []})
    monkeypatch.setattr(cli, 'install_with_retry', lambda c, app, form: calls.append(('install', app)) or {'status': 'running'})
    monkeypatch.setattr(cli, 'uninstall', lambda c, app: calls.append(('uninstall', app)) or {'status': 'removed'})
    monkeypatch.setattr(cli, 'app_lifecycle', lambda c, verb, app: calls.append((verb, app)) or {'status': 'running'})
    monkeypatch.setattr(cli, 'app_logs', lambda c, app, tail: {'logs': 'hello', 'containers': 1})
    http = cli.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    def req(method, path, body=None, token='a' * 43, headers=None):
        request = Request(f'http://127.0.0.1:{http.server_port}' + path,
                          data=None if body is None else json.dumps(body).encode(), method=method,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', **(headers or {})})
        try:
            with urlopen(request, timeout=5) as r: return r.status, json.load(r)
        except HTTPError as e: return e.code, json.load(e)
    yield req, calls
    http.shutdown(); thread.join(); http.server_close()


def test_agent_scope_never_owner_scope_even_forged_identity(server):
    req, calls = server
    from wam.owner import PATHS
    for path in PATHS:
        status, result = req('POST', path, {}, headers={'Tailscale-User-Login': 'owner@example.test'})
        assert status == 403 and result['error'] == 'owner_only'
    assert req('POST', '/api/bootstrap', {})[0] == 403
    assert calls == []


def test_named_lifecycle_and_logs(server):
    req, calls = server
    assert req('GET', '/api/apps')[0] == 200
    assert req('GET', '/api/apps', token='bad')[0] == 401
    assert req('POST', '/api/install', {'app': 'hello-wizard'})[0] == 200
    for verb in ('start', 'stop', 'restart'):
        assert req('POST', '/api/app/' + verb, {'app': 'hello-wizard'})[0] == 200
    assert req('GET', '/api/apps/hello-wizard/logs')[1]['logs'] == 'hello'
    assert req('DELETE', '/api/apps/hello-wizard')[0] == 200
    for app in ('../etc', 'x:y', '-v', 'https://github.com/foo/bar', ''):
        assert req('POST', '/api/install', {'app': app})[0] == 400
    assert req('DELETE', '/api/apps/hello-wizard/extra')[0] == 404


def test_server_token_keeps_legacy_urn_lifecycle(server):
    req, calls = server
    assert req('POST', '/api/app/restart', {'app': 'hello-wizard:wizardapps'}, token='s' * 43)[0] == 200
    assert calls[-1] == ('restart', 'hello-wizard:wizardapps')
    assert req('POST', '/api/app/restart', {'app': 'hello-wizard:wizardapps'})[0] == 400


def test_thin_client_fails_closed_without_local_fallback(monkeypatch, capsys):
    def fail(*args, **kwargs): raise AssertionError('no subprocess fallback')
    monkeypatch.setattr(subprocess, 'run', fail)
    for verb in ('backup', 'links', 'builder', 'approve', 'grants', 'network', 'serve', 'bootstrap'):
        assert client.main([verb]) == 2
        assert 'not yet available without server access' in capsys.readouterr().err


def test_client_will_not_send_token_to_remote_origin(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_OS_HOME', str(tmp_path))
    (tmp_path / 'agent-token').write_text('a' * 43)
    for url in ('https://evil.test', 'http://127.0.0.1:7790@evil.test', 'http://127.0.0.1:7790/path', 'http://localhost:7790'):
        (tmp_path / 'client.json').write_text(json.dumps({'url': url}))
        with pytest.raises(ValueError): client.settings()


def test_separated_service_runs_at_agent_uid_without_privileged_credentials(tmp_path, monkeypatch):
    (tmp_path / 'privsep.json').write_text('{}')
    st = {'agent': {'uid': 1001, 'gid': 1001, 'hermes_home': '/home/hermes/.hermes', 'origin': 'http://127.0.0.1:8642'},
          'service': {'port': 8923}, 'images': {}}
    def forbidden(*args, **kwargs): raise AssertionError('system must not inspect agent config')
    monkeypatch.setattr(alongside, 'api_key', forbidden)
    monkeypatch.setattr(alongside, 'load', lambda home: st)
    assert alongside.shared_secret(tmp_path, 'WIZARD_HERMES_API_KEY') is None
    text = alongside.shell_override(st, 'wizard-agent-service', tmp_path, 'service', prepare=False)
    assert 'user: "1001:1001"' in text
    assert 'cap_drop: [ALL]' in text and 'no-new-privileges:true' in text
    assert '/var/run/docker.sock' not in text and 'api-token' not in text
    assert 'WIZARD_PRIVSEP_LINK: /state/link.json' in text
    assert '/registry:ro' in text


def test_builder_bundle_entrypoint_is_still_thin_on_separated_account(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('WIZARD_OS_HOME', str(tmp_path))
    (tmp_path / 'client.json').write_text('{}')
    monkeypatch.setattr(cli, 'Config', lambda: (_ for _ in ()).throw(AssertionError('no local manager config')))
    assert cli.main(['builder-bundle', 'fresh']) == 2
    assert 'not yet available without server access' in capsys.readouterr().err


def test_new_layout_source_paths_and_units_are_protected():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'scripts/prepare-alongside.py').read_text()
    assert "BASE = Path('/var/lib/wizard-os')" in text
    assert 'User=wizard-system' in text
    assert "'usermod', '-aG', 'wizard-files', name" in text
    assert "'usermod', '-aG', 'docker', name" not in text
    assert "'runuser', '-u', name" in text
    assert 'root never opens a file in the agent home' in text
