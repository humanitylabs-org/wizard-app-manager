"""Explicit links refresh applies installed-app trust; all state is synthetic."""
import json
from types import SimpleNamespace

import pytest

from wam import cli, links, store_data
from wam.runtipi import Runtipi, read_env


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    home, root = tmp_path / 'home', tmp_path / 'rt'
    home.mkdir()
    recipe = root / 'apps/wizardapps/demo'
    recipe.mkdir(parents=True)
    (recipe / 'docker-compose.yml').write_text(
        'services:\n  demo:\n    environment:\n'
        '      WIZARD_TRUSTED_HOSTS: ${WIZARD_TRUSTED_HOSTS:-}\n'
        '      WIZARD_TRUSTED_ORIGINS: ${WIZARD_TRUSTED_ORIGINS:-}\n')
    (home / 'links.json').write_text(json.dumps({'host': 'box.tail.ts.net'}))
    env = root / 'user-config/wizardapps/demo/app.env'
    env.parent.mkdir(parents=True)
    env.write_text('# owner configuration\nOTHER=keep\nWIZARD_OWNER_LOGINS=owner@example.test\n'
                   'WIZARD_TRUSTED_HOSTS=custom.test,box.tail.ts.net:8796\n'
                   'WIZARD_TRUSTED_ORIGINS=https://custom.test,https://box.tail.ts.net:8796\n')
    state = {'ports': [8796, 8444], 'status': 'running', 'fail': False}
    calls = []
    def request(method, path, body=None, **kwargs):
        assert method == 'POST' and path == '/app-lifecycle/demo:wizardapps/restart'
        calls.append(body)
        # Model container creation consuming user-config; this is not live Docker evidence.
        state['runtime'] = read_env(env)
        if state['fail']:
            raise RuntimeError('synthetic restart failure')
        return {}
    rt = Runtipi('http://unused.test', 'synthetic')
    monkeypatch.setattr(rt, 'req', request)
    monkeypatch.setattr(rt, 'app', lambda urn: {'app': {'status': state['status'], 'port': 8801}})
    monkeypatch.setattr(rt, 'wait_status', lambda *a: 'running')
    monkeypatch.setattr(cli.time, 'sleep', lambda *a: None)
    reg = {'apps': [{'id': 'demo', 'urn': 'demo:wizardapps', 'status': 'running'}]}
    c = SimpleNamespace(home=home, runtipi_root=root, runtipi=lambda: rt, port=7790, host_ip=lambda: '127.0.0.1')
    monkeypatch.setattr(cli, 'Config', lambda: c)
    monkeypatch.setattr(cli, 'write_registry', lambda *a: reg)
    monkeypatch.setattr(cli.registry, 'build', lambda *a: reg)
    monkeypatch.setattr(rt, 'installed', lambda: [])
    monkeypatch.setattr(cli.Runtipi, 'from_install', lambda *a: rt)
    monkeypatch.setattr(cli.alongside, 'load', lambda *a: {'shell': {'port': 8796}})
    monkeypatch.setattr(links, 'serve_status', lambda: {})
    monkeypatch.setattr(links, 'gateway_shell_ports', lambda *a: state['ports'])
    monkeypatch.setattr(links, 'publish', lambda *a, **kw: {'links': [], 'errors': {}})
    return SimpleNamespace(c=c, env=env, state=state, calls=calls, reg=reg)


def test_links_reconciles_existing_app_and_later_alias_without_rewriting_unchanged(box):
    assert cli.main(['links', '--json']) == 0
    assert {'box.tail.ts.net:8444'}.issubset(set(read_env(box.env)['WIZARD_TRUSTED_HOSTS'].split(',')))
    assert box.calls == [{'skipPull': True}]
    assert box.state['runtime'] == read_env(box.env)
    assert read_env(box.env)['OTHER'] == 'keep'
    assert read_env(box.env)['WIZARD_OWNER_LOGINS'] == 'owner@example.test'
    assert 'custom.test' in read_env(box.env)['WIZARD_TRUSTED_HOSTS'].split(',')
    stat, content = box.env.stat(), box.env.read_bytes()
    assert cli.main(['links', '--json']) == 0
    assert box.env.read_bytes() == content and box.env.stat().st_mtime_ns == stat.st_mtime_ns
    assert len(box.calls) == 1
    box.state['ports'].append(9444)
    assert cli.main(['links', '--json']) == 0
    assert {'https://box.tail.ts.net:9444'}.issubset(set(box.state['runtime']['WIZARD_TRUSTED_ORIGINS'].split(',')))
    assert len(box.calls) == 2


