"""#400: `wam install` publishes links like Store install does, behind app-manager.json `cli_install_links` (off)."""
import json
from types import SimpleNamespace

from wam import cli, links


def _box(tmp_path, cfg=None):
    if cfg is not None:
        (tmp_path / "app-manager.json").write_text(json.dumps(cfg))
    return SimpleNamespace(home=tmp_path)


def test_switch_is_off_by_default(tmp_path):
    assert links.cli_install_publishes(tmp_path) is False
    assert links.cli_install_publishes(_box(tmp_path, {"cli_install_links": "yes"}).home) is False
    assert links.cli_install_publishes(_box(tmp_path, {"cli_install_links": True}).home) is True


def test_off_publishes_nothing(tmp_path):
    calls = []
    res = {"id": "search", "status": "running"}
    assert cli.cli_install_links(_box(tmp_path), res, publish=calls.append) == res
    assert calls == []


def test_on_publishes_after_a_good_install(tmp_path):
    c = _box(tmp_path, {"cli_install_links": True})
    for status in ("running", "stopped", "already installed"):
        calls = []
        out = cli.cli_install_links(c, {"id": "search", "status": status}, publish=calls.append)
        assert calls == [c] and out["links"] == "refreshed", status


def test_on_skips_a_failed_or_pending_install(tmp_path):
    c = _box(tmp_path, {"cli_install_links": True})
    for status in ("installing", "missing", "already handled"):
        calls = []
        assert "links" not in cli.cli_install_links(c, {"id": "search", "status": status}, publish=calls.append)
        assert calls == [], status


def test_cli_install_routes_through_it():
    src = open(cli.__file__).read()
    assert "print(json.dumps(cli_install_links(c, install_with_retry(" in src
