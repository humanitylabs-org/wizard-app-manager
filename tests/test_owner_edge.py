"""The owner edge (wam/owner_edge.py): only the owner, through tailscaled, on their own device, gets a signed request.

Each test runs a real edge on a Unix socket in front of a real loopback HTTP backend and talks to it the way
Tailscale Serve does (Host: localhost, X-Forwarded-Host/-For and Tailscale-User-Login set by Serve)."""
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import owner_edge, owner_proof as op  # noqa: E402

BOX = "box.tailnet.example.test"
OWNER = "owner@example.com"
PHONE = "192.0.2.9"
BOX_IP = "192.0.2.1"


class FakeIdentity:
    def __init__(self, allow_box=False):
        self.val = {"logins": {OWNER}, "ips": {BOX_IP, "2001:db8::1"}, "host": BOX, "allow_box": allow_box}

    def current(self):
        return self.val


class EnrolledPhone:
    """The device record, for the edge tests that are about something else: every resolvable caller is enrolled."""
    def check(self, hop, login):
        return None


class Backend(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_backend(seen):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _any(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()),
                         "all": self.headers, "body": body})
            if self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Connection", "close")
                self.end_headers()
                for i in range(3):
                    self.wfile.write(f"part{i}\n".encode()); self.wfile.flush(); time.sleep(0.05)
                self.close_connection = True
                return
            out = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        do_GET = do_POST = do_HEAD = _any
    return H


@pytest.fixture
def edge():
    root = Path(tempfile.mkdtemp(prefix="oe"))
    home = root / "h"
    op.ensure_edge_key(home, "messenger")
    seen = []
    backend = Backend(("127.0.0.1", 0), make_backend(seen))
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    app = {"id": "messenger", "name": "Messenger", "port": backend.server_address[1], "activity": ["/api/drafts"],
           "strip": ["x-messenger-owner-proof"]}
    state = {"peer": True}
    srv = owner_edge.serve_app(home, app, FakeIdentity(), peer_check=lambda conn: state["peer"], devices=EnrolledPhone())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"home": home, "seen": seen, "srv": srv, "backend": backend, "state": state, "app": app,
           "key": op.edge_key_path(home, "messenger").read_bytes()}
    srv.shutdown(); srv.server_close(); backend.shutdown(); backend.server_close()
    shutil.rmtree(root, ignore_errors=True)


def serve_headers(**over):
    """What Tailscale Serve sends for the owner's phone, plus any overrides (None drops a header)."""
    h = {"Host": "localhost", "X-Forwarded-Host": f"{BOX}:8796", "X-Forwarded-For": PHONE,
         "X-Forwarded-Proto": "https", "Tailscale-User-Login": OWNER, "Tailscale-User-Name": "Owner"}
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


def test_relay_rejects_malformed_backend_header_names_and_values(edge, monkeypatch):
    """CodeQL source-to-header sink: invalid bytes never reach a browser response, valid tokens stay identical."""
    class Response:
        status = 200
        def getheaders(self):
            return [("X-Safe", "kept"), ("X-Bad\r\nInjected", "yes"), ("X-Colon:", "yes"),
                    ("X-BadValue", "ok\r\nInjected: yes")]
        def read1(self, n):
            return b""

    class Connection:
        def putrequest(self, *a, **k): pass
        def putheader(self, *a): pass
        def endheaders(self): pass
        def getresponse(self): return Response()
        def close(self): pass

    monkeypatch.setattr(owner_edge.Handler, "_connect", lambda self: Connection())
    status, headers, _ = call(edge)
    assert status == 200 and headers["x-safe"] == "kept"
    assert all(not name.lower().startswith(("x-bad", "x-colon", "injected")) for name in headers)


