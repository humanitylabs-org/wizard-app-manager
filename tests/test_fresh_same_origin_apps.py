"""Fresh boxes: Store-installed apps open inside Wizard.

The shell only opens app views on its own origin (`hostedOpenable`/`WizCanOpen`). Alongside boxes route apps under
the shell origin; a fresh box published each app only on its own port, so the Store showed "✓ Installed" with no
Open. Every installed app page now also gets a same-origin gateway path on each owner shell Serve port, and
apps.json/links.json point at it.
"""
import copy
import json
from types import SimpleNamespace

from wam import links

HOST = 'box.ts.net'


def serve_config():
    # Fresh layout: shell :8796 with the Store routes; Files already on its declared path; Morning Report on its port.
    return {'TCP': {p: {'HTTPS': True} for p in ('8796', '8802')}, 'Web': {
        f'{HOST}:8796': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8796'},
                                      '/files/': {'Proxy': 'http://127.0.0.1:8794/files/'},
                                      '/wizard-os-home/store-install': {'Proxy': 'http://127.0.0.1:7791'}}},
        f'{HOST}:8802': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8802'}}}}}


def fake(monkeypatch, cfg):
    monkeypatch.delenv('WAM_PRIVATE_CATALOG', raising=False)
    monkeypatch.setattr(links, 'tailnet', lambda: {'state': 'Running', 'host': HOST})
    monkeypatch.setattr(links, 'serve_status', lambda: copy.deepcopy(cfg))
    calls = []

    def run(args, *a):
        calls.append(args)
        port = next(x.split('=', 1)[1] for x in args if x.startswith('--https='))
        path = next((x.split('=', 1)[1] for x in args if x.startswith('--set-path=')), '/')
        cfg['TCP'][port] = {'HTTPS': True}
        cfg['Web'].setdefault(f'{HOST}:{port}', {'Handlers': {}})['Handlers'][path] = {'Proxy': args[-1]}
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(links, '_run', run)
    return calls


def registry():
    return {'apps': [
        {'id': 'wizard-ux', 'name': 'Wizard Shell', 'category': 'interface', 'status': 'running',
         'provides': ['shell'], 'view': {'url': 'http://127.0.0.1:8796/wizard-os-home/'}},
        {'id': 'files', 'name': 'Files', 'category': 'system', 'status': 'running',
         'view': {'url': 'http://127.0.0.1:8794/files/'}},
        {'id': 'morning-report', 'name': 'Morning Report', 'category': 'app', 'status': 'running',
         'view': {'url': 'http://127.0.0.1:8802/'}},
        {'id': 'wizard-chatgpt', 'name': 'ChatGPT', 'category': 'system', 'status': 'running',
         'view': {'url': 'http://127.0.0.1:8801/'}},
        {'id': 'hermes-agent', 'name': 'Hermes Agent', 'category': 'agent', 'status': 'running',
         'view': {'url': 'http://127.0.0.1:9119/'}},
        {'id': 'notifications', 'name': 'Notifications', 'category': 'system', 'status': 'running', 'view': None},
    ]}


def same_origin(url):
    return url.startswith('/') and not url.startswith('//')


def test_store_apps_get_same_origin_path_on_fresh_box(tmp_path, monkeypatch):
    cfg = serve_config()
    fake(monkeypatch, cfg)
    reg = registry()
    res = links.publish(reg, tmp_path, {'wizard-ux'})
    assert res['errors'] == {}
    h = cfg['Web'][f'{HOST}:8796']['Handlers']
    # Root-serving view: stripped prefix, exactly like the alongside gateway.
    assert h['/morning-report/'] == {'Proxy': 'http://127.0.0.1:8802'}
    assert h['/wizard-chatgpt/'] == {'Proxy': 'http://127.0.0.1:8801'}
    # The app keeps its own port too.
    assert cfg['Web'][f'{HOST}:8802']['Handlers']['/'] == {'Proxy': 'http://127.0.0.1:8802'}
    # Declared view.path (Files) keeps its existing unstripped route; the agent dashboard keeps its own port.
    assert h['/files/'] == {'Proxy': 'http://127.0.0.1:8794/files/'}
    assert '/hermes-agent/' not in h
    links.apply_to_registry(reg, tmp_path)
    by = {a['id']: a for a in reg['apps']}
    assert by['morning-report']['view'] == {'url': '/morning-report/', 'local': 'http://127.0.0.1:8802/'}
    assert by['wizard-chatgpt']['view']['url'] == '/wizard-chatgpt/'
    assert by['files']['view']['url'] == '/files/'
    assert by['hermes-agent']['view']['url'] == f'https://{HOST}:9119/'
    assert by['notifications']['view'] is None
    for app in ('morning-report', 'wizard-chatgpt', 'files'):
        assert same_origin(by[app]['view']['url']), app   # what the shell's WizCanOpen requires
    mr = next(l for l in res['links'] if l['id'] == 'morning-report')
    assert mr['url'] == f'https://{HOST}:8796/morning-report/' and mr['gateway_strip'] is True
    assert mr['own'] == f'https://{HOST}:8802/'


