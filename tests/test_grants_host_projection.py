"""The host CLI must project metadata without reading root-owned grant files."""
import json
from pathlib import Path
from types import SimpleNamespace

from wam import cli, network, registry


def test_registry_refresh_uses_authenticated_metadata_when_state_is_private(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    state = root / 'state' / 'wizard-grants.json'
    state.parent.mkdir(parents=True)
    state.write_text('{"grants":[]}')
    grants = [{'from': 'calendar', 'to': 'provider', 'approved': True}]
    calls = []
    def request(method, path):
        calls.append((method, path))
        return grants
    runtime = SimpleNamespace(installed=lambda: [], req=request)
    config = SimpleNamespace(home=tmp_path / 'owner', runtipi_root=root,
                             host_ip=lambda: '127.0.0.1', runtipi=lambda: runtime)
    original = Path.read_text
    def private_read(self, *args, **kwargs):
        if self == state:
            raise PermissionError('root-owned metadata')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', private_read)
    # Connections has its own grant-read and Docker probes. Keep this host
    # projection regression isolated from that separately tested subsystem.
    monkeypatch.setattr(network, 'report', lambda *_: {'schema': 1, 'links': []})
    result = cli.write_registry(config)
    assert result['grants'] == grants
    assert calls == [('GET', '/apps/wizard/grants')]
    assert json.loads((config.home / 'apps.json').read_text())['grants'] == grants
    assert json.loads((root / 'app-data/wizard-os/apps.json').read_text())['grants'] == grants


def test_explicit_metadata_skips_private_registry_fallback(tmp_path, monkeypatch):
    state = tmp_path / 'wizard-grants.json'
    state.write_text('{}')
    monkeypatch.setenv('WIZARD_GRANTS_FILE', str(state))
    original = Path.read_text
    def private_read(self, *args, **kwargs):
        if self == state:
            raise PermissionError('must not read grant state when metadata was supplied')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', private_read)
    expected = [{'from': 'calendar', 'to': 'provider', 'approved': True}]
    out = registry.write({'apps': [], 'slots': {}, 'grants': expected}, tmp_path / 'owner')
    assert json.loads(out.read_text())['grants'] == expected
