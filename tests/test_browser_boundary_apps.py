"""The installer switches the browser boundary on only while every installed app window opens inside it (WAM#1043).

Real HTTP servers on 127.0.0.1 stand in for apps: one like TG style / Files (a module script with no CORS answer for
`Origin: null`), one that answers it (like wizard-notes), one that refuses every cross-site file (like Tinfoil before
its fix), one that refuses the box-local page itself. Synthetic paths only.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from wam import boundary_apps as ba, browser_boundary as bb, cli

PAGE = """<!doctype html><html><head>
<link rel="stylesheet" href="{p}app.css">
<script src="/wizard-os-home/core/app-client.js"></script>
<script type="module" crossorigin src="{p}assets/index.js"></script>
<link rel="modulepreload" crossorigin href="{p}assets/vendor.js">
<script src="/elsewhere/not-ours.js"></script>
<script nomodule crossorigin src="{p}assets/legacy.js"></script>
</head><body></body></html>"""


def _server(kind):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")))
            if kind == "host" and self.headers.get("Host") != "box.example.ts.net:8444":
                self.send_response(403)
                self.end_headers()
                return
            if kind == "login" or self.path.startswith("/wizard-os-home/") or self.path.startswith("/elsewhere/"):
                self.send_response(401 if kind == "login" else 404)
                self.end_headers()
                return
            if kind == "strict" and self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_response(403)
                self.end_headers()
                return
            # Files lives at /files/ (WAM#1046), TG style at /tg-style/ (telegram#68)
            p = next((q for q in ("/files/", "/tg-style/") if self.path.startswith(q)), "/app/")
            if self.path == p:
                body, ctype = PAGE.format(p=p).encode(), "text/html"
            elif self.path in tuple(p + f for f in ("app.css", "assets/index.js", "assets/vendor.js", "assets/legacy.js")):
                body, ctype = b"/* x */", "text/javascript"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if kind == "cors" and self.path not in ("/app/", "/files/", "/tg-style/"):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


@pytest.fixture
def apps(tmp_path):
    servers = {}

    def add(app_id, kind, name=None):
        srv, seen = _server(kind)
        servers[app_id] = (srv, seen)
        reg = tmp_path / "home" / "apps.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        doc = json.loads(reg.read_text()) if reg.is_file() else {"apps": []}
        doc["apps"] = [a for a in doc["apps"] if a["id"] != app_id] + [
            {"id": app_id, "name": name or app_id, "urn": f"{app_id}:wizardapps",
             "view": {"url": f"http://127.0.0.1:{srv.server_address[1]}/app/"}}]
        reg.write_text(json.dumps(doc))
        return seen

    yield add
    for srv, _ in servers.values():
        srv.shutdown()


def _home(tmp_path):
    return tmp_path / "home"


def test_an_app_like_tg_style_would_open_blank(tmp_path, apps):
    seen = apps("tg-style", "plain", "TG style")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert [b["name"] for b in res["blank"]] == ["TG style"]
    why = res["blank"][0]["why"]
    assert "/app/assets/index.js" in why and "/app/assets/vendor.js" in why and "Origin: null" in why
    assert "app.css" not in why   # a plain stylesheet loads cross-site without CORS
    assert "legacy.js" not in why   # a modern browser never loads a nomodule file
    # it asked the way a sandboxed window does, and never for the shell's or another path's files
    assert ("/app/assets/index.js", "null", "cross-site") in seen
    assert not any(p.startswith(("/wizard-os-home/", "/elsewhere/")) for p, _, _ in seen)
    assert "TG style would open blank" in ba.reason(res)


def test_an_existing_box_with_files_and_passing_apps_switches_on(monkeypatch, tmp_path, apps):
    # WAM#1046: a box whose only "blank" app was Files (a default app) no longer stays off for good
    _files_like(tmp_path, apps)
    apps("notes", "cors", "Notes")
    c, recreated, told = _box(monkeypatch, tmp_path)
    out = cli.bootstrap(c)
    assert out["browser_boundary"]["on"] is True and out["browser_boundary"]["why"] == "existing box"
    assert bb.held(c.home) is None and sorted(recreated) == ["wizard-agent-service", "wizard-ux"]


def test_an_app_that_answers_origin_null_opens(tmp_path, apps):
    apps("notes", "cors", "Notes")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert res["ok"] == ["Notes"] and not res["blank"] and ba.reason(res) is None


def test_an_app_that_refuses_cross_site_files_would_open_blank(tmp_path, apps):
    apps("strict", "strict", "Strict")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert res["blank"] and "app.css (HTTP 403" in res["blank"][0]["why"]


def test_an_app_this_box_cannot_read_is_not_checked_never_blank(tmp_path, apps):
    apps("private", "login", "Private")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert not res["blank"] and res["unchecked"][0]["why"].endswith("HTTP 401 to a box-local request")


def test_it_names_the_host_the_app_trusts_like_the_browser_does(tmp_path, apps):
    # TG style answers only the box's tailnet host: the check asks with that host, from the app's own env
    apps("tg-style", "host", "TG style")
    assert ba.check(_home(tmp_path), tmp_path / "rt")["unchecked"]   # no host known: not checked, never blank
    env = tmp_path / "rt" / "app-data" / "wizardapps" / "tg-style" / "app.env"
    env.parent.mkdir(parents=True)
    env.write_text("WIZARD_TRUSTED_HOSTS=box.example.ts.net:8444,127.0.0.1\n")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert [b["name"] for b in res["blank"]] == ["TG style"]


def test_a_stopped_app_is_not_checked(tmp_path):
    h = _home(tmp_path)
    h.mkdir()
    (h / "apps.json").write_text(json.dumps({"apps": [{"id": "gone", "view": {"url": "http://127.0.0.1:59997/app/"}}]}))
    res = ba.check(h, tmp_path / "rt")
    assert not res["blank"] and "did not answer" in res["unchecked"][0]["why"]


def test_the_shell_and_apps_without_a_window_are_skipped(tmp_path, apps):
    apps("wizard-ux", "plain")
    h = _home(tmp_path)
    doc = json.loads((h / "apps.json").read_text())
    doc["apps"].append({"id": "worker", "mcp": {"url": "http://127.0.0.1:9/mcp"}})
    (h / "apps.json").write_text(json.dumps(doc))
    assert ba.check(h, tmp_path / "rt", skip={"wizard-ux"}) == {"ok": [], "blank": [], "unchecked": [], "own_tab": [], "same_origin": []}


def _files_like(tmp_path, apps, path="/files/"):
    """Files (File Browser): module scripts with no CORS answer, at the window path the shell opens as its own tab."""
    seen = apps("files", "plain", "Files")
    reg = _home(tmp_path) / "apps.json"
    doc = json.loads(reg.read_text())
    for a in doc["apps"]:
        if a["id"] == "files":
            a["view"]["url"] = a["view"]["url"].replace("/app/", path)
    reg.write_text(json.dumps(doc))
    return seen


def test_files_opens_in_its_own_tab_so_it_never_counts_as_blank(tmp_path, apps):
    # WAM#1046: under the boundary the shell opens Files as its own tab (wizard-ux#1821), never a sandboxed frame
    seen = _files_like(tmp_path, apps)
    apps("notes", "cors", "Notes")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert res == {"ok": ["Notes"], "blank": [], "unchecked": [], "own_tab": ["Files"], "same_origin": []}
    assert ba.reason(res) is None and not seen   # not even asked
    assert ba.check(_home(tmp_path), tmp_path / "rt", only={"files"})["blank"] == []


def test_only_the_shells_own_tab_apps_skip_the_check(tmp_path, apps):
    # same id at another window path, or another app at /files/: framed by the shell, so checked as before
    _files_like(tmp_path, apps, path="/app/")
    assert [b["name"] for b in ba.check(_home(tmp_path), tmp_path / "rt")["blank"]] == ["Files"]
    assert ba.own_tab("files", "/files/") and ba.own_tab("files", "/files/files/")
    assert not ba.own_tab("files", "/app/") and not ba.own_tab("tg-style", "/files/")
    assert not ba.own_tab("__proto__", "/files/") and not ba.own_tab("constructor", "/")


def _tg_style(tmp_path, apps, repo="humanitylabs-org/telegram", path="/tg-style/"):
    """TG style as the App Manager lists it: its window at /tg-style/, installed from its own repo."""
    seen = apps("tg-style", "plain", "TG style")
    reg = _home(tmp_path) / "apps.json"
    doc = json.loads(reg.read_text())
    for a in doc["apps"]:
        if a["id"] == "tg-style":
            a["view"]["url"] = a["view"]["url"].replace("/app/", path)
            if repo is not None:
                a["repo"] = repo
    reg.write_text(json.dumps(doc))
    return seen


def test_tg_style_from_its_own_listing_stays_same_origin_so_it_never_counts_as_blank(tmp_path, apps):
    # telegram#68 (option A): the shell keeps TG style's window same-origin (wizard-ux SAME_ORIGIN), never sandboxed
    seen = _tg_style(tmp_path, apps)
    apps("notes", "cors", "Notes")
    res = ba.check(_home(tmp_path), tmp_path / "rt")
    assert res == {"ok": ["Notes"], "blank": [], "unchecked": [], "own_tab": [], "same_origin": ["TG style"]}
    assert ba.reason(res) is None and not seen   # not even asked


def test_a_tg_style_from_anywhere_else_is_checked_like_any_app(tmp_path, apps):
    for repo, path in (("someone/telegram", "/tg-style/"), (None, "/tg-style/"), ("humanitylabs-org/telegram", "/app/")):
        _tg_style(tmp_path, apps, repo=repo, path=path)
        assert [b["name"] for b in ba.check(_home(tmp_path), tmp_path / "rt")["blank"]] == ["TG style"], (repo, path)
    assert ba.same_origin("tg-style", "/tg-style/", "humanitylabs-org/telegram")
    assert not ba.same_origin("tg-style", "/tg-style//x", "humanitylabs-org/telegram")
    assert not ba.same_origin("notes", "/tg-style/", "humanitylabs-org/telegram")
    assert not ba.same_origin("__proto__", "/tg-style/", "humanitylabs-org/telegram") and not ba.same_origin(None, "/", None)


def test_an_existing_box_with_tg_style_from_its_listing_switches_on(monkeypatch, tmp_path, apps):
    _tg_style(tmp_path, apps)
    apps("notes", "cors", "Notes")
    c, recreated, told = _box(monkeypatch, tmp_path)
    out = cli.bootstrap(c)
    assert out["browser_boundary"]["on"] is True and out["browser_boundary"]["why"] == "existing box"
    assert bb.held(c.home) is None



# ---------------------------------------------------------------- the installer


def _box(monkeypatch, tmp_path, notes=None):
    from wam import app_updates, release_pins, slots
    rt_root = tmp_path / "rt"
    for app in ("wizard-ux", "wizard-agent-service"):
        d = rt_root / "apps" / cli.STORE / app
        d.mkdir(parents=True, exist_ok=True)
        (d / ".wizard-commit").write_text("c0ffee\n")
    pins = {"default_apps": {"shell": {"app": "wizard-ux", "commit": "c0ffee"},
                             "agent-service": {"app": "wizard-agent-service", "commit": "c0ffee"}}}
    recreated = []
    told = notes if notes is not None else []
    monkeypatch.delenv(bb.SWITCH, raising=False)
    monkeypatch.setattr(release_pins, "load", lambda: pins)
    monkeypatch.setattr(bb, "mac_guest", lambda: False)
    monkeypatch.setattr(cli, "settle_connection_mode", lambda c, rt: None)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-ux", "wizard-agent-service", "tg-style"})
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {"wizard-agent-service": "service", "wizard-ux": "shell"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: {"apps": []})
    monkeypatch.setattr(slots, "to_install", lambda reg: [])
    monkeypatch.setattr(slots, "table", lambda reg: [])
    monkeypatch.setattr(cli, "hand_overrides_step", lambda c, rt: None)
    monkeypatch.setattr(cli, "release_updates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "relink_fresh_agent", lambda c, rt: None)
    monkeypatch.setattr(cli, "network_step", lambda c, rt: None)
    monkeypatch.setattr(cli, "recreate_apps", lambda c, rt, ids: recreated.extend(ids) or {a: "recreated" for a in ids})
    monkeypatch.setattr(app_updates, "_notify", lambda c, title, msg, priority="default", required=False:
                        told.append((title, msg)) or True)
    return SimpleNamespace(runtipi=lambda: None, home=_home(tmp_path), runtipi_root=rt_root), recreated, told


def test_an_existing_box_stays_off_while_an_app_would_open_blank_and_follows_once_fixed(monkeypatch, tmp_path, apps):
    apps("tg-style", "plain", "TG style")
    c, recreated, told = _box(monkeypatch, tmp_path)
    out = cli.bootstrap(c)
    assert out["browser_boundary"]["on"] is False and "TG style" in out["browser_boundary"]["held"]
    assert not bb.state_path(c.home).exists() and not bb.token_path(c.home).exists() and not recreated
    assert not told
    assert "TG style" in bb.held(c.home)["why"] and bb.held(c.home)["apps"] == ["TG style"]
    # the app is fixed: the next installer run switches the box on and tells the owner once
    apps("tg-style", "cors", "TG style")
    out = cli.bootstrap(c)
    assert out["browser_boundary"]["on"] is True and out["browser_boundary"]["why"] == "existing box"
    assert sorted(recreated) == ["wizard-agent-service", "wizard-ux"]
    assert bb.held(c.home) is None and bb.state(c.home)["checked"] and bb.state(c.home)["told"] is True
    assert [t for t, _ in told] == [cli.BOUNDARY_ON_NOTE[0]] and "wam browser-boundary off" in told[0][1]
    cli.bootstrap(c)
    assert len(told) == 1   # once


def test_a_box_the_installer_switched_on_goes_back_off_when_an_app_would_open_blank(monkeypatch, tmp_path, apps):
    apps("tg-style", "plain", "TG style")
    c, recreated, told = _box(monkeypatch, tmp_path)
    # an earlier release switched it on without checking (Miguel's box, 25 Nov)
    bb.set_box(c.home, c.runtipi_root, cli.STORE, ["wizard-ux", "wizard-agent-service"], True, why="existing box")
    assert bb.status_one(c.runtipi_root, cli.STORE, "wizard-ux")
    out = cli.bootstrap(c)
    assert out["browser_boundary"]["switched_off"] is True and "TG style" in out["browser_boundary"]["held"]
    assert not bb.box_on(c.home) and not bb.state_path(c.home).exists()   # nothing recorded: tried again next run
    assert not bb.status_one(c.runtipi_root, cli.STORE, "wizard-ux")
    assert sorted(recreated) == ["wizard-agent-service", "wizard-ux"]
    assert told and told[0][0] == "Apps open the old way again" and "TG style" in told[0][1]


def test_a_new_box_whose_default_app_would_open_blank_ends_off(monkeypatch, tmp_path, apps):
    apps("files", "plain", "Files")
    c, recreated, told = _box(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    from wam import slots
    monkeypatch.setattr(slots, "to_install", lambda reg: ["wizard-ux"])
    seen_on = []
    monkeypatch.setattr(cli, "install", lambda c_, a: seen_on.append(bb.box_on(c.home)) or {"id": a})
    out = cli.bootstrap(c)
    assert seen_on == [True]   # the shell still got its block as it was installed
    assert out["browser_boundary"]["on"] is False and out["browser_boundary"]["switched_off"] is True
    assert "Files" in bb.held(c.home)["why"] and not bb.state_path(c.home).exists()
    assert not told   # a new box was never on for its owner: nothing to explain


def test_a_new_box_whose_apps_open_stays_on_and_tells_the_owner(monkeypatch, tmp_path, apps):
    apps("files", "cors", "Files")
    c, recreated, told = _box(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    out = cli.bootstrap(c)
    assert out["browser_boundary"] == {"on": True, "why": "new box"}
    assert bb.box_on(c.home) and bb.state(c.home)["checked"] and [t for t, _ in told] == [cli.BOUNDARY_ON_NOTE[0]]


def test_an_owner_choice_is_never_undone(monkeypatch, tmp_path, apps):
    apps("tg-style", "plain", "TG style")
    c, recreated, told = _box(monkeypatch, tmp_path)
    bb.set_box(c.home, c.runtipi_root, cli.STORE, ["wizard-ux"], True)          # `wam browser-boundary on`
    out = cli.bootstrap(c)
    assert "browser_boundary" not in out and bb.box_on(c.home) and not recreated
    bb.set_box(c.home, c.runtipi_root, cli.STORE, ["wizard-ux"], False)         # `wam browser-boundary off`
    out = cli.bootstrap(c)
    assert "browser_boundary" not in out and not bb.box_on(c.home)


def test_doctor_shows_why_it_is_off(monkeypatch, tmp_path):
    bb.hold(tmp_path, "TG style would open blank in a sandboxed window", ["TG style"])
    monkeypatch.setattr(bb, "box_on", lambda home: False)
    h = bb.held(tmp_path)
    assert h["apps"] == ["TG style"]
    assert bb.status(tmp_path, tmp_path / "rt", cli.STORE, [])["held"].startswith("TG style")
    bb.set_box(tmp_path, tmp_path / "rt", cli.STORE, [], True)   # any owner choice clears the old reason
    assert bb.held(tmp_path) is None


def test_installing_an_app_that_would_open_blank_on_a_box_with_the_boundary_on_tells_the_owner(monkeypatch, tmp_path, apps):
    apps("tg-style", "plain", "TG style")
    c, recreated, told = _box(monkeypatch, tmp_path)
    bb.set_box(c.home, c.runtipi_root, cli.STORE, [], True)
    got = cli.boundary_after_app(c, "tg-style")
    assert got["opens_blank"] is True and got["way_back"] == "wam browser-boundary off"
    assert told[0][0] == "TG style may open blank" and "wam browser-boundary off" in told[0][1]
    assert bb.box_on(c.home)   # never lowered by an app
    apps("tg-style", "cors", "TG style")
    assert cli.boundary_after_app(c, "tg-style", "updated") is None
    bb.set_box(c.home, c.runtipi_root, cli.STORE, [], False)
    apps("tg-style", "plain", "TG style")
    assert cli.boundary_after_app(c, "tg-style") is None   # boundary off: nothing to check