def test_explicit_name_sanitizer_is_identity_for_every_allowed_ascii_token():
    # The explicit sanitizer helps CodeQL recognize the existing fullmatch guard; it cannot change a name
    # accepted by that guard. Test the entire character alphabet, not just the one observed alert.
    for codepoint in range(128):
        name = "X" + chr(codepoint) + "Y"
        if owner_edge._TOKEN.fullmatch(name):
            assert name.replace("\r", "").replace("\n", "").replace(":", "") == name


def call(e, method="GET", path="/api/status", headers=None, body=b"", extra_lines=()):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    s.connect(str(op.socket_path(e["home"], "messenger")))
    hs = dict(headers if headers is not None else serve_headers())
    if body:
        hs["Content-Length"] = str(len(body))
    lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hs.items()] + list(extra_lines)
    try:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    except (BrokenPipeError, ConnectionResetError):
        pass  # the edge may answer and hang up before reading the request (a refused peer): read that answer
    data = b""
    while True:
        try:
            chunk = s.recv(65536)
        except (socket.timeout, ConnectionResetError):
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if head else 0
    hdrs = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        k, _, v = line.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, rest


def test_owner_request_is_signed_for_exactly_what_the_app_receives(edge):
    status, _, body = call(edge, path="/api/status?x=1", headers=serve_headers(**{
        "X-Wizard-App-Owner-Proof": "1.forged.sig", "X-Messenger-Owner-Proof": "forged", "X-Messenger-CSRF": "tok",
        "Cookie": "wizard_messenger_session=abc"}))
    assert status == 200 and body == b'{"ok":true}'
    got = edge["seen"][-1]
    proofs = got["all"].get_all(op.HEADER)
    assert len(proofs) == 1                                    # the client's copy was dropped, ours added
    assert op.verify(proofs[0], edge["key"], "messenger", "GET", "/api/status?x=1")
    h = {k.lower(): v for k, v in got["headers"].items()}
    assert h["host"] == f"{BOX}:8796"
    assert h["x-messenger-csrf"] == "tok" and h["cookie"] == "wizard_messenger_session=abc"   # app's own headers pass
    for gone in ("x-messenger-owner-proof", "tailscale-user-login", "tailscale-user-name", "x-forwarded-for",
                 "x-forwarded-host"):
        assert gone not in h


def test_writes_carry_their_body_and_need_the_same_origin(edge):
    origin = f"https://{BOX}:8796"
    status, _, _ = call(edge, "POST", "/api/send", serve_headers(Origin=origin, **{"Content-Type": "application/json"}),
                        b'{"text":"hi"}')
    assert status == 200 and edge["seen"][-1]["body"] == b'{"text":"hi"}'
    n = len(edge["seen"])
    assert call(edge, "POST", "/api/send", serve_headers(), b"{}")[0] == 403                      # no Origin
    assert call(edge, "POST", "/api/send", serve_headers(Origin="https://evil.example"), b"{}")[0] == 403
    assert call(edge, "GET", "/", serve_headers(**{"Sec-Fetch-Site": "cross-site"}))[0] == 403
    assert len(edge["seen"]) == n                                                                # nothing forwarded


@pytest.mark.parametrize("why,headers", [
    ("another tailnet user", serve_headers(**{"Tailscale-User-Login": "guest@example.com"})),
    ("tagged node or Funnel: no identity", serve_headers(**{"Tailscale-User-Login": None})),
    ("from this box itself", serve_headers(**{"X-Forwarded-For": BOX_IP})),
    ("from loopback", serve_headers(**{"X-Forwarded-For": "127.0.0.1"})),
    ("no source address", serve_headers(**{"X-Forwarded-For": None})),
    ("wrong host", serve_headers(**{"X-Forwarded-Host": "evil.example:8796"})),
    ("not through Serve's unix proxy", serve_headers(Host=f"{BOX}:8796")),
    ("bearer (agent) caller", serve_headers(Authorization="Bearer wzak_x")),
])
def test_non_owner_callers_are_refused_before_anything_is_forwarded_or_signed(edge, why, headers):
    status, _, body = call(edge, headers=headers)
    assert status == 403, why
    assert json.loads(body)["ok"] is False
    assert edge["seen"] == []


