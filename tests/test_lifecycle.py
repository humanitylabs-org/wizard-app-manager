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
from wam.lifecycle import Bridge, BridgeError, BridgeRuntipi, Server, UpstreamFailure, token_file
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
        if s.reject_action == action:
            self.send_response(400)
            self.end_headers()
            self.wfile.write((SECRET + TOKEN).encode())
            return
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
    tipi.reject_action = None
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
    b = Bridge(cfg, TOKEN, rt=BridgeRuntipi(f"http://127.0.0.1:{tipi.server_port}/api", SECRET), timeout=0.5, poll=0.01)
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


def test_lifecycle_refresh_preserves_main_grants_in_both_copies(bridge):
    b = bridge
    approved = [{"appId": "hello-wizard", "provider": "fixture", "scope": "read"}]
    b.cfg.home.mkdir(parents=True, exist_ok=True)
    (b.cfg.home / "apps.json").write_text(json.dumps({"grants": approved}))
    reg, _ = b.refresh()
    assert reg["grants"] == approved
    for root in [b.cfg.home, b.cfg.runtipi_root / "app-data" / "wizard-os"]:
        assert json.loads((root / "apps.json").read_text())["grants"] == approved


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
    original = b.rt.installed
    monkeypatch.setattr(b.rt, "installed", lambda: (_ for _ in ()).throw(OSError(SECRET + TOKEN)))
    with pytest.raises(OSError):
        b._status(URN)
    with pytest.raises(BridgeError, match="projection_refresh_failed"):
        op(b, "stop", "fixture-failure-key-0001")
    assert (b.cfg.home / "apps.json").read_bytes() == before
    monkeypatch.setattr(b.rt, "installed", original)
    assert op(b, "stop", "fixture-recovered-key-0001")[1]["status"] == "stopped"


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


def test_declared_service_gateway_projection(bridge):
    b = bridge
    (b.cfg.home / "app-manager.json").write_text(json.dumps({"service_app_host": "host.docker.internal"}))
    fresh = Bridge(b.cfg, TOKEN, rt=b.rt, timeout=0.5, poll=0.01)
    _, result = op(fresh, "install")
    app = result["registry"]["apps"][0]
    assert app["mcp"]["url"] == "http://host.docker.internal:8799/mcp"
    assert app["health"] == "http://host.docker.internal:8799/healthz"
    assert app["view"]["url"] == "/apps/hello-wizard/"
    assert fresh.read("/api/internal/apps/hello-wizard/view")["upstream"] == "http://host.docker.internal:8799/ui/"
    assert fresh.rt.base.startswith("http://127.0.0.1:")
    for invalid in ["http://host.docker.internal", "host:8080", "user@host", "host/path", "host?token=secret",
                    "host\nInjected: x", "bad..host", "-bad", "999.999.1.1", "", None]:
        with pytest.raises(ValueError):
            registry.service_app_host(invalid)


def test_corrupt_journal_fails_closed_and_refreshes(bridge):
    b = bridge
    op(b, "install")
    b.operations.write_text("{truncated")
    with pytest.raises(BridgeError, match="operation_journal_invalid"):
        op(b, "restart", "fixture-corrupt-key-0001")
    assert len(b.tipi.mutations) == 1
    assert json.loads((b.cfg.home / "apps.json").read_text())["apps"][0]["status"] == "running"


def test_generated_password_reused_and_atomic(bridge):
    b = bridge
    info = {"form_fields": [{"env_variable": "PW", "type": "password", "required": True}]}
    first = b._install_form("hello-wizard", info, {}, [])
    second = b._install_form("hello-wizard", info, {}, [])
    assert first == second and len(first["PW"]) >= 32
    p = b.cfg.home / "secrets/hello-wizard.json"
    assert p.stat().st_mode & 0o777 == 0o600
    assert json.loads(p.read_text()) == {"PW": first["PW"]}
    assert not list(p.parent.glob(".*.json.*"))


def test_skill_source_symlink_cannot_copy_host_secret(bridge):
    b = bridge
    b.tipi.status = "running"
    source = b.cfg.runtipi_root / "repos/wizardapps/apps/hello-wizard/SKILL.md"
    private = b.cfg.runtipi_root / "private.env"
    private.write_text(SECRET)
    source.unlink()
    source.symlink_to(private)
    reg, _ = b.refresh()
    assert reg["apps"][0]["metadata_error"] == "metadata_invalid"
    assert reg["apps"][0]["skill"] is None
    assert not (b.cfg.home / "skills/hello-wizard/SKILL.md").exists()


