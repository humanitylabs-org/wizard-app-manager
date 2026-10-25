"""Host registry readers must not require backend-owned state permissions."""
import json
from types import SimpleNamespace

import pytest

from wam import cli, registry


class API:
    def req(self, method, path):
        assert (method, path) == ("GET", "/apps/wizard/grants")
        return [{"from": "api"}]


@pytest.mark.parametrize("body", [None, "not-json", "[]", '{"grants": null}'])
def test_missing_or_invalid_grants_uses_api(tmp_path, body):
    root = tmp_path / "runtipi"
    (root / "state").mkdir(parents=True)
    if body is not None:
        (root / "state" / "wizard-grants.json").write_text(body)
    cfg = SimpleNamespace(runtipi_root=root, home=tmp_path / "home")
    assert cli.read_grants(cfg, API()) == [{"from": "api"}]


@pytest.mark.parametrize("explicit", [True, False])
def test_registry_write_never_requires_backend_grants_permissions(tmp_path, monkeypatch, explicit):
    root = tmp_path / "wizard-os"
    root.mkdir()
    mirrored = [{"from": "mirror"}]
    (root / "apps.json").write_text(json.dumps({"grants": mirrored}))
    source = tmp_path / "state" / "wizard-grants.json"
    source.parent.mkdir()
    source.write_text("{}")
    source.chmod(0o600)
    monkeypatch.setenv("WIZARD_GRANTS_FILE", str(source))
    read_text = type(source).read_text
    def unreadable(self, *args, **kwargs):
        if self == source:
            raise PermissionError(13, "Permission denied", str(self))
        return read_text(self, *args, **kwargs)
    monkeypatch.setattr(type(source), "read_text", unreadable)
    reg = {"schema": 2, "apps": [], "slots": {}}
    expected = [{"from": "resolved-api"}] if explicit else mirrored
    if explicit:
        reg["grants"] = expected
    registry.write(reg, root)
    assert json.loads((root / "apps.json").read_text())["grants"] == expected
    assert source.stat().st_mode & 0o777 == 0o600
