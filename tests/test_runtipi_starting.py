"""wam#937: a Runtipi that is still starting is waited for, not treated as having no operator.

Right after the installer recreates the runtipi container, Runtipi answers 502/404 while its proxy route comes up,
and 401 to the CLI token while its database is not ready (the auth middleware swallows the database error).
The old ensure_operator took that 401 as "no operator", called register, got ADMIN_ALREADY_EXISTS and raised, so
`wam recover` failed and a fresh install on a slower machine ended `failed`.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wam.runtipi import Runtipi, RuntipiError

SECRET = "s" * 32
EXISTS = (403, {"statusCode": 403, "message": "AUTH_ERROR_ADMIN_ALREADY_EXISTS", "path": "/api/auth/register"})
UNAUTH = (401, {"statusCode": 401, "message": "Unauthorized"})
OK = (200, {"appStores": []})


def _server(gets, registers):
    """Answers GET /api/marketplace/all and POST /api/auth/register from the given lists (last one repeats)."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def _answer(self, answers):
            code, body = answers.pop(0) if len(answers) > 1 else answers[0]
            raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            seen.append(("GET", self.path))
            self._answer(gets)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append(("POST", self.path))
            self._answer(registers)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


@pytest.fixture
def box():
    started = []

    def make(gets, registers=None):
        srv, seen = _server(list(gets), list(registers or [(500, {"message": "register must not be called"})]))
        started.append(srv)
        rt = Runtipi(f"http://127.0.0.1:{srv.server_port}/api", SECRET)
        naps = []
        rt.sleep = naps.append
        return rt, seen, naps
    yield make
    for s in started:
        s.shutdown()


def test_401_twice_then_200_with_admin_already_exists_returns_without_a_new_operator_file(box, tmp_path):
    # The issue's "Done when" test, with register answering what Runtipi said on the failing installs.
    rt, seen, _ = box([UNAUTH, UNAUTH, OK], [EXISTS])
    cred = tmp_path / "runtipi-operator.json"
    assert rt.ensure_operator(cred) is False
    assert not cred.exists()
    assert ("POST", "/api/auth/register") not in seen  # two 401s are not enough to call it "no operator"


def test_a_401_that_outlasts_the_retries_but_register_says_exists_waits_for_the_operator(box, tmp_path):
    rt, seen, naps = box([UNAUTH] * (Runtipi.AUTH_TRIES + 3) + [OK], [EXISTS])
    cred = tmp_path / "runtipi-operator.json"
    assert rt.ensure_operator(cred) is False
    assert not cred.exists()
    assert seen.count(("POST", "/api/auth/register")) == 1
    assert seen[-1] == ("GET", "/api/marketplace/all")
    assert sum(naps) <= Runtipi.AUTH_TRIES * Runtipi.STEP + Runtipi.EXISTS_WAIT


def test_user_already_exists_from_a_newer_runtipi_is_the_same_answer(box, tmp_path):
    exists = (400, {"statusCode": 400, "message": "AUTH_ERROR_USER_ALREADY_EXISTS"})
    rt, _, _ = box([UNAUTH] * Runtipi.AUTH_TRIES + [OK], [exists])
    assert rt.ensure_operator(tmp_path / "op.json") is False


def test_still_401_after_the_wait_fails_naming_both_answers(box, tmp_path):
    rt, _, naps = box([UNAUTH], [EXISTS])
    with pytest.raises(RuntipiError) as e:
        rt.ensure_operator(tmp_path / "op.json")
    msg = str(e.value)
    assert "-> 401" in msg and "AUTH_ERROR_ADMIN_ALREADY_EXISTS" in msg
    assert sum(naps) >= Runtipi.EXISTS_WAIT
    assert not (tmp_path / "op.json").exists()


def test_the_restart_window_502_and_404_is_waited_out(box, tmp_path):
    # What a recreated runtipi container answers on this repo's test machine: 502, then 404 until the route is back.
    gets = [(502, b"Bad Gateway"), (404, b"404 page not found\n"), (404, b"404 page not found\n"), OK]
    rt, seen, _ = box(gets)
    assert rt.ensure_operator(tmp_path / "op.json") is False
    assert ("POST", "/api/auth/register") not in seen


def test_nothing_listening_is_waited_for_then_given_up_on():
    rt = Runtipi("http://127.0.0.1:9/api", SECRET)  # discard port: refused
    naps = []
    rt.sleep = naps.append
    with pytest.raises(RuntipiError, match="did not answer within"):
        rt.ensure_operator(__import__("pathlib").Path("/nonexistent/op.json"))
    assert sum(naps) >= Runtipi.READY_WAIT


def test_a_fresh_runtipi_still_gets_its_operator(box, tmp_path):
    # No operator at all: the 401 persists, register succeeds, the token then works.
    rt, seen, _ = box([UNAUTH] * Runtipi.AUTH_TRIES + [OK], [(201, {"success": True})])
    cred = tmp_path / "runtipi-operator.json"
    assert rt.ensure_operator(cred) is True
    assert json.loads(cred.read_text())["username"] == "owner@wizard.local"
    assert oct(cred.stat().st_mode & 0o777) == "0o600"
    assert seen.count(("POST", "/api/auth/register")) == 1


def test_register_while_the_database_is_down_is_retried(box, tmp_path):
    # Seen live: with the database not ready, register answers 500.
    rt, seen, _ = box([UNAUTH] * Runtipi.AUTH_TRIES + [OK], [(500, {"message": "Internal server error"}), (201, {"success": True})])
    assert rt.ensure_operator(tmp_path / "op.json") is True
    assert seen.count(("POST", "/api/auth/register")) == 2


def test_a_real_refusal_is_not_waited_for(box, tmp_path):
    rt, _, naps = box([(400, {"message": "bad request"})])
    with pytest.raises(RuntipiError, match="-> 400"):
        rt.ensure_operator(tmp_path / "op.json")
    assert naps == []


def test_a_working_runtipi_is_asked_once(box, tmp_path):
    rt, seen, naps = box([OK])
    assert rt.ensure_operator(tmp_path / "op.json") is False
    assert seen == [("GET", "/api/marketplace/all")] and naps == []


def test_a_stalled_runtipi_is_bounded_by_the_clock_not_by_request_timeouts(tmp_path):
    # Codex P2: a Runtipi that accepts the connection but never answers must not stretch the 2-minute wait into
    # 40 request timeouts. Naps are counted as 0 s here, so only the clock can end the wait.
    import socket
    import time
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(64)  # accepts (backlog) but never answers
    try:
        rt = Runtipi(f"http://127.0.0.1:{s.getsockname()[1]}/api", SECRET)
        rt.sleep = lambda _s: None
        rt.STEP, rt.READY_WAIT, rt.PROBE_TIMEOUT = 0, 3, 1
        t = time.monotonic()
        with pytest.raises(RuntipiError, match="did not answer within"):
            rt.ensure_operator(tmp_path / "op.json")
        assert time.monotonic() - t < 6
    finally:
        s.close()
