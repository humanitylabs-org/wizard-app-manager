"""Browser owner boundary on the App Manager's shell-origin routes (wizard-os#133, slice 3).

Off by default: nothing changes. With WIZARD_BROWSER_BOUNDARY=1 every Store/Settings listener route and every host
API route published beside the shell also needs the shell-only token, so an app page (same origin, owner login,
shell Origin) can no longer act as the owner. Synthetic token and headers only.
"""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from wam import browser_boundary as bb, cli, resources, store_install as si

TOKEN = "ab" * 32
SHELL = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "owner@x.com",
         "Origin": "https://box.tail1.ts.net:8796", "X-Wizard-Store": "1", "Content-Type": "application/json",
         "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "empty"}

# Every route the Store/Settings listener answers (StoreHandler._go). The gate must run before each.
STORE_ROUTES = ["_connections/api/connections", "_connections/api/agents", "_updates", "_feature-switches",
                "_app-updates", "_disk", "_main-compare", "_views/notes", "_slots/chats", "_remove/notes",
                "_github", "_host-access/notes", "_recommended", "notes"]
HANDLERS = ["_connections", "_updates", "_feature_switches", "_app_updates", "_view_revert", "_chats_default",
            "_remove", "_github", "_host_access"]


@pytest.fixture
def on(tmp_path, monkeypatch):
    f = tmp_path / "shell-token"
    f.write_text(TOKEN + "\n")
    monkeypatch.setenv(bb.SWITCH, "1")
    monkeypatch.setenv(bb.TOKEN_FILE, str(f))
    return f


def test_off_by_default_nothing_is_checked(monkeypatch):
    monkeypatch.delenv(bb.SWITCH, raising=False)
    assert bb.refusal({}) is None and bb.refusal({"Origin": "null", "Sec-Fetch-Site": "cross-site"}) is None
    monkeypatch.setenv(bb.SWITCH, "true")          # only exactly "1" turns it on
    assert bb.refusal({}) is None


def test_on_needs_the_shell_token_from_the_shell_document(on):
    good = {**SHELL, bb.HEADER: TOKEN}
    assert bb.refusal(good) is None
    assert bb.refusal(SHELL) == bb.REFUSED                                    # app page: owner login, no token
    assert bb.refusal({**good, bb.HEADER: "cd" * 32}) == bb.REFUSED
    assert bb.refusal({**good, "Origin": "null"}) == bb.REFUSED               # sandboxed frame, even with token
    assert bb.refusal({**good, "Sec-Fetch-Site": "same-site"}) == bb.REFUSED
    assert bb.refusal({**good, "Sec-Fetch-Dest": "iframe"}) == bb.REFUSED     # a framed load
    assert bb.refusal({bb.HEADER: TOKEN}) is None                            # non-browser caller with the token


def test_on_without_a_usable_token_fails_closed(on, monkeypatch, tmp_path):
    good = {**SHELL, bb.HEADER: TOKEN}
    on.write_text("short")
    assert bb.refusal(good) == bb.REFUSED
    monkeypatch.setenv(bb.TOKEN_FILE, str(tmp_path / "missing"))
    assert bb.refusal(good) == bb.REFUSED
    monkeypatch.delenv(bb.TOKEN_FILE)
    assert bb.refusal(good) == bb.REFUSED


