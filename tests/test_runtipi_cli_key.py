"""wam signs Runtipi CLI tokens with Runtipi 4.12's CLI key when switch wizard/runtipi-cli-key is on (WAM#839)."""
import base64
import hashlib
import hmac
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from wam import runtipi_key
from wam.runtipi import Runtipi, RuntipiError

KEY = "ab" * 32
JWT = "legacy-jwt-secret"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "home"))
    runtipi_key.clear_cache()
    yield
    runtipi_key.clear_cache()


def _root(tmp_path, port=80):
    root = tmp_path / "runtipi"
    root.mkdir(exist_ok=True)
    (root / ".env").write_text(f"INTERNAL_IP=127.0.0.1\nNGINX_PORT={port}\nJWT_SECRET={JWT}\n")
    return root


def _signed_with(token: str, secret: str) -> bool:
    head, body, sig = token.split(".")
    want = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return want.rstrip(b"=").decode() == sig


def test_off_signs_with_jwt_secret_and_removes_the_shell_copy(tmp_path, monkeypatch):
    copy = runtipi_key.key_path()
    copy.parent.mkdir(parents=True)
    copy.write_text(KEY)
    monkeypatch.setattr(runtipi_key, "switch_on", lambda home=None: False)
    monkeypatch.setattr(runtipi_key, "container_key", lambda *a, **k: pytest.fail("never asks docker when off"))
    rt = Runtipi.from_install(_root(tmp_path))
    assert rt.secret == JWT and rt.fallback is None
    assert not copy.exists()


def test_on_signs_with_the_container_key_and_keeps_a_private_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(runtipi_key, "switch_on", lambda home=None: True)
    monkeypatch.setattr(runtipi_key, "container_key", lambda *a, **k: KEY)
    rt = Runtipi.from_install(_root(tmp_path))
    assert rt.secret == KEY and rt.fallback == JWT
    copy = runtipi_key.key_path()
    assert copy.read_text() == KEY
    assert copy.stat().st_mode & 0o777 == 0o600


def test_on_with_an_older_backend_falls_back_to_jwt_secret_and_drops_a_stale_copy(tmp_path, monkeypatch):
    copy = runtipi_key.key_path()
    copy.parent.mkdir(parents=True)
    copy.write_text(KEY)
    monkeypatch.setattr(runtipi_key, "switch_on", lambda home=None: True)
    monkeypatch.setattr(runtipi_key, "container_key", lambda *a, **k: None)
    rt = Runtipi.from_install(_root(tmp_path))
    assert rt.secret == JWT and rt.fallback is None
    assert not runtipi_key.key_path().exists()


def test_the_answer_is_cached_for_a_minute(tmp_path):
    calls = []
    on = lambda home=None: calls.append(1) or True   # noqa: E731
    t = [100.0]
    args = dict(on=on, read=lambda: KEY, now=lambda: t[0])
    assert runtipi_key.secret(tmp_path, JWT, **args) == KEY
    t[0] += 30
    assert runtipi_key.secret(tmp_path, JWT, **args) == KEY
    assert len(calls) == 1
    t[0] += 31
    runtipi_key.secret(tmp_path, JWT, **args)
    assert len(calls) == 2


def test_container_key_reads_the_upstream_path_and_rejects_junk(monkeypatch):
    seen = []

    def run(argv, **k):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=out[0], stderr="")
    monkeypatch.setattr(runtipi_key.subprocess, "run", run)
    out = [KEY + "\n"]
    assert runtipi_key.container_key(["docker"]) == KEY
    assert seen[0] == ["docker", "exec", "runtipi", "cat", "/data/.secrets/cli-secret"]
    out[0] = "not-a-key"
    assert runtipi_key.container_key(["docker"]) is None

    def missing(argv, **k):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such file or directory")
    monkeypatch.setattr(runtipi_key.subprocess, "run", missing)
    assert runtipi_key.container_key(["docker"]) is None


def _server(accept: set[str]):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
            ok = next((s for s in accept if _signed_with(tok, s)), None)
            seen.append(ok)
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode() if ok else b"{}")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_a_rejected_key_is_retried_once_with_jwt_secret():
    srv, seen = _server({JWT})
    try:
        rt = Runtipi(f"http://127.0.0.1:{srv.server_port}/api", KEY, JWT)
        assert rt.req("GET", "/marketplace/all") == {"ok": True}
        assert seen == [None, JWT]
    finally:
        srv.shutdown()


def test_the_key_alone_is_used_when_runtipi_accepts_it():
    srv, seen = _server({KEY})
    try:
        rt = Runtipi(f"http://127.0.0.1:{srv.server_port}/api", KEY, JWT)
        assert rt.req("GET", "/marketplace/all") == {"ok": True}
        assert seen == [KEY]
    finally:
        srv.shutdown()


def test_no_fallback_means_a_401_is_still_an_error():
    srv, _ = _server({KEY})
    try:
        with pytest.raises(RuntipiError, match="-> 401"):
            Runtipi(f"http://127.0.0.1:{srv.server_port}/api", JWT).req("GET", "/marketplace/all")
    finally:
        srv.shutdown()


def test_switch_reads_the_feature_switch(monkeypatch):
    from wam import feature_switches
    asked = []
    monkeypatch.setattr(feature_switches, "is_on", lambda tid, home=None, pages=None, **k: asked.append(tid) or True)
    assert runtipi_key.switch_on(Path("/nonexistent")) is True
    assert asked == ["wizard/runtipi-cli-key"]