def test_links_stopped_app_saves_trust_without_starting(box):
    box.state['status'] = 'stopped'
    assert cli.main(['links', '--json']) == 0
    assert {'box.tail.ts.net:8444'}.issubset(set(read_env(box.env)['WIZARD_TRUSTED_HOSTS'].split(',')))
    assert box.calls == []


def test_links_reports_failed_runtime_application_and_retries(box):
    box.state['fail'] = True
    assert cli.main(['links', '--json']) == 1
    box.state['fail'] = False
    assert cli.main(['links', '--json']) == 0
    assert len(box.calls) == 2


def test_links_repairs_an_install_that_never_saved_trust(box):
    box.env.unlink()
    assert cli.main(['links', '--json']) == 0
    assert {'box.tail.ts.net:8444'}.issubset(set(box.state['runtime']['WIZARD_TRUSTED_HOSTS'].split(',')))
    assert box.calls == [{'skipPull': True}]


def test_fresh_box_retains_the_apps_own_origin(box, monkeypatch):
    monkeypatch.setattr(cli.alongside, 'load', lambda *a: None)
    (box.c.home / 'apps.json').write_text(json.dumps({'apps': []}))
    monkeypatch.setattr(store_data, 'fresh_ports', lambda reg: (8796, 8797))
    assert cli.main(['links', '--json']) == 0
    assert {'https://box.tail.ts.net:8801'}.issubset(set(box.state['runtime']['WIZARD_TRUSTED_ORIGINS'].split(',')))


@pytest.mark.parametrize('hold', ['busy', 'skipEnv', 'disabled-config', 'wait-error'])
def test_links_does_not_claim_runtime_success_on_a_hold(box, monkeypatch, capsys, hold):
    if hold == 'busy':
        box.state['status'] = 'updating'
    elif hold == 'skipEnv':
        monkeypatch.setattr(box.c.runtipi(), 'app', lambda urn: {'app': {'status': 'running', 'config': {'skipEnv': True}}})
    elif hold == 'disabled-config':
        monkeypatch.setattr(box.c.runtipi(), 'app', lambda urn: {'app': {'status': 'running', 'userConfigEnabled': False}})
    else:
        monkeypatch.setattr(box.c.runtipi(), 'wait_status', lambda *a: 'restart_error')
    assert cli.main(['links', '--json']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['errors']['trust:demo'].startswith('error:')
    if hold != 'disabled-config':
        assert (box.env.parent / '.wizard-trust-pending').exists()
    assert len(box.calls) == (1 if hold == 'wait-error' else 0)


def test_links_does_not_echo_backend_exception_details(box, monkeypatch, capsys):
    sentinel = 'synthetic-sensitive-marker'
    def unavailable(urn):
        raise RuntimeError(sentinel)
    monkeypatch.setattr(box.c.runtipi(), 'app', unavailable)
    assert cli.main(['links', '--json']) == 1
    output = capsys.readouterr().out
    assert sentinel not in output
    assert json.loads(output)['errors']['trust:demo'] == 'error: trust not applied; retry `wam links`'


def test_links_unreadable_serve_never_removes_trust(box, monkeypatch):
    assert cli.main(['links', '--json']) == 0
    before = box.env.read_bytes(), box.env.stat().st_mtime_ns
    def unavailable():
        raise links.ServeUnavailable('synthetic unreadable Serve')
    monkeypatch.setattr(links, 'serve_status', unavailable)
    assert cli.main(['links', '--json']) == 0
    assert (box.env.read_bytes(), box.env.stat().st_mtime_ns) == before
    assert len(box.calls) == 1


@pytest.mark.parametrize('mode', ['dry-run', 'other-store', 'unsupported'])
def test_links_does_not_mutate_unmanaged_or_dry_run(box, mode):
    if mode == 'other-store':
        box.reg['apps'][0]['urn'] = 'demo:private-store'
    if mode == 'unsupported':
        (box.c.runtipi_root / 'apps/wizardapps/demo/docker-compose.yml').write_text('services: {}\n')
    before = {p.relative_to(box.c.runtipi_root): p.read_bytes() for p in box.c.runtipi_root.rglob('*') if p.is_file()}
    assert cli.main(['links', '--json'] + (['--dry-run'] if mode == 'dry-run' else [])) == 0
    assert {p.relative_to(box.c.runtipi_root): p.read_bytes() for p in box.c.runtipi_root.rglob('*') if p.is_file()} == before
    assert box.calls == []