def test_duplicated_identity_header_fails_closed(edge):
    h = serve_headers()
    status, _, _ = call(edge, headers=h, extra_lines=[f"Tailscale-User-Login: {OWNER}"])
    assert status == 403 and edge["seen"] == []


def test_a_process_that_is_not_tailscaled_cannot_even_talk(edge):
    edge["state"]["peer"] = False
    status, _, _ = call(edge)
    assert status == 403 and edge["seen"] == []


def test_restarting_app_page_load_gets_a_waiting_page_and_api_gets_json_502(edge, monkeypatch):
    monkeypatch.setattr(owner_edge, "BACKEND_RETRY_S", 0.2)
    edge["backend"].shutdown(); edge["backend"].server_close()
    status, h, body = call(edge, path="/", headers=serve_headers(**{"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}))
    assert status == 503 and h["x-wizard-waiting"] == "1" and b"Messenger is restarting" in body
    status, _, body = call(edge, path="/api/status")
    assert status == 502 and json.loads(body)["error"]["code"] == "app_unavailable"


@pytest.mark.parametrize("state", ["stopped", "start_error", "update_error"])
def test_stopped_app_page_load_says_stopped_without_an_endless_spinner(edge, monkeypatch, state):
    # #713: a stopped app was shown as "restarting" with a spinner that never ended.
    monkeypatch.setattr(owner_edge, "BACKEND_RETRY_S", 0.2)
    asked = []
    edge["srv"].app_status = lambda app_id: asked.append(app_id) or state
    edge["backend"].shutdown(); edge["backend"].server_close()
    status, h, body = call(edge, path="/", headers=serve_headers(**{"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}))
    assert status == 503 and "x-wizard-waiting" not in h and "retry-after" not in h
    assert b"Messenger is stopped. Ask your Wizard to start it." in body
    assert b"restarting" not in body and b"animation" not in body
    assert asked == ["messenger"]
    status, _, body = call(edge, path="/api/status")
    assert status == 502 and json.loads(body)["error"]["code"] == "app_unavailable"


@pytest.mark.parametrize("state", ["restarting", "starting", "updating", ""])
def test_an_app_on_its_way_back_still_gets_the_restarting_page(edge, monkeypatch, state):
    monkeypatch.setattr(owner_edge, "BACKEND_RETRY_S", 0.2)
    edge["srv"].app_status = lambda app_id: state
    edge["backend"].shutdown(); edge["backend"].server_close()
    status, h, body = call(edge, path="/", headers=serve_headers(**{"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}))
    assert status == 503 and h["x-wizard-waiting"] == "1" and b"Messenger is restarting" in body


def test_app_status_caches_and_never_raises(tmp_path):
    calls = []
    def lookup(app_id):
        calls.append(app_id)
        if len(calls) == 1:
            return "stopped"
        raise OSError("runtipi down")
    st = owner_edge.AppStatus(tmp_path, "wizardapps", ttl=60, lookup=lookup)
    assert st("messenger") == "stopped" and st("messenger") == "stopped" and calls == ["messenger"]
    st.ttl = 0
    assert st("messenger") == "" and len(calls) == 2
    # No Runtipi install at all (no .env): unknown, so the edge keeps the restarting page.
    assert owner_edge.AppStatus(tmp_path / "none", "wizardapps", ttl=0)("messenger") == ""


def test_app_status_asks_on_a_box_that_just_booted(tmp_path, monkeypatch):
    # time.monotonic() is the uptime: seconds after boot it is below the ttl, and a never-asked app must still be asked.
    monkeypatch.setattr(owner_edge.time, "monotonic", lambda: 1.0)
    st = owner_edge.AppStatus(tmp_path, "wizardapps", ttl=60, lookup=lambda app_id: "stopped")
    assert st("messenger") == "stopped"


def test_app_status_asks_runtipi_for_the_app_in_the_wizard_store(tmp_path, monkeypatch):
    seen = []
    class RT:
        def req(self, method, path, timeout=60):
            seen.append((method, path, timeout))
            return {"app": {"status": "stopped"}}
    monkeypatch.setattr(owner_edge.Runtipi, "from_install", classmethod(lambda cls, root: RT()))
    assert owner_edge.AppStatus(tmp_path, "wizardapps")("messenger") == "stopped"
    assert seen == [("GET", "/apps/messenger:wizardapps", 3)]


def test_responses_stream_instead_of_buffering(edge):
    status, _, body = call(edge, path="/stream")
    assert status == 200 and body == b"part0\npart1\npart2\n"


def test_owner_activity_is_recorded_for_page_loads_writes_and_declared_paths_only(edge):
    marker = edge["home"] / "owner-activity" / "messenger"
    call(edge, path="/api/inbox/wait")
    assert not marker.exists()                                   # a hidden view's long poll is not "on screen"
    call(edge, path="/api/drafts")
    assert marker.exists()
    assert oct(marker.stat().st_mode & 0o777) == "0o600"


def test_socket_is_private(edge):
    p = op.socket_path(edge["home"], "messenger")
    assert oct(p.stat().st_mode & 0o777) == "0o600" and oct(p.parent.stat().st_mode & 0o777) == "0o700"


def test_identity_uses_the_node_owner_and_refuses_tagged_nodes(tmp_path):
    status = {"Self": {"UserID": 7, "DNSName": "box.tailnet.example.test.", "TailscaleIPs": [BOX_IP]},
              "User": {"7": {"LoginName": "Owner@Example.com"}}}
    assert owner_edge.Identity(tmp_path, status=lambda: status).current()["logins"] == {OWNER}
    tagged = json.loads(json.dumps(status)); tagged["Self"]["Tags"] = ["tag:server"]
    assert owner_edge.Identity(tmp_path, status=lambda: tagged).current()["logins"] == set()
    (tmp_path / "owner-edge.json").write_text(json.dumps({"owner_logins": ["Pinned@Example.com"]}))
    assert owner_edge.Identity(tmp_path, status=lambda: tagged).current()["logins"] == {"pinned@example.com"}


def test_registry_lists_only_owner_proof_apps_with_keys(tmp_path):
    op.ensure_edge_key(tmp_path, "messenger")
    (tmp_path / "apps.json").write_text(json.dumps({"apps": [
        {"id": "messenger", "name": "Messenger", "owner_proof": {"activity": [], "strip": []},
         "view": {"url": "/messenger/", "local": "http://127.0.0.1:9245/"}},
        {"id": "contacts", "view": {"url": "http://127.0.0.1:8822/"}},
        {"id": "notes", "owner_proof": {"activity": [], "strip": []}, "view": {"url": "http://127.0.0.1:9257/"}},
    ]}))
    assert list(owner_edge.apps_from_registry(tmp_path)) == ["messenger"]          # notes has no key yet
    assert owner_edge.apps_from_registry(tmp_path)["messenger"]["port"] == 9245


def test_the_edge_never_relays_a_cr_or_lf_inside_a_header(edge, monkeypatch):
    """http.client already splits bare CR/LF while parsing, so this pins the edge's own filter (CodeQL
    py/http-response-splitting): a value that still carried one must be dropped, never written raw."""
    import http.client
    real = http.client.HTTPResponse.getheaders
    monkeypatch.setattr(http.client.HTTPResponse, "getheaders",
                        lambda self: real(self) + [("X-Bad", "a\r\nSet-Cookie: evil=1"), ("Bad Name", "x")])
    status, h, body = call(edge, path="/api/status")
    assert status == 200 and body == b'{"ok":true}'
    assert "set-cookie" not in h and "x-bad" not in h and "bad name" not in h