@pytest.fixture
def store(monkeypatch, tmp_path):
    """The real StoreHandler behind its owner gate, with every sub-handler replaced by a marker."""
    monkeypatch.setattr(si, "peer_uid", lambda p: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(si, "served_origins", lambda h: {SHELL["Origin"]})
    monkeypatch.setattr(si, "check_request", lambda *a: None)
    monkeypatch.setattr(si, "start", lambda *a, **k: (299, {"reached": "install"}))
    monkeypatch.setattr(si, "status", lambda a: (299, {"reached": "status"}))
    (tmp_path / "links.json").write_text(json.dumps({"host": "box.tail1.ts.net"}))
    for name in HANDLERS:
        monkeypatch.setattr(cli.StoreHandler, name, lambda self, *a, _n=name: self._json(299, {"reached": _n}))
    from wam import disk, main_compare, recommended
    monkeypatch.setattr(disk, "status", lambda *a, **k: {})
    monkeypatch.setattr(disk, "public", lambda s: {"reached": "disk"})
    monkeypatch.setattr(main_compare, "compare", lambda c: {"reached": "main-compare"})
    monkeypatch.setattr(recommended, "listing", lambda c: {"reached": "recommended"})
    monkeypatch.setattr(cli.StoreHandler, "cfg", SimpleNamespace(home=tmp_path, index="x"), raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, headers, method="GET"):
        h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        h.request(method, "/" + path, None if method == "GET" else "{}",
                  headers if method == "GET" else {**headers, "Content-Length": "2"})
        r = h.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    yield call
    srv.shutdown()


def test_store_routes_unchanged_with_the_switch_off(store, monkeypatch):
    monkeypatch.delenv(bb.SWITCH, raising=False)
    for path in STORE_ROUTES:
        status, body = store(path, SHELL)
        assert body.get("error") != bb.REFUSED, path


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_every_store_route_refuses_an_app_page_with_the_switch_on(store, on, method):
    for path in STORE_ROUTES:
        assert store(path, SHELL, method) == (403, {"error": bb.REFUSED}), path


def test_every_store_route_still_works_for_the_shell(store, on):
    for path in STORE_ROUTES:
        status, body = store(path, {**SHELL, bb.HEADER: TOKEN})
        assert body.get("error") != bb.REFUSED and status != 403, (path, status, body)


def test_connections_view_page_files_load_without_the_token_but_its_api_does_not(store, on):
    frame = {**SHELL, "Sec-Fetch-Dest": "iframe"}
    assert store("_connections/", frame)[1] == {"reached": "_connections"}
    assert store("_connections/app.js", frame)[1] == {"reached": "_connections"}
    assert store("_connections/api/connections", frame) == (403, {"error": bb.REFUSED})
    assert store("_connections/api/connections", SHELL, "POST") == (403, {"error": bb.REFUSED})
    assert store("_connectionsx", frame) == (403, {"error": bb.REFUSED})


def test_gateway_listener_has_the_same_gate(monkeypatch, on, tmp_path):
    monkeypatch.setattr(si, "gateway_proof_ok", lambda h, home: True)
    monkeypatch.setattr(si, "gateway_origins", lambda home: {SHELL["Origin"]})
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(cli.StoreHandler, "_updates", lambda self, m: self._json(299, {"reached": "_updates"}))
    monkeypatch.setattr(cli.GatewayStoreHandler, "cfg", SimpleNamespace(home=tmp_path, index="x"), raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.GatewayStoreHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for headers, want in [(SHELL, 403), ({**SHELL, bb.HEADER: TOKEN}, 299)]:
            h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            h.request("GET", "/" + cli.UPDATES_PATH, headers=headers)
            assert h.getresponse().status == want
    finally:
        srv.shutdown()


@pytest.fixture
def host_api(monkeypatch, tmp_path):
    from wam import devices
    monkeypatch.setattr(si, "owner_login", lambda: "owner@x.com")
    monkeypatch.setattr(devices, "reader_for", lambda home: lambda: {"devices": []})
    monkeypatch.setattr(resources, "read", lambda rt, d=None: {"cpuLoad": 1})

    class Handler(cli.Handler):
        cfg = SimpleNamespace(home=tmp_path, runtipi=lambda: None, runtipi_root=None)
        token = "synthetic-api-token"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, headers):
        h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        h.request("GET", path, headers=headers)
        r = h.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    yield call
    srv.shutdown()


def test_every_host_api_route_published_beside_the_shell_is_gated(host_api, monkeypatch, tmp_path):
    """resources.PUBLISHED is the list `wam links` puts on the shell origin; a route added there later is covered."""
    f = tmp_path / "t"
    f.write_text(TOKEN)
    for path in resources.PUBLISHED:
        monkeypatch.delenv(bb.SWITCH, raising=False)
        assert host_api(path, SHELL)[0] == 200, path                       # off: as before
        monkeypatch.setenv(bb.SWITCH, "1")
        monkeypatch.setenv(bb.TOKEN_FILE, str(f))
        assert host_api(path, SHELL) == (403, {"error": bb.REFUSED}), path  # app page
        assert host_api(path, {**SHELL, bb.HEADER: TOKEN})[0] == 200, path  # the shell