@pytest.mark.parametrize("text", [
    "---\nview: {path: 'http://bad/'}\n---\n",
    "---\nprovides: [{bad: value}]\n---\n",
    "---\nview: [broken\n---\n",
    "---\nsource: 2026-10-12\n---\n",
])
def test_bad_metadata_remains_visible_and_uninstallable(bridge, text):
    b = bridge
    op(b, "install")
    (b.cfg.runtipi_root / "repos/wizardapps/apps/hello-wizard/wizard.md").write_text(text)
    reg, _ = b.refresh()
    assert reg["apps"][0]["metadata_error"] == "metadata_invalid"
    assert reg["apps"][0]["view"] is None
    assert op(b, "uninstall", "fixture-quarantine-key-0001")[0] == 200


def test_skill_write_failure_retains_generated_ownership(bridge, monkeypatch):
    b = bridge
    op(b, "install")
    source = b.cfg.runtipi_root / "repos/wizardapps/apps/hello-wizard/SKILL.md"
    source.write_text("generated-v2")
    original = registry.atomic_write
    def interrupted(path, data, mode=0o644):
        if path == b.cfg.home / "apps.json":
            raise OSError("fixture-fault-after-skill-write")
        return original(path, data, mode)
    monkeypatch.setattr(registry, "atomic_write", interrupted)
    with pytest.raises(OSError):
        b.refresh()
    assert (b.cfg.home / "skills/hello-wizard/SKILL.md").read_text() == "generated-v2"
    monkeypatch.setattr(registry, "atomic_write", original)
    source.write_text("generated-v3")
    b.refresh()
    assert (b.cfg.home / "skills/hello-wizard/SKILL.md").read_text() == "generated-v3"
    op(b, "uninstall", "fixture-ownership-key-0001")
    assert not (b.cfg.home / "skills/hello-wizard/SKILL.md").exists()


def test_reads_do_not_wait_for_operation_lock(bridge):
    b = bridge
    op(b, "install")
    with ThreadPoolExecutor(max_workers=1) as worker:
        with b.serialized():
            rows = worker.submit(b.read, "/api/apps/installed").result(timeout=2)
            assert rows["snapshot"] is True and rows["installed"][0]["wizard"]["id"] == "hello-wizard"
            assert_clean(rows)
            load = worker.submit(b.read, "/api/system/load").result(timeout=2)
            assert load["cpu"] == 0.1
    # Same contract for another Bridge/process scope sharing the filesystem lock.
    another = Bridge(b.cfg, TOKEN, rt=b.rt)
    with b.serialized():
        assert another.read("/api/apps/installed")["snapshot"] is True


def test_secret_named_slot_is_not_dropped(bridge):
    b = bridge
    wizard = b.cfg.runtipi_root / "repos/wizardapps/apps/hello-wizard/wizard.md"
    meta = registry._front(wizard.read_text())
    meta["provides"] = ["password-manager", "secret-store", "api-key-vault"]
    wizard.write_text("---\n" + yaml.safe_dump(meta) + "---\n")
    _, result = op(b, "install")
    assert set(result["registry"]["slots"]) == {"password-manager", "secret-store", "api-key-vault", "chats"}


def test_definite_rejection_is_retryable_and_form_types_checked(bridge):
    b = bridge
    for i, value in enumerate([None, {"a": 1}, ["a"], float("nan")]):
        code, result = op(b, "install", f"fixture-types-key-{i:06d}", {"PW": value})
        assert code == 400 and result["error"] == "invalid_form"
    b.tipi.reject_action = "install"
    code, result = op(b, "install", "fixture-rejected-key-001")
    assert code == 502 and result["error"] == "upstream_rejected"
    assert_clean(result)
    b.tipi.reject_action = None
    assert op(b, "install", "fixture-recovered-key-002")[0] == 200
    assert len(b.tipi.mutations) == 1


def test_token_creation_concurrent_publish_is_complete(tmp_path):
    p = tmp_path / "token"
    with ThreadPoolExecutor(max_workers=8) as workers:
        values = list(workers.map(lambda _: token_file(p), range(8)))
    assert len(set(values)) == 1 and len(values[0]) >= 32
    assert not list(p.parent.glob(".lifecycle-token.*"))


