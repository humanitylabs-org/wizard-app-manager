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

BOX = "box.tail1234.ts.net"
OWNER = "owner@example.com"
PHONE = "100.64.0.9"
BOX_IP = "100.64.0.1"


class FakeIdentity:
    def __init__(self, allow_box=False):
        self.val = {"logins": {OWNER}, "ips": {BOX_IP, "fd7a:115c:a1e0::1"}, "host": BOX, "allow_box": allow_box}

    def current(self):
        return self.val


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
    srv = owner_edge.serve_app(home, app, FakeIdentity(), peer_check=lambda conn: state["peer"])
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


def call(e, method="GET", path="/api/status", headers=None, body=b"", extra_lines=()):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    s.connect(str(op.socket_path(e["home"], "messenger")))
    hs = dict(headers if headers is not None else serve_headers())
    if body:
        hs["Content-Length"] = str(len(body))
    lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hs.items()] + list(extra_lines)
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
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
    status = {"Self": {"UserID": 7, "DNSName": "box.tail1234.ts.net.", "TailscaleIPs": [BOX_IP]},
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
