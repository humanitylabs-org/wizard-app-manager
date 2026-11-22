"""wizard-connectors#63: a default app whose gateway strips its prefix (Connected accounts, /connected-accounts/)
serves its page at its root. It must still open at its Wizard path, not fall back to a port of its own."""
import copy
from types import SimpleNamespace

from wam import links


def _box(monkeypatch):
    cfg = {'TCP': {'8796': {'HTTPS': True}},
           'Web': {'box.ts.net:8796': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8796'}}}}}
    monkeypatch.setattr(links, 'tailnet', lambda: {'state': 'Running', 'host': 'box.ts.net'})
    monkeypatch.setattr(links, 'serve_status', lambda: copy.deepcopy(cfg))

    def run(args, *a):
        port = next(s.split('=', 1)[1] for s in args if s.startswith('--https='))
        path = next((s.split('=', 1)[1].rstrip('/') for s in args if s.startswith('--set-path=')), '/')
        cfg['Web'].setdefault('box.ts.net:' + port, {'Handlers': {}})['Handlers'][path] = {'Proxy': args[-1]}
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(links, '_run', run)
    return cfg


def _reg(view):
    return {'apps': [
        {'id': 'wizard-ux', 'name': 'Wizard', 'category': 'interface', 'status': 'running',
         'view': {'url': 'http://127.0.0.1:8796/'}},
        {'id': 'connected-accounts', 'name': 'Connected accounts', 'category': 'app', 'status': 'running',
         'view': {'url': view}}]}


def test_defaults_local_path_follows_strip_prefix():
    gw = links.gateway_paths()
    assert gw['connected-accounts'] == {'path': '/connected-accounts/', 'strip': True, 'local_path': '/'}
    assert gw['files'] == {'path': '/files/', 'strip': False, 'local_path': '/files/'}


def test_connected_accounts_opens_at_its_wizard_path(tmp_path, monkeypatch):
    cfg = _box(monkeypatch)
    result = links.publish(_reg('http://127.0.0.1:8819/'), tmp_path, {'wizard-ux'})
    link = next(l for l in result['links'] if l['id'] == 'connected-accounts')
    assert link['open'] == '/connected-accounts/'
    assert link['url'] == 'https://box.ts.net:8796/connected-accounts/'
    assert cfg['Web']['box.ts.net:8796']['Handlers']['/connected-accounts'] == {'Proxy': 'http://127.0.0.1:8819'}
    assert 'connected-accounts' not in result['errors']


def test_declared_path_app_still_waits_for_its_matching_recipe(tmp_path, monkeypatch):
    """The /files/ rule stays: an old recipe serving at / is not routed through a non-stripping path."""
    _box(monkeypatch)
    monkeypatch.setattr(links, 'gateway_paths',
                        lambda home: {'connected-accounts': {'path': '/files/', 'strip': False, 'local_path': '/files/'}})
    result = links.publish(_reg('http://127.0.0.1:8819/'), tmp_path, {'wizard-ux'})
    link = next(l for l in result['links'] if l['id'] == 'connected-accounts')
    assert link['open'] == 'https://box.ts.net:8819/'
