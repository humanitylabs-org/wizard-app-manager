"""Mac launch integration must not let worker env redirect isolated owner state."""
import json
from wam import alongside, view_share


def test_explicit_owner_root_beats_unrelated_worker_environment(tmp_path, monkeypatch):
    owner = tmp_path / 'owner'
    owner.mkdir()
    wanted = tmp_path / 'owner-runtipi'
    (owner / 'app-manager.json').write_text(json.dumps({'runtipi_root': str(wanted)}))
    monkeypatch.setenv('RUNTIPI_ROOT', str(tmp_path / 'unrelated-install'))
    assert view_share.shared_home(owner) == wanted / 'app-data/wizard-os'


def test_isolated_unconfigured_owner_never_uses_worker_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNTIPI_ROOT', str(tmp_path / 'unrelated-install'))
    assert view_share.shared_home(tmp_path / 'isolated-owner') is None


def test_scalar_yaml_is_not_an_api_server_config_and_is_never_modified(tmp_path):
    path = tmp_path / 'config.yaml'
    original = b'owner sentinel\n'
    path.write_bytes(original)
    assert alongside._config(tmp_path) == {}
    assert alongside.api_origin(tmp_path) == 'http://127.0.0.1:8642'
    assert path.read_bytes() == original
