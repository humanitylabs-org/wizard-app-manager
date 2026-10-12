"""Contract fixtures: real HTTP bridge/client/JWT, no live Runtipi or Hermes mutations."""
import base64
import hashlib
import hmac
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import yaml

from test_wam import _fake_store_app
from wam import alongside, registry
from wam.lifecycle import Bridge, BridgeError, Server, token_file
from wam.runtipi import Runtipi

TOKEN = "dedicated-fixture-bridge-token-123456789"
SECRET = "runtipi-fixture-jwt-secret-123456789"
URN = "hello-wizard:wizardapps"


class TipiHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _auth(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        head, payload, signature = token.split(".")
        digest = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), (head + "." + payload).encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        assert hmac.compare_digest(digest, signature)
        body = json.loads(base64.urlsafe_b64decode(payload + "=="))
        assert body["sub"] == "cli" and body["exp"] > time.time()
        self.server.jwt_calls += 1

    def _json(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._auth()
        s = self.server
        if self.path == "/api/sse/app":
            events = queue.Queue()
            s.subscribers.append(events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": subscribed\n\n")
            self.wfile.flush()
            try:
                event = events.get(timeout=4)
                if event is not None:
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    self.wfile.flush()
            finally:
                s.subscribers.remove(events)
            return
        if self.path == "/api/apps/installed":
            return self._json({"installed": s.rows()})
        if self.path == f"/api/apps/{URN}":
            return self._json(s.detail())
        if self.path in {"/api/marketplace/all", "/api/marketplace/enabled"}:
            return self._json({"appStores": [{"slug": "wizardapps", "name": "Wizard Apps", "enabled": True}]})
        if self.path.startswith("/api/marketplace/apps/search?"):
            s.search_path = self.path
            return self._json({"data": [s.detail()["info"]], "nextCursor": None})
        if self.path == "/api/system/load":
            return self._json({"cpu": 0.1, "memory": {"used": 123}, "jwt_secret": SECRET})
        if self.path == f"/api/marketplace/apps/{URN}/image":
            if s.redirect_image:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/must-not-follow")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(b"\x89PNG\r\nfixture-image")
            return
        self.send_error(404)

    def mutate(self):
        self._auth()
        s = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        action = self.path.rsplit("/", 1)[1]
        s.mutations.append((self.command, self.path, body))
        s.status = {"install": "running", "start": "running", "stop": "stopped", "restart": "running",
                    "update": "running", "uninstall": "missing"}[action]
        if s.fail_action == action:
            s.status = "stopped"
        self._json({"requestId": "12345678-1234-1234-1234-123456789abc"})
        event = {"appUrn": URN, "event": action + ("_error" if s.fail_action == action else "_success"),
                 "error": SECRET + TOKEN}
        for events in list(s.subscribers):
            events.put(None if s.lose_completion else event)

    do_POST = do_DELETE = do_PATCH = mutate


@pytest.fixture
def bridge(tmp_path):
    root = tmp_path / "tipi"
    source = _fake_store_app(root)
    wizard = source / "wizard.md"
    meta = registry._front(wizard.read_text())
    meta.update(view={"path": "/ui/", "source": "legacy-view", "backend": {"kind": "http", "token": SECRET}},
                api={"path": "/api", "source": "fixture", "authorization": SECRET},
                source="https://example.test/source?access_token=must-not-leak", backend={"kind": "python", "password": SECRET},
                replaces_view="old-view", summary=SECRET)
    wizard.write_text("---\n" + yaml.safe_dump(meta) + "---\n")
    tipi = ThreadingHTTPServer(("127.0.0.1", 0), TipiHandler)
    tipi.daemon_threads = True
    tipi.status = "missing"
    tipi.mutations, tipi.subscribers = [], []
    tipi.jwt_calls, tipi.fail_action, tipi.lose_completion, tipi.redirect_image = 0, None, False, False

    def detail():
        return {"app": None if tipi.status == "missing" else {
            "status": tipi.status, "port": 8799, "version": 1, "config": {"PW": SECRET}, "password": SECRET},
            "info": {"id": "hello-wizard", "urn": URN, "name": "Hello", "source": "https://example.test",
                     "form_fields": [{"env_variable": "PW", "type": "password", "required": True, "default": SECRET}],
                     "api": {"path": "/api", "token": SECRET}},
            "metadata": {"latestVersion": 2, "innocent": TOKEN, "secret": SECRET}}
    tipi.detail = detail
    tipi.rows = lambda: [] if tipi.status == "missing" else [detail()]
    thread = threading.Thread(target=tipi.serve_forever, daemon=True)
    thread.start()
    cfg = SimpleNamespace(home=tmp_path / "home", runtipi_root=root, secrets=tmp_path / "home" / "secrets")
    b = Bridge(cfg, TOKEN, rt=Runtipi(f"http://127.0.0.1:{tipi.server_port}/api", SECRET), timeout=0.5, poll=0.01)
    b.tipi = tipi
    try:
        yield b
    finally:
        tipi.shutdown()
        tipi.server_close()
        thread.join(timeout=5)


def op(b, action, key="fixture-key-000001", body=None):
    method = {"update": "PATCH", "uninstall": "DELETE"}.get(action, "POST")
    return b.operate(method, URN, action, body or {}, key)


def assert_clean(value):
    text = json.dumps(value)
    assert TOKEN not in text and SECRET not in text and "must-not-leak" not in text
    assert "_view_upstream" not in text and "_skill_src" not in text


def test_complete_lifecycle_and_projection(bridge):
    b = bridge
    for i, action in enumerate(["install", "stop", "start", "restart", "update", "uninstall"]):
        code, result = op(b, action, f"fixture-key-{i:06d}")
        assert code == 200 and result["success"] and not result["replayed"]
        assert_clean(result)
        for root in [b.cfg.home, b.cfg.runtipi_root / "app-data" / "wizard-os"]:
            data = json.loads((root / "apps.json").read_text())
            assert data == result["registry"]
            assert_clean(data)
            if action != "uninstall":
                app = data["apps"][0]
                assert app["view"]["url"] == "/apps/hello-wizard/"
                assert app["view"]["source"] == "legacy-view"
                assert app["api"]["path"] == "/api"
                assert app["backend"] == {"kind": "python"}
                assert app["replaces_view"] == "old-view"
                assert (root / "skills/hello-wizard/SKILL.md").is_file()
            else:
                assert data["apps"] == [] and not (root / "skills/hello-wizard/SKILL.md").exists()
    assert len(b.tipi.mutations) == 6
    assert b.tipi.mutations[0][2]["openPort"] is False
    assert b.tipi.mutations[4][0] == "PATCH" and b.tipi.mutations[4][2] == {"performBackup": True}
    assert b.tipi.mutations[5][2] == {"removeBackups": False}
    assert b.tipi.jwt_calls > 6


def test_persistent_duplicate_concurrent_and_conflict(bridge):
    b = bridge
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(lambda _: op(b, "install"), range(6)))
    assert all(code == 200 for code, _ in results)
    assert sum(not result["replayed"] for _, result in results) == 1
    assert len(b.tipi.mutations) == 1
    new = Bridge(b.cfg, TOKEN, rt=b.rt, timeout=0.5)
    assert op(new, "install")[1]["replayed"]
    with pytest.raises(BridgeError, match="idempotency_conflict"):
        op(new, "stop")
    assert len(b.tipi.mutations) == 1
    assert (b.operations.stat().st_mode & 0o777) == 0o600
    assert SECRET not in b.operations.read_text() and TOKEN not in b.operations.read_text()


