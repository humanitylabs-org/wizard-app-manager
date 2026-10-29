"""Connections on the shell's origin: the owner listener serves the view and makes only the owner's connection
decisions, through Runtipi's owner API; a change from the box itself (where every app reaches Serve as the owner)
or without the Store header is refused before Runtipi is called."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from wam import cli, store_install as si

HOST = "box.tail1.ts.net"
ORIGIN = f"https://{HOST}:8444"
PHONE = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "owner@x.com", "Origin": ORIGIN,
         "X-Wizard-Store": "1", "Content-Type": "application/json"}
BOX = {**PHONE, "X-Forwarded-For": "100.64.0.1"}   # this node's own address (conftest): any app container


class FakeRuntipi:
    def __init__(self):
        self.calls = []

    def req(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"mode": "open", "grants": [], "denied": [], "pending": [], "sensitive": [], "notices": []}
        return {"ok": True}


@pytest.fixture
def listener(monkeypatch, tmp_path):
    rt = FakeRuntipi()
    (tmp_path / "links.json").write_text(json.dumps({"host": HOST}))

    class C:
        home = tmp_path
        index = "x"

        def runtipi(self):
            return rt

    monkeypatch.setattr(si, "peer_uid", lambda p: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(si, "served_origins", lambda h: {ORIGIN})
    from wam import network
    monkeypatch.setattr(network, "report", lambda c: {"enabled": True, "mode": "open", "apps": []})
    monkeypatch.setattr(cli.StoreHandler, "cfg", C(), raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=PHONE):
        h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        raw = json.dumps(body).encode() if body is not None else None
        h.request(method, path, raw, {**headers, **({"Content-Length": str(len(raw))} if raw else {})})
        r = h.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    yield call, rt
    srv.shutdown()


def test_page_and_assets_are_served_with_the_owner_api_wired(listener):
    call, _ = listener
    code, hd, _ = call("GET", "/_connections")
    assert code == 308 and hd["Location"] == "_connections/"
    code, hd, b = call("GET", "/_connections/")
    assert code == 200 and hd["Content-Type"].startswith("text/html")
    assert b'data-only="connections"' in b and b'data-api="api"' in b and b'data-owner-header="X-Wizard-Store"' in b
    assert "frame-ancestors 'self'" in hd["Content-Security-Policy"]
    code, hd, b = call("GET", "/_connections/wizard-apps.js")
    assert code == 200 and b"renderConnections" in b and hd["Content-Type"].startswith("text/javascript")
    code, hd, b = call("GET", "/_connections/connections-map.js")
    assert code == 200 and b"export function mapModel" in b and hd["Content-Type"].startswith("text/javascript")
    for bad in ("/_connections/../wam/cli.py", "/_connections/README.md", "/_connections/x.js", "/_connectionsx"):
        assert call("GET", bad)[0] in (403, 404), bad


def test_settings_embed_mode_is_marked_before_first_paint_and_only_on_request(listener):
    """?embed=settings (the shell's Settings › Connections tab) marks <html> so the page drops its own title, sheet and
    canvas (standalone.css); the standalone page and any other embed value stay exactly as they were."""
    call, _ = listener
    plain = call("GET", "/_connections/")[2]
    code, _, b = call("GET", "/_connections/?embed=settings")
    assert code == 200 and b'<html lang="en" data-embed="settings">' in b
    assert b.replace(b' data-embed="settings"', b"", 1) == plain
    assert b"data-embed" not in plain
    for other in ("?embed=", "?embed=window", "?embed=settings%22%20onload%3D", "?x=settings"):
        assert call("GET", "/_connections/" + other)[2] == plain, other
    css = (Path(__file__).resolve().parent.parent / "wizard-views" / "standalone.css").read_text()
    for rule in ('html[data-embed="settings"] .wam-cl-title', 'html[data-embed="settings"] body.wux .wux-sheet',
                 'html[data-embed="settings"] .wam-cl-card'):
        assert rule in css, rule


def test_the_page_reuses_the_shells_kit_sheet_and_app_glyphs(listener):
    """The map is drawn with the shell's own pieces on the same origin (kit tokens + look, WzSheet, WizSigil), linked
    before the App Manager's styles so its rules still win a tie; the page needs nothing else from the shell."""
    call, _ = listener
    b = call("GET", "/_connections/")[2].decode()
    for ref in ("/wizard-os-home/kit/wizard-tokens.css", "/wizard-os-home/kit/wizard-ui.css", "/wizard-os-home/core/sigils.css",
                "/wizard-os-home/kit/wizard-sheet.js", "/wizard-os-home/core/sigils.js"):
        assert ref in b, ref
        assert b.index(ref) < b.index('href="wux.css"'), ref
    assert 'data-wz-shared="kit"' in b   # app-client.js treats it as the shared kit, not a vendored copy to swap


def test_reads_say_whether_this_device_may_change(listener):
    call, _ = listener
    code, _, b = call("GET", "/_connections/api/apps/wizard/connections")
    assert code == 200 and json.loads(b)["owner_writes"] is True and json.loads(b)["mode"] == "open"
    code, _, b = call("GET", "/_connections/api/apps/wizard/connections", headers=BOX)
    st = json.loads(b)
    assert code == 200 and st["owner_writes"] is False and "this Wizard itself" in st["owner_writes_why"]
    code, _, b = call("GET", "/_connections/api/connections")
    assert code == 200 and json.loads(b)["mode"] == "open"
    assert call("GET", "/_connections/", headers={**PHONE, "Tailscale-User-Login": "guest@x.com"})[0] == 403


def test_owner_decisions_go_to_runtipi_exactly(listener):
    call, rt = listener
    assert call("POST", "/_connections/api/apps/cinequeue:wizardapps/app-grants",
                {"to": "wizard-shopping", "approve": False})[0] == 200
    assert call("POST", "/_connections/api/apps/cinequeue:wizardapps/app-grants",
                {"to": "wizard-shopping", "approve": True})[0] == 200
    assert call("POST", "/_connections/api/apps/files:wizardapps/sensitive", {"sensitive": True})[0] == 200
    assert call("POST", "/_connections/api/apps/wizard/connection-mode", {"mode": "declared"})[0] == 200
    assert [c for c in rt.calls if c[0] == "POST"] == [
        ("POST", "/apps/cinequeue:wizardapps/app-grants", {"to": "wizard-shopping", "approve": False}),
        ("POST", "/apps/cinequeue:wizardapps/app-grants", {"to": "wizard-shopping", "approve": True}),
        ("POST", "/apps/files:wizardapps/sensitive", {"sensitive": True}),
        ("POST", "/apps/wizard/connection-mode", {"mode": "declared"}),
    ]


def test_apps_and_bad_requests_never_reach_runtipi(listener):
    call, rt = listener
    grant = ("/_connections/api/apps/cinequeue:wizardapps/app-grants", {"to": "wizard-shopping", "approve": False})
    code, _, b = call("POST", *grant, headers=BOX)                       # an app container, through Serve
    assert code == 403 and "this Wizard itself" in json.loads(b)["error"]
    assert call("POST", *grant, headers={k: v for k, v in PHONE.items() if k != "X-Wizard-Store"})[0] == 403
    assert call("POST", *grant, headers={**PHONE, "Origin": "https://box.tail1.ts.net:9205"})[0] == 403
    assert call("POST", *grant, headers={**PHONE, "Tailscale-User-Login": "guest@x.com"})[0] == 403
    for path, body in [
        ("/_connections/api/apps/cinequeue:wizardapps/app-grants", {"to": "wizard-shopping"}),
        ("/_connections/api/apps/cinequeue:wizardapps/app-grants", {"to": "../x", "approve": True}),
        ("/_connections/api/apps/cinequeue:wizardapps/app-grants", {"to": "cinequeue", "approve": False}),
        ("/_connections/api/apps/cinequeue:wizardapps/sensitive", {"sensitive": "yes"}),
        ("/_connections/api/apps/wizard/connection-mode", {"mode": "off"}),
    ]:
        assert call("POST", path, body)[0] == 400, (path, body)
    for path in ("/_connections/api/app-lifecycle/cinequeue:wizardapps/uninstall",
                 "/_connections/api/apps/cinequeue:wizardapps/share-grants",
                 "/_connections/api/apps/wizard/connection-notices"):
        assert call("POST", path, {"x": 1})[0] == 404, path
    assert [c for c in rt.calls if c[0] == "POST"] == []


def test_agents_access_goes_to_the_agent_service_only_from_the_owners_device(listener, monkeypatch):
    """Connections → Agents: the list and the owner's switches. An app container (through Serve, as the owner's
    login) or a request without the Store header never changes an agent's access."""
    from wam import agent_access
    call, rt = listener
    seen = []
    monkeypatch.setattr(agent_access, "instances", lambda home: {"service": True, "agents": [{"id": "hermes", "name": "Oscar"}]})
    monkeypatch.setattr(agent_access, "set_access", lambda home, a, apps, on: seen.append((a, apps, on)) or {"service": True, "agents": []})
    code, _, b = call("GET", "/_connections/api/agents")
    assert code == 200 and json.loads(b)["agents"][0]["name"] == "Oscar"
    sw = ("/_connections/api/agents/hermes/access", {"apps": ["cinequeue"], "on": False})
    assert call("POST", *sw, headers=BOX)[0] == 403
    assert call("POST", *sw, headers={k: v for k, v in PHONE.items() if k != "X-Wizard-Store"})[0] == 403
    assert call("POST", *sw, headers={**PHONE, "Tailscale-User-Login": "guest@x.com"})[0] == 403
    assert seen == []
    assert call("POST", *sw)[0] == 200
    assert seen == [("hermes", ["cinequeue"], False)]
    for path, body in [("/_connections/api/agents/hermes/access", {"apps": ["cinequeue"]}),
                       ("/_connections/api/agents/hermes/access", {"apps": "cinequeue", "on": True}),
                       ("/_connections/api/agents/hermes/access", {"apps": ["cinequeue"], "on": "no"})]:
        assert call("POST", path, body)[0] == 400, body
    assert call("POST", "/_connections/api/agents/../x/access", {"apps": ["a"], "on": True})[0] in (400, 404)
    assert len(seen) == 1 and [c for c in rt.calls if c[0] == "POST"] == []


def test_agents_say_plainly_when_the_agent_service_cannot_answer(listener, monkeypatch):
    from wam import agent_access
    call, _ = listener

    def down(*a, **k):
        raise agent_access.AccessError("can't reach the Agent Service (refused)", 503)
    monkeypatch.setattr(agent_access, "instances", down)
    monkeypatch.setattr(agent_access, "set_access", down)
    code, _, b = call("GET", "/_connections/api/agents")
    assert code == 200 and json.loads(b) == {"service": False, "why": "can't reach the Agent Service (refused)", "agents": []}
    code, _, b = call("POST", "/_connections/api/agents/hermes/access", {"apps": ["money"], "on": True})
    assert code == 503 and json.loads(b)["error"].startswith("Not changed: can't reach the Agent Service")


def test_restore_defaults_shows_its_plan_and_needs_the_owners_confirm(listener, monkeypatch):
    from wam import connections_reset, network
    call, rt = listener
    monkeypatch.setattr(network, "connection_state", lambda c, r: {"grants": [], "denied": [
        {"from": "messenger", "to": "money", "approved": "owner", "reason": "uses"}]})
    monkeypatch.setattr(network, "report", lambda c, r=None: {"apps": [], "controls": []})
    applied = []
    monkeypatch.setattr(connections_reset, "apply", lambda c, r, reconcile=None, expect=None: applied.append(expect) or {"changed": []})
    code, _, b = call("GET", "/_connections/api/connections/restore")
    got = json.loads(b)
    assert code == 200 and got["changes"][0]["text"] == "messenger → money will be turned back on."
    assert got["plan"] == connections_reset.plan_id(got["changes"])
    ok = {"confirm": True, "plan": got["plan"]}
    assert call("POST", "/_connections/api/connections/restore", ok, headers=BOX)[0] == 403
    assert call("POST", "/_connections/api/connections/restore", {"confirm": False, "plan": got["plan"]})[0] == 400
    assert call("POST", "/_connections/api/connections/restore", {"confirm": True})[0] == 400   # no plan: not applied
    assert call("POST", "/_connections/api/connections/restore", {})[0] == 400
    assert applied == []
    assert call("POST", "/_connections/api/connections/restore", ok)[0] == 200
    assert applied == [got["plan"]]
