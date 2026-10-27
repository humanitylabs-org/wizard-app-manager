"""A privately staged fork in a Git-backed store must stay visible to Wizard."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from wam import registry


def fixture(root, name, store, private=False):
    directory = root / 'apps' / store / name
    directory.mkdir(parents=True)
    (directory / 'wizard.md').write_text(f'---\nid: {name}\nname: {name}\ntype: external\nversion: 0.2.0\nprovides: [ai-connection]\nview: {{path: /}}\n---\n')
    (directory / 'config.json').write_text('{"port": 18799}')
    (directory / '.wizard-source').write_text(json.dumps({'repo': 'example/release-fork', 'private': private}))
    (directory / '.wizard-commit').write_text('a' * 40)
    return {'app': {'status': 'running', 'port': 18799}, 'info': {'urn': f'{name}:{store}'}}


def test_private_fork_from_git_store_keeps_real_lifecycle_identity(tmp_path):
    rows = [fixture(tmp_path, 'regular', 'wizardapps'), fixture(tmp_path, 'fork', 'core', True), fixture(tmp_path, 'not-wizard', 'other')]
    result = registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path)
    assert [a['id'] for a in result['apps']] == ['fork', 'regular']
    fork = result['apps'][0]
    assert fork['urn'] == 'fork:core'
    assert fork['status'] == 'running'
    assert fork['commit'] == 'a' * 40
    assert fork['repo'] == 'example/release-fork'
    assert fork['view']['url'] == '/apps/fork/'
    assert fork['_view_upstream'] == 'http://127.0.0.1:18799/'
    assert result['slots']['ai-connection']['candidates'] == ['fork', 'regular']


def test_duplicate_installed_ids_across_stores_fail_closed(tmp_path):
    rows = [fixture(tmp_path, 'same-id', 'wizardapps'), fixture(tmp_path, 'same-id', 'core', True)]
    with pytest.raises(ValueError, match='Duplicate installed Wizard app id'):
        registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path)
