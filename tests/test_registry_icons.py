"""Launcher icons: the registry offers an app's own silhouette (served at <view>/icon.svg) to the shell."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wam import registry

SILHOUETTE = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect x="6" y="342" width="164" '
              b'height="164" rx="40"/><rect x="342" y="6" width="164" height="164" rx="40"/></svg>')
# A cut-out mark: full-canvas rects inside <mask> and a masked shape are still a silhouette.
MASKED = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><mask id="b"><rect width="512" '
          b'height="512" fill="#fff"/><circle cx="256" cy="430" r="15"/></mask><path mask="url(#b)" d="M196 0h120'
          b'v512H196Z"/><rect mask="url(#b)" width="512" height="512"/></svg>')
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><rect width="128" height="128" rx="28" '
           b'fill="#17132b"/><circle cx="54" cy="54" r="28" fill="none" stroke="#c4aeff"/></svg>')
FAVICON_PCT = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="100%" height="100%"/></svg>'
DISC = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="50"/></svg>'
RASTER = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><image href="data:image/png;base64,AA=="'
          b' width="5" height="5"/></svg>')


@pytest.mark.parametrize("body,ok", [
    (SILHOUETTE, True), (MASKED, True), (FAVICON, False), (FAVICON_PCT, False), (DISC, False), (RASTER, False),
    (b'<!doctype html><html></html>', False), (b'<svg', False),
    (b'<!DOCTYPE svg [<!ENTITY a "x">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>', False),
    (SILHOUETTE[:-6] + b' ' * registry.ICON_MAX_BYTES + b'</svg>', False),
])
def test_only_silhouettes_are_offered(body, ok):
    assert registry.svg_silhouette(body) is ok


class _App(BaseHTTPRequestHandler):
    files = {}

    def do_GET(self):  # noqa: N802
        hit = self.files.get(self.path)
        if not hit:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = hit
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def app_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _app(app_id, local, url=None):
    return {"id": app_id, "name": app_id, "provides": [], "view": {"url": url or f"/{app_id}/", "local": local}}


def test_resolve_and_project_icons(app_server, tmp_path):
    _App.files = {
        "/own/icon.svg": ("image/svg+xml", SILHOUETTE),
        "/fav/icon.svg": ("image/svg+xml; charset=utf-8", FAVICON),
        "/spa/icon.svg": ("text/html", b"<!doctype html>"),
    }
    reg = {"apps": [_app("own", f"{app_server}/own/"), _app("fav", f"{app_server}/fav/"),
                    _app("spa", f"{app_server}/spa/"), _app("none", f"{app_server}/none/"),
                    # published on its own origin: never a same-origin launcher icon
                    {"id": "raw", "view": {"url": f"{app_server}/own/"}}], "slots": {}}
    registry.resolve_icons(reg, {})
    assert [a["id"] for a in reg["apps"] if a.get("_icon")] == ["own", "raw"]
    registry.write(reg, tmp_path)
    out = {a["id"]: a for a in json.loads((tmp_path / "apps.json").read_text())["apps"]}
    assert out["own"]["icon"] == "/own/icon.svg"
    assert all("icon" not in out[i] for i in ("fav", "spa", "none", "raw"))
    assert all(not k.startswith("_") for a in out.values() for k in a)


def test_unreachable_app_keeps_previous_answer_and_new_answers_win(app_server):
    dead = "http://127.0.0.1:9/"   # discard port: connection refused
    prev = {"apps": [{"id": "down", "icon": "/down/icon.svg"}, {"id": "gone", "icon": "/gone/icon.svg"}]}
    _App.files = {}
    reg = {"apps": [_app("down", dead), _app("gone", f"{app_server}/gone/"), _app("fresh", dead)]}
    registry.resolve_icons(reg, prev, timeout=0.5)
    assert {a["id"]: bool(a.get("_icon")) for a in reg["apps"]} == {"down": True, "gone": False, "fresh": False}


def test_owner_route_moves_the_icon_with_the_view(app_server, tmp_path):
    _App.files = {"/own/icon.svg": ("image/svg+xml", SILHOUETTE)}
    (tmp_path / "routes.json").write_text(json.dumps({"own": {"view": "/own-next/"}, "gone": {}}))
    reg = {"apps": [_app("own", f"{app_server}/own/"), {**_app("gone", f"{app_server}/own/"), "_icon": "icon.svg"}],
           "slots": {}}
    registry.resolve_icons(reg, {})
    registry.write(reg, tmp_path)
    out = {a["id"]: a for a in json.loads((tmp_path / "apps.json").read_text())["apps"]}
    assert out["own"]["icon"] == "/own-next/icon.svg"
    assert out["gone"]["view"] is None and "icon" not in out["gone"]


def test_rewriting_a_public_registry_keeps_only_icons_under_the_view(tmp_path):
    # store_install._links re-writes the public projection after moving views; a stale icon must not survive.
    reg = {"apps": [{"id": "a", "view": {"url": "/a/"}, "icon": "/a/icon.svg"},
                    {"id": "b", "view": {"url": "/b2/"}, "icon": "/b/icon.svg"},
                    {"id": "c", "view": {"url": "/c/"}, "icon": "https://evil.example/icon.svg"}], "slots": {}}
    registry.write(reg, tmp_path)
    out = {a["id"]: a.get("icon") for a in json.loads((tmp_path / "apps.json").read_text())["apps"]}
    assert out == {"a": "/a/icon.svg", "b": None, "c": None}


def test_probe_can_be_switched_off(app_server, monkeypatch):
    _App.files = {"/own/icon.svg": ("image/svg+xml", SILHOUETTE)}
    monkeypatch.setenv("WAM_ICON_PROBE", "0")
    reg = {"apps": [_app("own", f"{app_server}/own/")]}
    registry.resolve_icons(reg, {})
    assert "_icon" not in reg["apps"][0]


@pytest.mark.parametrize("url", ["//evil/", "/a/../b/", "https://x/", None, "/a b/"])
def test_icon_url_is_same_origin_only(url):
    assert registry.launcher_icon_url({"url": url}, "icon.svg") is None