def test_declared_subpath_view_is_not_stripped(tmp_path, monkeypatch):
    cfg = serve_config()
    fake(monkeypatch, cfg)
    reg = registry()
    reg['apps'].append({'id': 'docs', 'name': 'Docs', 'category': 'app', 'status': 'running',
                        'view': {'url': 'http://127.0.0.1:8810/docs/'}})
    links.publish(reg, tmp_path, {'wizard-ux'})
    assert cfg['Web'][f'{HOST}:8796']['Handlers']['/docs/'] == {'Proxy': 'http://127.0.0.1:8810/docs/'}


def test_every_owner_shell_port_and_conflicts_left_alone(tmp_path, monkeypatch):
    cfg = serve_config()
    cfg['TCP']['8444'] = {'HTTPS': True}
    cfg['Web'][f'{HOST}:8444'] = {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8796'},
                                               '/wizard-os-home/store-install': {'Proxy': 'http://127.0.0.1:7791'},
                                               '/morning-report/': {'Proxy': 'http://127.0.0.1:9999'}}}
    fake(monkeypatch, cfg)
    reg = registry()
    res = links.publish(reg, tmp_path, {'wizard-ux'})
    # Alias gets the other app; the owner's different /morning-report/ route there is never replaced.
    assert cfg['Web'][f'{HOST}:8444']['Handlers']['/wizard-chatgpt/'] == {'Proxy': 'http://127.0.0.1:8801'}
    assert cfg['Web'][f'{HOST}:8444']['Handlers']['/morning-report/'] == {'Proxy': 'http://127.0.0.1:9999'}
    assert 'morning-report@8444' in res['gateway_skipped'] and res['errors'] == {}
    # The main shell port has the route, so the app still opens same-origin there.
    assert next(l for l in res['links'] if l['id'] == 'morning-report')['open'] == '/morning-report/'


def test_conflict_on_main_shell_port_keeps_own_port_link(tmp_path, monkeypatch):
    cfg = serve_config()
    cfg['Web'][f'{HOST}:8796']['Handlers']['/morning-report/'] = {'Proxy': 'http://127.0.0.1:9999'}
    fake(monkeypatch, cfg)
    res = links.publish(registry(), tmp_path, {'wizard-ux'})
    assert cfg['Web'][f'{HOST}:8796']['Handlers']['/morning-report/'] == {'Proxy': 'http://127.0.0.1:9999'}
    mr = next(l for l in res['links'] if l['id'] == 'morning-report')
    assert mr['open'] == f'https://{HOST}:8802/' and 'gateway_implicit' not in mr


def test_rerun_is_idempotent_and_never_pins_a_stale_path(tmp_path, monkeypatch):
    cfg = serve_config()
    calls = fake(monkeypatch, cfg)
    first = links.publish(registry(), tmp_path, {'wizard-ux'})
    n = len(calls)
    again = links.publish(registry(), tmp_path, {'wizard-ux'})
    assert again['links'] == first['links'] and len(calls) == n
    # Implicit paths are not treated as owner-declared gateways on the next run.
    assert 'morning-report' not in links.gateway_paths(tmp_path)


def test_store_get_rewrites_and_reshares_same_origin_url(tmp_path, monkeypatch):
    """A later Store Get runs store_install._links: the new app's shared launcher URL is same-origin."""
    from wam import cli, store_install
    cfg = serve_config()
    fake(monkeypatch, cfg)
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'rt')
    c.home.mkdir()
    reg = registry()
    reg['apps'].append({'id': 'transcriber', 'name': 'Transcriber', 'category': 'app', 'status': 'running',
                        'view': {'url': 'http://127.0.0.1:8803/'}})
    monkeypatch.setattr(cli, 'write_registry', lambda c: copy.deepcopy(reg))
    store_install._links(c)
    shared = json.loads((c.runtipi_root / 'app-data/wizard-os/apps.json').read_text())
    by = {a['id']: a for a in shared['apps']}
    assert by['transcriber']['view']['url'] == '/transcriber/'
    assert cfg['Web'][f'{HOST}:8796']['Handlers']['/transcriber/'] == {'Proxy': 'http://127.0.0.1:8803'}
