"""apps.json carries an app page's in-network address (`view.internal`), the target the Agent Service uses for local
app routes on a fresh box (switch wizard/local-app-routes, wizard-app-manager#629). The launcher address is unchanged."""
import json
import re
import shutil
from pathlib import Path

from wam import links, registry

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'hello-wizard'


def _row(tmp_path, view_block=None):
    app = tmp_path / 'repos' / 'wizardapps' / 'apps' / 'hello-wizard'
    shutil.copytree(TEMPLATE, app)
    shutil.copy2(app / 'runtipi' / 'config.json', app / 'config.json')
    if view_block is not None:
        wm = app / 'wizard.md'
        text = re.sub(r'\nview:\n(?:  .*\n)+', '\n' + view_block, wm.read_text(), count=1)
        assert view_block in text
        wm.write_text(text)
    rows = [{'app': {'status': 'running', 'port': 8799}, 'info': {'urn': 'hello-wizard:wizardapps'}}]
    return json.loads(registry.write(registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path), tmp_path / 'owner').read_text())['apps'][0]


def test_view_with_a_port_gets_its_in_network_address(tmp_path):
    row = _row(tmp_path, 'view:\n  path: /files/\n  port: 8794\n')
    assert row['view'] == {'url': 'http://127.0.0.1:8799/files/', 'internal': 'http://hello-wizard:8794/files/'}


def test_view_without_a_usable_port_keeps_only_its_url(tmp_path):
    for i, block in enumerate(['view:\n  path: /\n', 'view:\n  path: /\n  port: nope\n', 'view:\n  path: /\n  port: 70000\n']):
        row = _row(tmp_path / str(i), block)
        assert row['view'] == {'url': 'http://127.0.0.1:8799/'}


def test_launcher_address_still_comes_from_url_and_links(tmp_path):
    # links.pages (what gets published) and apply_to_registry read url/local only; internal rides along untouched.
    row = _row(tmp_path, 'view:\n  path: /\n  port: 8080\n')
    assert links.pages({'apps': [row]}) == [{'id': 'hello-wizard', 'name': row['name'], 'category': row.get('category'),
                                             'port': 8799, 'path': '/'}]
