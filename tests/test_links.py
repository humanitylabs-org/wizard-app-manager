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
    {"id": "files", "name": "Files", "category": "system", "status": "running", "view": {"url": "http://127.0.0.1:8794/files/"}},
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
    served.write_text(json.dumps({"TCP": {"8794": {"HTTPS": True}}, "Web": {
        "box.tail1234.ts.net:8794": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8794"}}}}}))
    (bin_dir / "tailscale").write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
p = Path({str(served)!r})
with open({str(calls)!r}, 'a') as f: f.write(' '.join(args) + '\\n')
if args[:2] == ['status', '--json']:
    print(Path({str(state)!r}).read_text())
elif args[:2] == ['serve', 'status']:
    print(p.read_text())
elif args[:2] == ['serve', '--bg']:
    if os.environ.get('SERVE_RC'): sys.exit(int(os.environ['SERVE_RC']))
    d = json.loads(p.read_text())
    port = next(a.split('=',1)[1] for a in args if a.startswith('--https='))
    path = next((a.split('=',1)[1] for a in args if a.startswith('--set-path=')), '/')
    d.setdefault('TCP', {{}})[port] = {{'HTTPS': True}}
    d.setdefault('Web', {{}}).setdefault('box.tail1234.ts.net:'+port, {{}}).setdefault('Handlers', {{}})[path] = {{'Proxy': args[-1]}}
    p.write_text(json.dumps(d))
""")
    (bin_dir / "tailscale").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return calls, state


def test_publishes_only_app_pages_shell_first(tmp_path, fake_tailscale):
    calls, _ = fake_tailscale
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"})
    assert [l["id"] for l in res["links"]] == ["wizard-shell", "files"]
    assert res["links"][0]["url"] == "https://box.tail1234.ts.net:8796/wizard-ux/"
    assert res["links"][1]["url"] == "https://box.tail1234.ts.net:8796/files/"
    assert res["links"][1]["open"] == "/files/"
    assert res["links"][1]["gateway_strip"] is False
    log = calls.read_text()
    assert "serve --bg --https=8796 http://127.0.0.1:8796" in log
    assert "--https=8794" not in log          # already served: left alone
    for never in ("8480", "8800", "funnel"):  # admin page, stopped app, Funnel
        assert never not in log
    saved = json.loads((tmp_path / "home" / "links.json").read_text())
    assert saved["links"] == res["links"]


@pytest.mark.parametrize("who", ["messenger", "wizard-shell"])
def test_an_app_or_shell_mid_restart_keeps_its_same_origin_path(tmp_path, fake_tailscale, who):
    """A registry written while an app (or the shell) restarts must not move the app off its /<id>/ path."""
    reg = {"apps": [dict(a) for a in REG["apps"]] + [
        {"id": "messenger", "name": "Messenger", "category": "app", "status": "running",
         "view": {"url": "http://127.0.0.1:9245/"}}]}
    next(a for a in reg["apps"] if a["id"] == who)["status"] = "restarting"
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    m = next(l for l in res["links"] if l["id"] == "messenger")
    assert m["open"] == "/messenger/" and m["url"].startswith("https://") and m["url"].endswith(":8796/messenger/")
    links.apply_to_registry(reg, tmp_path / "home")
    assert next(a for a in reg["apps"] if a["id"] == "messenger")["view"]["url"] == "/messenger/"
    assert "8800" not in fake_tailscale[0].read_text()   # a stopped app still isn't published


def test_owner_unix_gateway_keeps_the_app_on_its_wizard_path(tmp_path, fake_tailscale):
    """Messenger's owner ingress (a Unix-socket Serve route the owner set) must not push the app onto its raw port."""
    calls, _ = fake_tailscale
    served = tmp_path / "served"
    d = json.loads(served.read_text())
    d["TCP"]["8796"] = {"HTTPS": True}
    d["Web"]["box.tail1234.ts.net:8796"] = {"Handlers": {
        "/": {"Proxy": "http://127.0.0.1:8796"},
        "/messenger": {"Proxy": "unix:/home/o/.wizard-os/messenger-owner/ingress.sock"}}}
    served.write_text(json.dumps(d))
    reg = {"apps": [dict(a) for a in REG["apps"]] + [
        {"id": "messenger", "name": "Messenger", "category": "app", "status": "running",
         "view": {"url": "http://127.0.0.1:9245/"}}]}
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    m = next(l for l in res["links"] if l["id"] == "messenger")
    assert m["open"] == "/messenger/" and not res.get("gateway_skipped")
    assert "--set-path=/messenger/" not in calls.read_text()        # the owner's route is never replaced
    links.apply_to_registry(reg, tmp_path / "home")
    assert next(a for a in reg["apps"] if a["id"] == "messenger")["view"]["url"] == "/messenger/"


def test_a_stale_serve_read_does_not_move_an_owner_fronted_app(tmp_path, fake_tailscale, monkeypatch):
    """One Serve read that misses the owner's gateway (Serve mid-rewrite) must not push Messenger onto its port."""
    real = links.serve_status
    owner = {"Proxy": "unix:/home/o/.wizard-os/messenger-owner/ingress.sock"}
    state = {"stale": True}   # Serve looks mid-rewrite until wam pauses and reads it again

    def flaky():
        d = real()
        d.setdefault("TCP", {})["8796"] = {"HTTPS": True}
        site = d.setdefault("Web", {}).setdefault("box.tail1234.ts.net:8796", {}).setdefault("Handlers", {})
        site.setdefault("/", {"Proxy": "http://127.0.0.1:8796"})
        site["/messenger"] = {"Proxy": "http://127.0.0.1:1"} if state["stale"] else owner
        return d

    monkeypatch.setattr(links, "serve_status", flaky)
    monkeypatch.setattr(links.time, "sleep", lambda _s: state.update(stale=False))
    reg = {"apps": [{"id": "wizard-shell", "name": "Wizard", "category": "interface", "status": "running",
                     "view": {"url": "http://127.0.0.1:8796/wizard-ux/"}},
                    {"id": "messenger", "name": "Messenger", "category": "app", "status": "running",
                     "view": {"url": "http://127.0.0.1:9245/"}}]}
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    assert next(l for l in res["links"] if l["id"] == "messenger")["open"] == "/messenger/"
    assert not res.get("gateway_skipped")


MESSENGER = {"id": "messenger", "name": "Messenger", "category": "app", "status": "running",
             "view": {"url": "http://127.0.0.1:9245/"}}


def _published_once(tmp_path):
    """A box where Messenger already opens at /messenger/ (links.json from a good run)."""
    reg = {"apps": [dict(a) for a in REG["apps"]] + [dict(MESSENGER)]}
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    assert next(l for l in res["links"] if l["id"] == "messenger")["open"] == "/messenger/"
    return reg


def _messenger_view(reg, home):
    reg = json.loads(json.dumps(reg))
    links.apply_to_registry(reg, home)
    return next(a for a in reg["apps"] if a["id"] == "messenger")["view"]["url"]


def test_an_unreadable_serve_never_moves_a_published_app_to_its_raw_port(tmp_path, fake_tailscale, monkeypatch):
    """A busy box (installs, builds) made one `tailscale serve status` time out; Messenger fell back to :9245 and
    stayed there until someone ran `wam links` by hand. A read that fails says nothing about the routes."""
    reg = _published_once(tmp_path)
    real_run = links._run

    def busy(args, timeout=30):
        if args[1:3] == ["serve", "status"]:
            raise links.subprocess.TimeoutExpired(args, timeout)
        return real_run(args, timeout)

    monkeypatch.setattr(links, "_run", busy)
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    m = next(l for l in res["links"] if l["id"] == "messenger")
    assert m["open"] == "/messenger/" and res["kept"]["messenger"] == "serve unreadable"
    assert _messenger_view(reg, tmp_path / "home") == "/messenger/"


def test_without_a_previous_address_an_unreadable_serve_still_reports(tmp_path, fake_tailscale, monkeypatch):
    reg = {"apps": [dict(a) for a in REG["apps"]] + [dict(MESSENGER)]}
    calls = {"n": 0}
    real = links.serve_status

    def flaky():
        calls["n"] += 1
        if calls["n"] > 2:   # the shell's own route reads fine, the app's reads fail
            raise links.ServeUnavailable("could not read Tailscale Serve configuration")
        return real()

    monkeypatch.setattr(links, "serve_status", flaky)
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    assert "messenger" not in (res.get("kept") or {})
    assert res.get("gateway_skipped") or res.get("errors")


def test_shell_away_keeps_apps_on_their_wizard_paths(tmp_path, fake_tailscale):
    """The shell briefly stopped (my-release update) used to send every same-origin app back to its raw port."""
    reg = _published_once(tmp_path)
    next(a for a in reg["apps"] if a["id"] == "wizard-shell")["status"] = "stopped"
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    assert res["kept"]["messenger"] == "shell restarting"
    assert _messenger_view(reg, tmp_path / "home") == "/messenger/"


def test_a_real_conflict_still_is_not_papered_over(tmp_path, fake_tailscale, monkeypatch):
    monkeypatch.setattr(links, "CONFLICT_RECHECK_S", 0)
    """Last known good is only for unreadable Serve: a different (non-owner) route on the path still wins."""
    reg = _published_once(tmp_path)
    served = tmp_path / "served"
    d = json.loads(served.read_text())
    d["Web"]["box.tail1234.ts.net:8796"]["Handlers"]["/messenger/"] = {"Proxy": "http://127.0.0.1:1"}
    served.write_text(json.dumps(d))
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    assert "messenger" not in (res.get("kept") or {})
    assert next(l for l in res["links"] if l["id"] == "messenger")["open"].startswith("https://")


def test_link_refreshes_never_overlap(tmp_path, fake_tailscale, monkeypatch):
    """Two installs finishing together each publish; interleaved runs raced Serve and links.json."""
    import threading
    import time as _t
    live, overlap = {"n": 0}, {"seen": False}
    real = links._publish

    def slow(*a, **k):
        live["n"] += 1
        overlap["seen"] |= live["n"] > 1
        _t.sleep(0.2)
        try:
            return real(*a, **k)
        finally:
            live["n"] -= 1

    monkeypatch.setattr(links, "_publish", slow)
    ts = [threading.Thread(target=links.publish, args=(REG, tmp_path / "home", {"wizard-shell"})) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not overlap["seen"]


def test_unpaired_box_gets_no_links_and_a_plain_note(tmp_path, fake_tailscale):
    calls, state = fake_tailscale
    state.write_text(json.dumps({"BackendState": "NeedsLogin", "Self": {"DNSName": ""}}))
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"})
    assert res["links"] == [] and "tailscale up" in res["note"]
    assert "serve --bg" not in calls.read_text()
    assert "tailscale up" in links.describe(res)


def test_failed_serve_is_reported_not_listed(tmp_path, fake_tailscale, monkeypatch):
    monkeypatch.setenv("SERVE_RC", "1")
    which = links.shutil.which
    monkeypatch.setattr(links.shutil, "which", lambda name: None if name == "sudo" else which(name))
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"})
    assert res["links"] == []
    assert "files" in res["errors"]
    assert "wizard-shell" in res["errors"]


def test_dry_run_changes_nothing(tmp_path, fake_tailscale):
    calls, _ = fake_tailscale
    res = links.publish(REG, tmp_path / "home", {"wizard-shell"}, apply=False)
    assert len(res["links"]) == 2
    assert "serve" not in calls.read_text()
    assert not (tmp_path / "home" / "links.json").exists()


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
    assert by["files"]["view"]["url"] == "/files/"
    assert by["stopped-app"]["view"] == {"url": "http://127.0.0.1:8800/"}   # not published: unchanged
    # re-running reads the box-only address, not the rewritten one
    assert {r["id"]: r["port"] for r in links.pages(reg)}["wisdom"] == 9230


def test_gateway_path_survives_a_run_without_the_catalog(tmp_path, fake_tailscale, monkeypatch):
    """Once published at /wisdom/, a later `wam links` without WAM_PRIVATE_CATALOG keeps it there."""
    cat = tmp_path / "cat.json"
    cat.write_text(json.dumps({"schema": 1, "scope": "private-test", "apps": [
        {"id": "wisdom", "repo": "o/wisdom", "commit": "a" * 40, "gateway": {"path": "/wisdom/", "strip_prefix": True}}]}))
    monkeypatch.setenv("WAM_PRIVATE_CATALOG", str(cat))
    reg = {"apps": REG["apps"] + [{"id": "wisdom", "name": "Wisdom", "category": "app", "status": "running",
                                    "view": {"url": "http://127.0.0.1:9230/"}}]}
    links.publish(reg, tmp_path / "home", {"wizard-shell"})
    monkeypatch.delenv("WAM_PRIVATE_CATALOG")
    res = links.publish(reg, tmp_path / "home", {"wizard-shell"})
    assert next(l for l in res["links"] if l["id"] == "wisdom")["open"] == "/wisdom/"


def test_no_tailscale_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(links.shutil, "which", lambda _: None)
    res = links.publish(REG, tmp_path / "home")
    assert res["state"] == "missing" and res["links"] == []
