"""The installer's final wam links must publish the final shared launcher URLs."""
import json
from types import SimpleNamespace
from wam import cli, links


def test_links_cli_shares_final_urls_on_first_install(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    home = tmp_path / 'home'
    root = tmp_path / 'runtipi'
    app = root / 'apps/wizardapps/files'
    app.mkdir(parents=True)
    (app / 'wizard.md').write_text('---\nid: files\nname: Files\ntype: view\nprovides: [files]\nview: {path: /files/, port: 8794}\n---\n')
    (app / 'config.json').write_text('{"port":8794}')
    (app / 'SKILL.md').write_text('Files skill survives both registry writes')
    rt = SimpleNamespace(installed=lambda: [{'info': {'urn': 'files:wizardapps'}, 'app': {'status': 'running', 'port': 8794}}])
    c = SimpleNamespace(home=home, runtipi_root=root, host_ip=lambda: '127.0.0.1', runtipi=lambda: rt, port=7790)
    monkeypatch.setattr(cli, 'Config', lambda: c)
    calls = []
    def publish(reg, path, shell_ids, apply=True, api_port=7790):
        calls.append(apply)
        # This is what the already-tested first-party gateway publisher writes.
        result = {'links': [{'id': 'files', 'open': '/files/'}], 'errors': {}}
        if apply:
            assert reg['apps'][0]['view']['url'] == '/apps/files/'
            # write_registry returns the persisted public projection, never a
            # private loopback upstream. The owner gateway is applied next.
            assert '_view_upstream' not in reg['apps'][0]
            (path / 'links.json').write_text(json.dumps(result))
        return result
    monkeypatch.setattr(links, 'publish', publish)
    assert cli.main(['links', '--json']) == 0
    for path in (home / 'apps.json', root / 'app-data/wizard-os/apps.json'):
        data = json.loads(path.read_text())
        assert data['apps'][0]['view'] == {'path': '/files/', 'port': 8794,
                                           'url': '/files/', 'local': '/apps/files/'}
    for base in (home, root / 'app-data/wizard-os'):
        assert (base / 'skills/files/SKILL.md').read_text() == 'Files skill survives both registry writes'
    assert calls == [True]
    capsys.readouterr()
