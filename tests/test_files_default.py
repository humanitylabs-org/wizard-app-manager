"""Files is a first-party bootstrap default, not a per-box private catalog repair."""
import copy
from wam import links, slots


def test_fresh_defaults_bundle_files_with_repo(monkeypatch):
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    defaults = slots.load_defaults()
    assert defaults['files']['required'] is True
    assert 'files' in slots.to_install({'apps': []}, defaults)
    assert slots.repo_for('files', defaults) == 'oscarhenrycollins/files'


def test_fresh_files_routes_without_saved_links_or_catalog(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_gateway_shell_aliases import config
    monkeypatch.delenv('WAM_PRIVATE_CATALOG', raising=False)
    cfg = config()
    monkeypatch.setattr(links, 'tailnet', lambda: {'state': 'Running', 'host': 'box.ts.net'})
    monkeypatch.setattr(links, 'serve_status', lambda: copy.deepcopy(cfg))
    def run(args, *a):
        port = next(x.split('=', 1)[1] for x in args if x.startswith('--https='))
        path = next((x.split('=', 1)[1].rstrip('/') for x in args if x.startswith('--set-path=')), '/')
        cfg['TCP'][port] = {'HTTPS': True}
        cfg['Web'].setdefault('box.ts.net:' + port, {'Handlers': {}})['Handlers'][path] = {'Proxy': args[-1]}
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(links, '_run', run)
    reg = {'apps': [
        {'id': 'wizard-ux', 'name': 'Wizard', 'category': 'interface', 'view': {'url': 'http://127.0.0.1:8796/wizard-os-home/'}},
        {'id': 'files', 'name': 'Files', 'category': 'system', 'view': {'url': 'http://127.0.0.1:8794/files/'}}]}
    # A historical release recipe must not be redirected to an unsupported prefix.
    legacy = copy.deepcopy(reg)
    legacy['apps'][1]['view'] = {'url': 'http://127.0.0.1:8794/'}
    old = links.publish(legacy, tmp_path / 'legacy', {'wizard-ux'}, apply=False)
    assert next(a for a in old['links'] if a['id'] == 'files')['open'] == 'https://box.ts.net:8794/'
    assert not (tmp_path / 'links.json').exists()
    result = links.publish(reg, tmp_path, {'wizard-ux'})
    assert result['errors'] == {}
    for port in (8444, 8796, 8806):
        assert cfg['Web'][f'box.ts.net:{port}']['Handlers']['/files'] == {'Proxy': 'http://127.0.0.1:8794/files/'}
    links.apply_to_registry(reg, tmp_path)
    assert reg['apps'][1]['view'] == {'url': '/files/', 'local': 'http://127.0.0.1:8794/files/'}
    assert links.publish(reg, tmp_path, {'wizard-ux'})['links'] == result['links']
