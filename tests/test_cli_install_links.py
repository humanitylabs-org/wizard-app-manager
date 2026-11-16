"""#400: `wam install` publishes links like Store install does. A bug fix, so always on (no feature switch)."""
from types import SimpleNamespace

from wam import cli


def _box(tmp_path):
    return SimpleNamespace(home=tmp_path)


def test_publishes_after_a_good_install_without_any_switch(tmp_path, monkeypatch):
    from wam import feature_switches as fs
    monkeypatch.setattr(fs, "is_on", lambda *a, **k: False)   # every switch off: links still publish
    c = _box(tmp_path)
    for status in ("running", "stopped", "already installed"):
        calls = []
        out = cli.cli_install_links(c, {"id": "search", "status": status}, publish=calls.append)
        assert calls == [c] and out["links"] == "refreshed", status


def test_default_publisher_is_store_install_links(tmp_path, monkeypatch):
    from wam import store_install
    calls = []
    monkeypatch.setattr(store_install, "_links", calls.append)
    c = _box(tmp_path)
    assert cli.cli_install_links(c, {"id": "search", "status": "running"})["links"] == "refreshed"
    assert calls == [c]


def test_skips_a_failed_or_pending_install(tmp_path):
    c = _box(tmp_path)
    for status in ("installing", "missing", "already handled"):
        calls = []
        out = cli.cli_install_links(c, {"id": "search", "status": status}, publish=calls.append)
        assert "links" not in out and calls == [], status


def test_no_feature_switch_left():
    src = open(cli.__file__).read()
    assert "cli-install-links" not in src and "CLI_INSTALL_LINKS" not in src


def test_cli_install_routes_through_it():
    src = open(cli.__file__).read()
    assert "print(json.dumps(cli_install_links(c, install_with_retry(" in src