def test_failure_refresh_and_explicit_retry(bridge):
    b = bridge
    b.tipi.fail_action = "install"
    code, result = op(b, "install")
    assert code == 422 and result["error"] == "lifecycle_failed"
    assert result["registry"]["apps"][0]["status"] == "stopped"
    assert (b.cfg.home / "skills/hello-wizard/SKILL.md").exists()
    assert_clean(result)
    assert op(b, "install")[1]["replayed"]
    assert len(b.tipi.mutations) == 1
    b.tipi.fail_action = None
    assert op(b, "start", "fixture-retry-000001")[1]["status"] == "running"


def test_lost_receipt_never_resubmits_after_process_restart(bridge):
    b = bridge
    b.tipi.lose_completion = True
    code, result = op(b, "install")
    assert code == 504 and result["error"] == "completion_unconfirmed"
    assert result["registry"]["apps"][0]["status"] == "running"
    reopened = Bridge(b.cfg, TOKEN, rt=b.rt)
    for action, key in [("install", "fixture-key-000001"), ("restart", "fixture-new-key-00002")]:
        with pytest.raises(BridgeError, match="operation_unconfirmed"):
            op(reopened, action, key)
    assert len(b.tipi.mutations) == 1


def test_read_contract_and_private_view_target(bridge):
    b = bridge
    op(b, "install")
    rows = b.read("/api/apps/installed")
    assert rows["installed"][0]["wizard"]["view"]["url"] == "/apps/hello-wizard/"
    assert "config" not in rows["installed"][0]["app"]
    assert "default" not in rows["installed"][0]["info"]["form_fields"][0]
    assert_clean(rows)
    target = b.read("/api/internal/apps/hello-wizard/view")
    assert target == {"upstream": "http://127.0.0.1:8799/ui/", "path": "/apps/hello-wizard/"}
    assert (b.cfg.home / "view-upstreams.json").stat().st_mode & 0o777 == 0o600
    assert_clean(b.read("/api/apps/" + URN))
    assert b.read("/api/system/load") == {"cpu": 0.1, "memory": {"used": 123}}
    assert_clean(b.read("/api/marketplace/apps/search", "search=hello&pageSize=24&storeId=other"))
    assert "storeId=wizardapps" in b.tipi.search_path and "storeId=other" not in b.tipi.search_path
    assert b.image(URN)[1] == "image/png"
    b.tipi.redirect_image = True
    with pytest.raises(urllib.error.HTTPError) as error:
        b.image(URN)
    assert error.value.code == 302