def test_shared_root_symlink_and_bad_shapes_are_safe(bridge, tmp_path):
    b = bridge
    op(b, "install")
    shared = b.cfg.runtipi_root / "app-data/wizard-os"
    (shared / "apps.json").write_text(json.dumps({"slots": []}))
    (shared / ".generated-app-skills.json").write_text("[]")
    b.refresh()
    import shutil
    shutil.rmtree(shared / "skills")
    target = tmp_path / "private-skills"
    target.mkdir()
    (shared / "skills").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="skills root"):
        b.refresh()
    assert list(target.iterdir()) == []


def test_module_serve_uses_production_loopback_client(bridge):
    import os
    import subprocess
    import sys
    b = bridge
    (b.cfg.runtipi_root / ".env").write_text(
        f"INTERNAL_IP=10.99.99.99\nNGINX_PORT={b.tipi.server_port}\nJWT_SECRET={SECRET}\n")
    env = {**os.environ, "WIZARD_OS_HOME": str(b.cfg.home), "RUNTIPI_ROOT": str(b.cfg.runtipi_root),
           "WIZARD_APP_MANAGER_TOKEN": TOKEN}
    proc = subprocess.Popen([sys.executable, "-m", "wam.lifecycle", "serve", "--port", "0"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline().strip()
        assert line.startswith("WAM lifecycle bridge on 127.0.0.1:"), proc.stderr.read() if proc.poll() is not None else line
        port = int(line.rsplit(":", 1)[1])
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/app-lifecycle/{URN}/install", b"{}",
                {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "fixture-process-key-001"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.load(response)
            assert result["success"] and result["status"] == "running"
            assert_clean(result)
        assert len(b.tipi.mutations) == 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


def test_postaccept_verification_rejection_remains_uncertain(bridge, monkeypatch):
    b = bridge
    original = b.rt.installed
    def after_delivery():
        if b.tipi.mutations:
            raise UpstreamFailure(rejected=True)  # Received 401/connection refusal on VERIFY, not lifecycle POST.
        return original()
    monkeypatch.setattr(b.rt, "installed", after_delivery)
    with pytest.raises(BridgeError, match="projection_refresh_failed"):
        op(b, "install", "fixture-postaccepted-key-001")
    journal = json.loads(b.operations.read_text())
    assert next(iter(journal.values()))["state"] == "uncertain"
    monkeypatch.setattr(b.rt, "installed", original)
    with pytest.raises(BridgeError, match="operation_unconfirmed"):
        op(b, "restart", "fixture-postaccepted-key-002")
    assert len(b.tipi.mutations) == 1


def test_control_requests_ignore_proxy_environment(bridge, monkeypatch):
    b = bridge
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert op(b, "install")[0] == 200  # Exercises direct control, SSE subscription and status verification.
    assert b.image(URN)[1] == "image/png"
    assert b.read("/api/system/load")["cpu"] == 0.1


def test_shared_only_skill_collision_keeps_same_projection(bridge):
    b = bridge
    shared = b.cfg.runtipi_root / "app-data/wizard-os"
    registry.atomic_write(shared / "skills/hello-wizard/SKILL.md", b"system-owned-collision")
    _, result = op(b, "install")
    assert result["registry"]["apps"][0]["skill"] is None
    assert json.loads((b.cfg.home / "apps.json").read_text()) == result["registry"]
    assert json.loads((shared / "apps.json").read_text()) == result["registry"]
    assert (shared / "skills/hello-wizard/SKILL.md").read_bytes() == b"system-owned-collision"


def test_dependency_on_a_slot_is_filled_by_any_installed_provider(bridge):
    """The split shell's recipe says `uses: [agent-service]`, a slot: an installed app providing it satisfies it."""
    b = bridge
    wizard = b.cfg.runtipi_root / "repos/wizardapps/apps/hello-wizard/wizard.md"
    meta = registry._front(wizard.read_text())
    meta["uses"] = ["agent-service"]
    wizard.write_text("---\n" + yaml.safe_dump(meta) + "---\n")
    with pytest.raises(BridgeError) as e:
        b._form(URN, "install", {}, [], reg={"apps": []})
    assert e.value.reason == "dependencies_required"
    provider = {"apps": [{"id": "wizard-agent-service", "provides": ["agent-service"]}]}
    assert b._form(URN, "install", {}, [], noop=True, reg=provider) == {}
    code, result = op(b, "install")   # the real path: nothing provides the slot on this box, so still refused
    assert code == 409 and result["error"] == "dependencies_required" and not b.tipi.mutations
