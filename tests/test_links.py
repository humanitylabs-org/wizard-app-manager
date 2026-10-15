"""wam links: app pages go on the owner's tailnet with stock `tailscale serve`, never the admin page or services."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import links  # noqa: E402

REG = {"apps": [
    {"id": "wizard-shell", "name": "Wizard", "category": "interface", "status": "running",
     "view": {"url": "http://127.0.0.1:8796/wizard-ux/"}},
    {"id": "files", "name": "Files", "category": "system", "status": "running", "view": {"url": "http://127.0.0.1:8794/"}},
    {"id": "secret-drop", "name": "Secret Drop", "category": "system", "status": "running", "view": None},
    {"id": "stopped-app", "name": "Stopped", "category": "app", "status": "stopped", "view": {"url": "http://127.0.0.1:8800/"}},
    {"id": "admin", "name": "Admin", "category": "system", "status": "running", "view": {"url": "http://127.0.0.1:8480/"}},
]}


@pytest.fixture
def fake_tailscale(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    state = tmp_path / "state"
    state.write_text(json.dumps({"BackendState": "Running", "Self": {"DNSName": "box.tail1234.ts.net."}}))
    served = tmp_path / "served"
    served.write_text(json.dumps({"TCP": {"8794": {"HTTPS": True}}}))
    (bin_dir / "tailscale").write_text(f"""#!/bin/sh
echo "$@" >> {calls}
case "$1 $2" in
  "status --json") cat {state} ;;
  "serve status") cat {served} ;;
  "serve --bg") exit "${{SERVE_RC:-0}}" ;;
esac
""")
    (bin_dir / "tailscale").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return calls, state


def test_publishes_only_app_pages_shell_first(tmp_path, fake_tailscale):
    calls, _ = fake_tailscale
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"})
    assert [l["id"] for l in res["links"]] == ["wizard-shell", "files"]
    assert res["links"][0]["url"] == "https://box.tail1234.ts.net:8796/wizard-ux/"
    assert res["links"][1]["url"] == "https://box.tail1234.ts.net:8794/"
    log = calls.read_text()
    assert "serve --bg --https=8796 http://127.0.0.1:8796" in log
    assert "--https=8794" not in log          # already served: left alone
    for never in ("8480", "8800", "funnel"):  # admin page, stopped app, Funnel
        assert never not in log
    saved = json.loads((tmp_path / "home" / "links.json").read_text())
    assert saved["links"] == res["links"]


def test_unpaired_box_gets_no_links_and_a_plain_note(tmp_path, fake_tailscale):
    calls, state = fake_tailscale
    state.write_text(json.dumps({"BackendState": "NeedsLogin", "Self": {"DNSName": ""}}))
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"})
    assert res["links"] == [] and "tailscale up" in res["note"]
    assert "serve --bg" not in calls.read_text()
    assert "tailscale up" in links.describe(res)


def test_failed_serve_is_reported_not_listed(tmp_path, fake_tailscale, monkeypatch):
    monkeypatch.setenv("SERVE_RC", "1")
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"})
    assert [l["id"] for l in res["links"]] == ["files"]
    assert "wizard-shell" in res["errors"]


def test_dry_run_changes_nothing(tmp_path, fake_tailscale):
    calls, _ = fake_tailscale
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"}, apply=False)
    assert len(res["links"]) == 2
    assert "serve" not in calls.read_text()


def test_gateway_app_shares_shell_origin_and_registry_gets_openable_url(tmp_path, fake_tailscale, monkeypatch):
    """A private-test app with a catalog `gateway` path is served on the shell's tailnet port at that path (same
    origin as the shell), and apps.json then carries addresses a phone can open instead of 127.0.0.1."""
    calls, _ = fake_tailscale
    cat = tmp_path / "cat.json"
    cat.write_text(json.dumps({"schema": 1, "scope": "private-test", "apps": [
        {"id": "wisdom", "repo": "o/wisdom", "commit": "a" * 40, "gateway": {"path": "/wisdom/", "strip_prefix": True}}]}))
    monkeypatch.setenv("WAM_PRIVATE_CATALOG", str(cat))
    reg = {"apps": REG["apps"] + [{"id": "wisdom", "name": "Wisdom", "category": "app", "status": "running",
                                    "view": {"url": "http://127.0.0.1:9230/"}}]}
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    w = next(l for l in res["links"] if l["id"] == "wisdom")
    assert w["url"] == "https://box.tail1234.ts.net:8796/wisdom/" and w["open"] == "/wisdom/"
    log = calls.read_text()
    assert "serve --bg --https=8796 --set-path=/wisdom/ http://127.0.0.1:9230" in log
    assert "--https=9230" not in log                       # no port of its own
    links.apply_to_registry(reg, tmp_path / "home")
    by = {a["id"]: a for a in reg["apps"]}
    assert by["wisdom"]["view"] == {"url": "/wisdom/", "local": "http://127.0.0.1:9230/"}
    assert by["files"]["view"]["url"] == "https://box.tail1234.ts.net:8794/"
    assert by["stopped-app"]["view"] == {"url": "http://127.0.0.1:8800/"}   # not published: unchanged
    # re-running reads the box-only address, not the rewritten one
    assert {r["id"]: r["port"] for r in links.pages(reg)}["wisdom"] == 9230


def test_no_tailscale_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(links.shutil, "which", lambda _: None)
    res = links.publish(REG, tmp_path / "home")
    assert res["state"] == "missing" and res["links"] == []