def test_http_auth_allowlist_methods_and_symbolic_errors(bridge):
    b = bridge
    srv = Server(("127.0.0.1", 0), b)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def request(path, *, method="GET", auth=True, body=None, extra=None):
        headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
        headers.update(extra or {})
        req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(), headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    try:
        for method in ["GET", "POST", "PATCH", "DELETE"]:
            assert request("/api/apps/installed", method=method, auth=False)[0] == 401
        assert request("/api/apps/installed", extra={"Origin": "http://localhost"})[0] == 401
        assert request("/api/app-lifecycle/" + URN + "/install", method="POST", body={})[0] == 400
        assert request("/api/app-lifecycle/" + URN + "/reset", method="POST", body={},
                       extra={"Idempotency-Key": "fixture-http-key-0001"})[0] == 405
        assert request("/api/auth/register", method="POST", body={})[0] == 404
        assert request("/api/apps/%2e%2e")[0] == 400
        code, result = request("/api/app-lifecycle/" + URN + "/install", method="POST", body={},
                               extra={"Idempotency-Key": "fixture-http-key-0001"})
        assert code == 200 and result["success"]
        code, rows = request("/api/apps/installed")
        assert code == 200 and rows["installed"][0]["app"]["status"] == "running"
        assert_clean(rows)
        assert request("/api/marketplace/apps/search", extra={"Authorization": "Bearer wrong"})[0] == 401
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
    with pytest.raises(ValueError, match="loopback"):
        Server(("0.0.0.0", 0), b)


def test_lookup_failure_is_not_missing_and_refresh_failure_is_explicit(bridge, monkeypatch):
    b = bridge
    op(b, "install")
    before = (b.cfg.home / "apps.json").read_bytes()
    monkeypatch.setattr(b.rt, "installed", lambda: (_ for _ in ()).throw(OSError(SECRET + TOKEN)))
    with pytest.raises(OSError):
        b._status(URN)
    with pytest.raises(BridgeError, match="projection_refresh_failed"):
        op(b, "stop", "fixture-failure-key-0001")
    assert (b.cfg.home / "apps.json").read_bytes() == before


def test_token_permissions_and_persistence(tmp_path):
    p = tmp_path / "token"
    first = token_file(p)
    assert token_file(p) == first and p.stat().st_mode & 0o777 == 0o600
    p.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        token_file(p)


def test_preserve_system_and_modified_app_skills(bridge):
    b = bridge
    for root in [b.cfg.home, b.cfg.runtipi_root / "app-data" / "wizard-os"]:
        system = root / "skills/system-owned/SKILL.md"
        registry.atomic_write(system, b"system-skill")
        external = root / "system-skills/wizard-builder/SKILL.md"
        registry.atomic_write(external, b"separately-linked-builder")
    op(b, "install")
    custom = b.cfg.home / "skills/hello-wizard/SKILL.md"
    custom.write_text("user-modified")
    op(b, "uninstall", "fixture-remove-key-001")
    assert custom.read_text() == "user-modified"
    for root in [b.cfg.home, b.cfg.runtipi_root / "app-data" / "wizard-os"]:
        assert (root / "skills/system-owned/SKILL.md").read_text() == "system-skill"
        assert (root / "system-skills/wizard-builder/SKILL.md").read_text() == "separately-linked-builder"


def test_modular_bridge_selection_and_override(tmp_path):
    assert alongside.is_bridge_app("wizard-agent-service")
    assert alongside.is_bridge_app("wizard-shell")
    assert not alongside.is_bridge_app("wizard-ux")
    hh = tmp_path / "hermes"
    hh.mkdir()
    st = {"agent": {"hermes_home": str(hh), "origin": "http://127.0.0.1:8642", "uid": 1000, "gid": 1000},
          "shell": {"port": 8796, "share_history": False}}
    text = alongside.shell_override(st, "wizard-agent-service", tmp_path / "wos")
    assert "  wizard-agent-service:" in text and "  wizard-shell:" not in text and "wizard-ux:" not in text
    assert "HERMES_ORIGIN: http://127.0.0.1:8642" in text
