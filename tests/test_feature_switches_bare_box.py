"""Feature switches on a box whose Agent Service is a systemd service beside Hermes (#726): no alongside.json, no
wizard-ux / wizard-agent-service rows in apps.json, the shell served by the Agent Service process itself, and the
Agent Service started with an owner proof (a loopback caller without X-Wizard-Owner-Proof is anonymous). A real
HTTP fake plays that Agent Service; nothing on the real box is touched."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wam import feature_switches as fs, updates

PROOF = "owner-proof-" + "0123456789abcdef" * 3
CATALOG = {"tweaks": ["wizard/chat-channels", "wizard/unified-inbox"], "defaults": ["wizard/unified-inbox"]}
MANIFEST = {"name": "Channels", "issues": ["humanitylabs-org/wizard-ux#1254"]}


class FakeService(BaseHTTPRequestHandler):
    """The Agent Service with an owner proof: health, the shell's tweak catalog, and owner-only Pages look.json."""
    proof = PROOF
    look = {"text": None, "version": None}
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        raw = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _owner(self):
        got = self.headers.get("X-Wizard-Owner-Proof")
        type(self).seen.append(got)
        return got == type(self).proof

    def do_GET(self):  # noqa: N802
        if self.path == "/wizard/health":
            return self._send(200, {"status": "ok", "upstream_reachable": True})
        if self.path == "/wizard-os-home/tweaks/catalog.json":
            return self._send(200, CATALOG)
        if self.path == "/wizard-os-home/tweaks/wizard/chat-channels/manifest.json":
            return self._send(200, MANIFEST)
        if self.path == fs.LOOK:
            if not self._owner():
                return self._send(403, {"error": "owner_required"})
            lk = type(self).look
            if lk["text"] is None:
                return self._send(404, {"error": "file_not_found"})
            return self._send(200, {"text": lk["text"], "version": lk["version"]})
        self._send(404, {"error": "not_found"})

    def do_PUT(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != fs.LOOK or not self._owner():
            return self._send(403, {"error": "owner_required"})
        lk = type(self).look
        if body.get("version") != lk["version"]:
            return self._send(409, {"current": {"exists": lk["text"] is not None, "text": lk["text"],
                                                "version": lk["version"]}})
        lk["text"], lk["version"] = body["text"], f"v{int(time.time() * 1000)}"
        self._send(200, {"version": lk["version"]})


@pytest.fixture
def service():
    FakeService.look = {"text": None, "version": None}
    FakeService.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def bare_box(tmp_path, monkeypatch, service):
    """No alongside.json, apps.json without the shell or the Agent Service; the service on its 'default' port."""
    h = tmp_path / "wos"
    h.mkdir()
    (h / "apps.json").write_text(json.dumps({"apps": [{"id": "wizard-notes", "health": "http://127.0.0.1:9100/h"}]}))
    monkeypatch.setenv("WIZARD_OS_HOME", str(h))
    monkeypatch.setattr(fs, "DEFAULT_SERVICE_PORT", service)
    monkeypatch.setattr(fs, "_looks_like_service", REAL_PROBE)
    fs._probe_cache.clear()
    updates.set_channel("latest", h)
    return h


REAL_PROBE = fs._looks_like_service


def _private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    path.chmod(0o600)


def test_finds_the_service_and_its_shell_without_apps_json_rows(bare_box, service):
    want = f"http://127.0.0.1:{service}"
    assert updates.service_origin(bare_box) is None          # what failed before: no alongside.json, no row
    assert fs.service_origin(bare_box) == want
    assert fs.shell_origin(bare_box) == want                 # the shell is served by the same process
    assert [t["id"] for t in fs.catalog(bare_box)] == ["wizard/chat-channels"]


def test_lists_and_flips_a_switch_with_the_owner_proof_file(bare_box, service):
    _private(bare_box / fs.OWNER_PROOF_FILE, PROOF)
    out = fs.listing(bare_box, app_rows=([], []))
    assert [(t["id"], t["on"]) for t in out["switches"]] == [("wizard/chat-channels", False)]
    out = fs.set_switch("wizard/chat-channels", True, bare_box)
    assert out["changed"]["on"] is True
    assert [(t["id"], t["on"]) for t in out["switches"] if t["kind"] == "core"] == [("wizard/chat-channels", True)]
    assert json.loads(FakeService.look["text"])["tweaks"]["wizard/chat-channels"]["on"] is True
    assert fs.is_on("wizard/chat-channels", bare_box) is True
    assert set(FakeService.seen) == {PROOF}


def test_without_a_proof_the_refusal_is_plain_not_a_missing_service(bare_box, service):
    with pytest.raises(fs.SwitchError) as e:
        fs.listing(bare_box, app_rows=([], []))
    assert "HTTP 403" in str(e.value) and e.value.code == 503
    assert FakeService.seen == [None]
    assert fs.is_on("wizard/chat-channels", bare_box) is False


def test_proof_from_wams_own_environment(bare_box, service, tmp_path, monkeypatch):
    f = tmp_path / "elsewhere" / "proof"
    _private(f, PROOF)
    monkeypatch.setenv(fs.OWNER_PROOF_ENV, str(f))
    assert fs.listing(bare_box, app_rows=([], []))["switches"][0]["id"] == "wizard/chat-channels"


def test_service_origin_from_the_environment_only_on_loopback(tmp_path, monkeypatch):
    h = tmp_path / "w"
    h.mkdir()
    monkeypatch.setenv(fs.SERVICE_ORIGIN_ENV, "http://127.0.0.1:8930/")
    assert fs.service_origin(h) == "http://127.0.0.1:8930"
    monkeypatch.setenv(fs.SERVICE_ORIGIN_ENV, "https://box.example.ts.net")
    assert fs.service_origin(h) is None                      # default port not answering (stubbed) and no remote
    (h / "alongside.json").write_text(json.dumps({"service": {"port": 8797}}))
    assert fs.service_origin(h) == "http://127.0.0.1:8797"   # the registered one still wins


def test_proof_is_never_sent_off_loopback_and_a_loose_file_is_refused(tmp_path):
    h = tmp_path / "w"
    _private(h / fs.OWNER_PROOF_FILE, PROOF)
    assert fs.owner_proof(h, "http://127.0.0.1:8923") == PROOF
    assert fs.owner_proof(h, "https://box.example.ts.net") == ""
    (h / fs.OWNER_PROOF_FILE).chmod(0o644)
    assert fs.owner_proof(h, "http://127.0.0.1:8923") == ""
    (h / fs.OWNER_PROOF_FILE).chmod(0o600)
    (h / fs.OWNER_PROOF_FILE).write_text("short\n")
    assert fs.owner_proof(h, "http://127.0.0.1:8923") == ""


def test_proof_file_named_by_the_running_service_process(tmp_path, monkeypatch):
    """The Agent Service's own WIZARD_OWNER_PROOF_FILE, read from the process listening on its port (same user)."""
    from wam import feature_switches as mod
    f = tmp_path / "svc" / "owner-proof"
    _private(f, PROOF)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, mod.OWNER_PROOF_ENV: str(f)}
    child = subprocess.Popen([sys.executable, "-c", (
        "import socket,time,sys\n"
        f"s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',{port}));"
        "s.listen();print('up',flush=True);time.sleep(30)")], env=env, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"up"
        monkeypatch.setattr(mod, "_listener_proof_file", LISTENER)
        h = tmp_path / "w"
        h.mkdir()
        assert mod._listener_proof_file(port) == str(f)
        assert mod.owner_proof(h, f"http://127.0.0.1:{port}") == PROOF
    finally:
        child.kill()
        child.wait()


LISTENER = fs._listener_proof_file
