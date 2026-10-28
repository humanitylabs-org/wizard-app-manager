"""Connections on the shell's origin: the owner listener serves the view and makes only the owner's connection
decisions, through Runtipi's owner API; a change from the box itself (where every app reaches Serve as the owner)
or without the Store header is refused before Runtipi is called."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

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
    for bad in ("/_connections/../wam/cli.py", "/_connections/README.md", "/_connections/x.js", "/_connectionsx"):
        assert call("GET", bad)[0] in (403, 404), bad


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
