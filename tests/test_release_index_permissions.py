import json
import os
from pathlib import Path
import stat
import pytest
import yaml
from wam.release_pins import configure_manager


def test_private_umask_keeps_owner_manifest_private_and_adapter_readable(tmp_path):
    original = json.loads((Path(__file__).parents[1] / 'releases/0.2.2.json').read_text())
    original['owner_private_extra'] = 'not-for-container'
    original['default_apps']['files']['private_extra'] = 'also-private'
    pins = tmp_path / 'owner-pins.json'
    pins.write_text(json.dumps(original)); pins.chmod(0o600)
    before = pins.read_bytes()
    path = tmp_path / 'compose.yml'
    prior = os.umask(0o077)
    try:
        configure_manager(path, 'example/manager:pinned', str(pins))
    finally:
        os.umask(prior)
    assert pins.read_bytes() == before
    assert stat.S_IMODE(pins.stat().st_mode) == 0o600
    service = yaml.safe_load(path.read_text())['services']['wizard-release-index']
    assert service['user'] == '1000:1000'
    assert all(v.endswith(':ro') for v in service['volumes'])
    assets = path.parent / '.wizard-release-index'
    assert all(stat.S_IMODE((assets / n).stat().st_mode) == 0o644 for n in ('release-index.mjs','release-pins.json'))
    public = json.loads((assets / 'release-pins.json').read_text())
    assert set(public) == {'version', 'default_apps'}
    assert 'private_extra' not in public['default_apps']['files']
    assert 'not-for-container' not in (assets / 'release-pins.json').read_text()


def test_release_index_asset_symlink_is_refused(tmp_path):
    pins = Path(__file__).parents[1] / 'releases/0.2.2.json'
    target = tmp_path / 'owner-file'; target.write_text('unchanged')
    assets = tmp_path / '.wizard-release-index'; assets.mkdir()
    (assets / 'release-index.mjs').symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        configure_manager(tmp_path / 'compose.yml', 'example/manager:pinned', str(pins))
    assert target.read_text() == 'unchanged'
