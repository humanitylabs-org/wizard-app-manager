"""Synthetic regression coverage; no host Hermes history or provider tokens are read."""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import cli, links, resources, slots


def test_installer_keeps_database_sharing_opt_in():
    script = (Path(__file__).parents[1] / 'install.sh').read_text()
    assert '"$HOME/.local/bin/wam" alongside --share-sessions ' not in script
    assert '"$HOME/.local/bin/wam" alongside --share-history' not in script


def test_multi_provider_release_default_is_additive():
    defaults = {'ai-connection': {'app': 'wizard-chatgpt', 'multi': True},
                'browser': {'app': 'browser'}}
    reg = {'apps': [{'id': 'wizard-claude', 'provides': ['ai-connection']},
                    {'id': 'other-browser', 'provides': ['browser']}]}
    assert slots.to_install(reg, defaults) == ['wizard-chatgpt']
    reg['apps'].append({'id': 'wizard-chatgpt', 'provides': ['ai-connection']})
    assert slots.to_install(reg, defaults) == []


def test_resource_projection_is_numeric_and_exact():
    calls = []
    def req(*args, **kwargs):
        calls.append((args, kwargs))
        return dict(cpuLoad=12, percentUsedMemory=42.5, percentUsed=True,
                    memoryTotal='8', diskUsed=float('nan'), diskSize=float('inf'),
                    JWT_SECRET='synthetic-never-return')
    got = resources.read(SimpleNamespace(req=req))
    assert got == dict(cpuLoad=12, percentUsedMemory=42.5, percentUsed=None,
                       memoryTotal=None, diskUsed=None, diskSize=None)
    assert calls == [(('GET', '/system/load'), {'timeout': 8})]
    assert 'synthetic' not in json.dumps(got)


def test_resource_http_payload_and_denials(tmp_path):
    def req(*args, **kwargs):
        return {**{k: i + 1 for i, k in enumerate(resources.FIELDS)}, 'password': 'synthetic-private'}
    class Handler(cli.Handler):
        cfg = SimpleNamespace(runtipi=lambda: SimpleNamespace(req=req))
        token = 'synthetic-api-token'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        with urllib.request.urlopen(base + resources.PATH) as r:
            got = json.load(r)
            assert r.headers['Cache-Control'] == 'no-store'
            assert got == {k: i + 1 for i, k in enumerate(resources.FIELDS)}
        for path, method, headers, status in [
            (resources.PATH, 'GET', {'Sec-Fetch-Site': 'cross-site'}, 403),
            (resources.PATH, 'POST', {}, 401),
            ('/api/system/arbitrary', 'GET', {}, 404),
        ]:
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + path, method=method, headers=headers))
            assert e.value.code == status
        def failed():
            raise RuntimeError('synthetic-private-credential')
        Handler.cfg = SimpleNamespace(runtipi=failed)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + resources.PATH)
        assert e.value.code == 503
        assert json.load(e.value) == {'error': 'server_resources_unavailable'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_resource_route_is_exact_preserves_routes_and_reads_back(monkeypatch):
    config = {'TCP': {'8796': {'HTTPS': True}}, 'Web': {'test.ts.net:8796': {'Handlers': {
        '/': {'Proxy': 'http://127.0.0.1:8796'}, '/files': {'Proxy': 'http://127.0.0.1:9999'}}}}}
    monkeypatch.setattr(links, 'serve_status', lambda: config)
    calls = []
    def run(args, timeout):
        calls.append(args)
        config['Web']['test.ts.net:8796']['Handlers'][resources.PATH] = {'Proxy': args[-1]}
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(links, '_run', run)
    assert resources.publish('test.ts.net', 8796, 7790)['state'] == 'ready'
    assert calls == [['tailscale', 'serve', '--bg', '--https=8796', '--set-path=/api/system/load',
                      'http://127.0.0.1:7790/api/system/load']]
    resources.publish('test.ts.net', 8796, 7790)
    assert len(calls) == 1
    assert config['Web']['test.ts.net:8796']['Handlers']['/files']['Proxy'].endswith(':9999')
    config['Web']['test.ts.net:8796']['Handlers'][resources.PATH] = {'Proxy': 'http://owner.invalid'}
    with pytest.raises(ValueError, match='left untouched'):
        resources.publish('test.ts.net', 8796, 7790)
    assert len(calls) == 1


def test_resource_route_readback_and_funnel_fail_closed(monkeypatch):
    monkeypatch.setattr(links, 'serve_status', lambda: {})
    monkeypatch.setattr(links, '_run', lambda *a: SimpleNamespace(returncode=0))
    with pytest.raises(ValueError, match='readback'):
        resources.publish('test.ts.net', 8796, 7790)
    monkeypatch.setattr(links, 'serve_status', lambda: {'AllowFunnel': {'test.ts.net:8796': True}})
    with pytest.raises(ValueError, match='left untouched'):
        resources.publish('test.ts.net', 8796, 7790)


def test_resource_disk_fields_come_from_the_host_when_the_manager_reports_zero(tmp_path, monkeypatch):
    """Live 0.3.6 named-user box: Runtipi's container sees no host disk and returns diskSize 0 / diskUsed 0."""
    zero = dict(cpuLoad=12.0, percentUsedMemory=25, percentUsed=0, memoryTotal=8, diskUsed=0, diskSize=0)
    rt = SimpleNamespace(req=lambda *a, **k: dict(zero))
    gib = 1024 ** 3
    monkeypatch.setattr(resources.shutil, 'disk_usage',
                        lambda p: SimpleNamespace(total=96 * gib, used=12 * gib, free=84 * gib))
    got = resources.read(rt, tmp_path)
    assert got == dict(cpuLoad=12.0, percentUsedMemory=25, percentUsed=12, memoryTotal=8, diskUsed=12, diskSize=96)
    assert resources.read(rt) == zero   # no host path: the manager's numbers are passed through unchanged
    real = dict(zero, diskUsed=5, diskSize=50, percentUsed=10)
    assert resources.read(SimpleNamespace(req=lambda *a, **k: dict(real)), tmp_path) == real
