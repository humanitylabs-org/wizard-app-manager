#!/usr/bin/env python3
"""Disposable cross-repo Chats fixture. No Docker/network, no live WAM config.

Run with an explicit fresh temporary root. The Agent Service e2e test invokes this
at the install/choose/uninstall boundaries, with only image pull/fetch mocked.
"""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import chats_views, cli, private, registry, slots

root = Path(sys.argv[1]).resolve()
operation = sys.argv[2]
assert root.name.startswith('chats-install-e2e-'), 'use a dedicated disposable fixture root'
home, tipi, source = root / 'owner', root / 'tipi', root / 'source'
commit = 'a' * 40
manifest = {'id': 'mizel-chats', 'name': 'Mizel Chats', 'provides': ['chats'],
            'chats_view': {'contract': chats_views.CONTRACT, 'module': 'web/chats.js'},
            'runtime': {'config': 'config.json', 'compose': 'compose.yml'}}
if operation == 'install':
    assert not home.exists(), 'fixture refuses an existing owner home'
    (source / 'web').mkdir(parents=True)
    (source / 'wizard.md').write_text('---\n' + yaml.safe_dump(manifest) + '---\n# Chats\n')
    (source / 'config.json').write_text('{"id":"mizel-chats", "port":8799}')
    (source / 'compose.yml').write_text('services: {web: {image: "fixture:1"}}')
    (source / 'web/chats.js').write_text("export default { contract:'wizard.chats-view/1', id:'mizel-chats', name:'Mizel Chats', mount(el,host) { return {update() { el.replaceChildren(); }, unmount() { el.replaceChildren(); }}; } };\n")
    with patch.object(private, 'fetch', side_effect=lambda repo, sha, dst, tok: shutil.copytree(source, dst)), \
         patch.object(private, 'head_commit', return_value=commit), patch.object(private, 'pull'):
        private.prepare(tipi, 'wizardapps', 'fixture/chats', tok='fixture-only')
    rows = [{'info': {'urn': 'mizel-chats:wizardapps'}, 'app': {'status': 'running'}}]
    reg = registry.build(rows, 'wizardapps', '127.0.0.1', tipi)
    registry.write(reg, home); slots.merge_into_registry(reg); registry.write(reg, home)
elif operation == 'choose':
    chats_views.choose(home, 'mizel-chats')
elif operation == 'uninstall':
    reg = registry.build([], 'wizardapps', '', tipi)
    registry.write(reg, home); slots.merge_into_registry(reg); registry.write(reg, home)
else:
    raise ValueError('unknown fixture operation')
assert cli.share_registry(SimpleNamespace(home=home, runtipi_root=tipi))
print(json.dumps({'apps_json': str(tipi / 'app-data/wizard-os/apps.json'),
                  'registry': json.loads((home / 'apps.json').read_text())}))
