"""Store "Sign in to GitHub": the device flow against a mocked GitHub, the owner gate, and the token's custody.

Every token here is fake (`gho_TESTONLY...`); no request leaves the machine: GitHub is a local http.server.
"""
import http.client
import io
import json
import logging
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wam import cli, github_signin as gs, private, store_install as si

TOKEN = "gho_TESTONLY0123456789abcdefABCDEF"
DEVICE = "dc_TESTONLY_device_code_0123456789"
USER_CODE = "WDJB-MJHT"


class FakeGitHub:
    """Scripted answers: `polls` is the sequence of replies to /login/oauth/access_token (the last one repeats)."""

    def __init__(self):
        self.polls = [{"error": "authorization_pending"}]
        self.device_reply = None
        self.user_reply = (200, {"login": "octo-friend"})
        self.calls = []
        self.bodies = []
        self.drop_polls = False
        self.auth = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                outer.calls.append(self.path)
                outer.bodies.append(body)
                if self.path == "/login/device/code":
                    return self._reply(*(outer.device_reply or (200, {
                        "device_code": DEVICE, "user_code": USER_CODE, "expires_in": 900, "interval": 5,
                        "verification_uri": f"{outer.url}/login/device"})))
                if self.path == "/login/oauth/access_token":
                    if outer.drop_polls:
                        self.connection.close()
                        return
                    reply = outer.polls.pop(0) if len(outer.polls) > 1 else outer.polls[0]
                    return self._reply(200, reply)
                self._reply(404, {})

            def do_GET(self):  # noqa: N802
                outer.calls.append(self.path)
                outer.auth = self.headers.get("Authorization")
                if self.path == "/user":
                    return self._reply(*outer.user_reply)
                self._reply(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @property
    def n_polls(self):
        return self.calls.count("/login/oauth/access_token")


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fake HOME (the token path), the mocked GitHub, a configured client id, and fast polling."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home / ".wizard-os"))
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WAM_GITHUB_CLIENT_ID", "Iv1.testonlyclient")
    gh = FakeGitHub()
    monkeypatch.setattr(gs, "GITHUB_URL", gh.url)
    monkeypatch.setattr(gs, "API_URL", gh.url)
    monkeypatch.setattr(gs, "TIME_SCALE", 0.002)
    monkeypatch.setattr(gs, "_gh_token", lambda: "")
    monkeypatch.setattr(gs, "switch_on", lambda: True)   # the feature switch: tests below the switch tests turn it on
    monkeypatch.setattr(gs, "_flow", None)
    monkeypatch.setattr(gs, "_gh_cache", None)
    gs._login_cache.clear()
    yield gh
    gs.signout()
    gh.srv.shutdown()


def _wait_state(want, timeout=5):
    end = time.monotonic() + timeout
    st = gs.status()
    while st["state"] != want and time.monotonic() < end:
        time.sleep(0.01)
        st = gs.status()
    assert st["state"] == want, st
    return st


def _no_secret(*things):
    for t in things:
        s = t if isinstance(t, str) else json.dumps(t, default=repr)
        assert TOKEN not in s and DEVICE not in s, s


# ---------------------------------------------------------------- the flow

def test_happy_path_saves_the_token_privately_and_only_the_login_is_reported(box, capsys):
    box.polls = [{"error": "authorization_pending"}, {"access_token": TOKEN, "token_type": "bearer", "scope": "repo"}]
    code, st = gs.start()
    assert code == 200 and st["state"] == "pending"
    assert st["user_code"] == USER_CODE and st["verification_uri"] == f"{box.url}/login/device"
    assert 0 < st["expires_in"] <= 900 and st["interval"] == 5 and "device_code" not in st
    assert "client_id=Iv1.testonlyclient" in box.bodies[0] and "scope=repo+read%3Apackages" in box.bodies[0]
    done = _wait_state("signed_in")
    assert done == {"state": "signed_in", "login": "octo-friend", "source": "file"}
    assert private.token() == TOKEN                                  # the exact file wam/private.py reads
    assert box.auth == f"Bearer {TOKEN}"                             # GET /user used it
    assert (gs.login_path()).read_text().strip() == "octo-friend" and TOKEN not in gs.login_path().read_text()
    _no_secret(st, done, capsys.readouterr().err)


def test_token_file_is_0600_in_a_0700_folder_and_written_atomically(box, monkeypatch):
    box.polls = [{"access_token": TOKEN}]
    folder = gs.token_path().parent
    folder.mkdir(mode=0o755)                                         # a pre-existing, too-open folder is tightened
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real(a, b))[1])
    gs.start()
    _wait_state("signed_in")
    assert stat.S_IMODE(gs.token_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(gs.login_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert (str(gs.token_path())) in [dst for _, dst in replaced]    # temp file renamed into place
    assert all(".tmp-" in src for src, _ in replaced)
    assert sorted(p.name for p in folder.iterdir()) == ["github-login", "github-token"]   # no temp files left


def test_authorization_pending_then_slow_down_then_success(box):
    box.polls = [{"error": "authorization_pending"}, {"error": "slow_down", "interval": 10}, {"error": "authorization_pending"},
                 {"access_token": TOKEN}]
    gs.start()
    flow = gs._flow
    _wait_state("signed_in")
    assert box.n_polls == 4 and flow["interval"] == 10               # GitHub's new interval was honoured


def test_slow_down_without_an_interval_adds_five_seconds(box):
    box.polls = [{"error": "slow_down"}, {"error": "authorization_pending"}]
    gs.start()
    flow = gs._flow
    end = time.monotonic() + 5
    while flow["interval"] == 5 and time.monotonic() < end:
        time.sleep(0.01)
    assert flow["interval"] == 10


def test_expired_token_ends_the_flow_as_expired(box):
    box.polls = [{"error": "authorization_pending"}, {"error": "expired_token"}]
    gs.start()
    st = _wait_state("expired")
    assert st == {"state": "expired", "login": "", "source": ""} and not gs.token_path().exists()


def test_flow_expires_by_the_clock_even_if_github_stays_pending(box, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gs, "_clock", lambda: now[0])
    gs.start()
    assert gs.status()["state"] == "pending"
    now[0] += 901
    assert gs.status()["state"] == "expired"


def test_access_denied(box):
    box.polls = [{"error": "access_denied"}]
    gs.start()
    assert _wait_state("denied") == {"state": "denied", "login": "", "source": ""}
    assert not gs.token_path().exists()


def test_an_unexpected_github_error_ends_as_error(box, capsys):
    box.polls = [{"error": "incorrect_client_credentials", "error_description": f"secret {TOKEN}"}]
    gs.start()
    _wait_state("error")
    err = capsys.readouterr().err
    assert "incorrect_client_credentials" in err and TOKEN not in err and "secret" not in err


def test_network_error_while_polling_ends_as_error_after_retries(box, capsys):
    box.drop_polls = True
    gs.start()
    _wait_state("error")
    assert box.n_polls == gs.MAX_POLL_FAILURES
    _no_secret(capsys.readouterr().err)


def test_network_error_starting_the_flow(box, monkeypatch):
    monkeypatch.setattr(gs, "GITHUB_URL", "http://127.0.0.1:1")      # nothing listens
    code, st = gs.start()
    assert code == 200 and st == {"state": "error", "login": "", "source": ""}


def test_github_refusing_the_device_request_is_an_error(box):
    box.device_reply = (404, {"error": "Not Found"})
    assert gs.start()[1]["state"] == "error"
    box.device_reply = (200, {"device_code": DEVICE, "user_code": "bad code!", "expires_in": 900, "verification_uri": "x"})
    assert gs.start()[1]["state"] == "error"
    box.device_reply = (200, {"device_code": DEVICE, "user_code": USER_CODE, "expires_in": 900,
                              "verification_uri": "https://evil.example/login/device"})   # only GitHub's own page
    assert gs.start()[1]["state"] == "error"


def test_a_rejected_access_token_is_not_saved(box):
    box.polls = [{"access_token": TOKEN}]
    box.user_reply = (401, {"message": "Bad credentials"})
    gs.start()
    _wait_state("error")
    assert not gs.token_path().exists()


def test_a_malformed_access_token_is_not_saved(box):
    box.polls = [{"access_token": "short"}]
    gs.start()
    _wait_state("error")
    assert not gs.token_path().exists()


def test_github_not_answering_user_still_saves_the_token_without_a_login(box):
    box.polls = [{"access_token": TOKEN}]
    box.user_reply = (500, {})
    gs.start()
    assert _wait_state("signed_in")["login"] == "" and private.token() == TOKEN and not gs.login_path().exists()


def test_a_github_username_that_is_not_a_username_is_dropped(box):
    box.polls = [{"access_token": TOKEN}]
    box.user_reply = (200, {"login": "<script>alert(1)</script>"})
    gs.start()
    assert _wait_state("signed_in")["login"] == ""


# ---------------------------------------------------------------- start / status / signout rules

def test_not_configured_is_409_and_makes_no_request(box, monkeypatch):
    monkeypatch.delenv("WAM_GITHUB_CLIENT_ID")
    assert gs.status() == {"state": "not_configured", "login": "", "source": ""}
    code, st = gs.start()
    assert code == 409 and st["state"] == "not_configured" and box.calls == []


def test_client_id_comes_from_app_manager_json_when_the_env_is_empty(box, monkeypatch, tmp_path):
    monkeypatch.delenv("WAM_GITHUB_CLIENT_ID")
    (tmp_path / "home/.wizard-os").mkdir(parents=True)
    (tmp_path / "home/.wizard-os/app-manager.json").write_text(json.dumps({"github_client_id": "Iv1.fromconfig"}))
    assert gs.client_id() == "Iv1.fromconfig" and gs.status()["state"] == "signed_out"
    monkeypatch.setenv("WAM_GITHUB_CLIENT_ID", "Iv1.fromenv")        # env wins
    assert gs.client_id() == "Iv1.fromenv"
    monkeypatch.setenv("WAM_GITHUB_CLIENT_ID", "not a client id; rm -rf")   # malformed counts as unset, falls to nothing
    assert gs.client_id() == ""


def test_start_is_idempotent_while_pending(box):
    a = gs.start()[1]
    b = gs.start()[1]
    assert a["state"] == b["state"] == "pending" and a["user_code"] == b["user_code"]
    assert box.calls.count("/login/device/code") == 1


def test_start_after_a_finished_flow_begins_a_new_one(box):
    box.polls = [{"error": "access_denied"}]
    gs.start()
    _wait_state("denied")
    box.polls = [{"error": "authorization_pending"}]
    assert gs.start()[1]["state"] == "pending" and box.calls.count("/login/device/code") == 2


def test_start_when_signed_in_does_not_begin_a_flow(box):
    box.polls = [{"access_token": TOKEN}]
    gs.start()
    _wait_state("signed_in")
    code, st = gs.start()
    assert code == 200 and st["state"] == "signed_in" and box.calls.count("/login/device/code") == 1


def test_signout_removes_file_and_sidecar_and_ends_a_pending_flow(box):
    gs.start()
    flow = gs._flow
    polls_before = box.n_polls
    assert gs.signout() == {"state": "signed_out", "login": "", "source": ""}
    assert flow["stop"].is_set()
    time.sleep(0.1)
    assert box.n_polls <= polls_before + 1                            # the loop stopped
    box.polls = [{"access_token": TOKEN}]
    gs.start()
    _wait_state("signed_in")
    assert gs.token_path().exists() and gs.login_path().exists()
    assert gs.signout()["state"] == "signed_out"
    assert not gs.token_path().exists() and not gs.login_path().exists()
    assert gs.signout()["state"] == "signed_out"                      # nothing to remove is fine


def test_signout_during_the_final_exchange_does_not_save_a_token(box, monkeypatch):
    box.polls = [{"access_token": TOKEN}]
    real = gs._user_login
    def slow(tok):
        gs.signout()                                                  # the owner signs out while GitHub answers /user
        return real(tok)
    monkeypatch.setattr(gs, "_user_login", slow)
    gs.start()
    time.sleep(0.3)
    assert not gs.token_path().exists() and gs.status()["state"] == "signed_out"


def test_environment_token_is_signed_in_with_source_env_and_signout_cannot_remove_it(box, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    st = gs.status()
    assert st == {"state": "signed_in", "login": "octo-friend", "source": "env"}
    assert gs.signout()["source"] == "env"                            # still signed in
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert gs.status()["source"] == "env"
    _no_secret(st)


def test_a_gh_cli_login_counts_as_env(box, monkeypatch):
    monkeypatch.setattr(gs, "_gh_token", lambda: TOKEN)
    assert gs.status() == {"state": "signed_in", "login": "octo-friend", "source": "env"}


def test_a_hand_placed_token_file_is_signed_in_and_signout_removes_it(box):
    gs.token_path().parent.mkdir(parents=True)
    gs.token_path().write_text(TOKEN + "\n")
    assert gs.status() == {"state": "signed_in", "login": "octo-friend", "source": "file"}
    assert gs.signout()["state"] == "signed_out" and not gs.token_path().exists()


def test_a_rejected_stored_token_still_reads_as_signed_in_with_no_login(box):
    box.user_reply = (401, {})
    gs.token_path().parent.mkdir(parents=True)
    gs.token_path().write_text(TOKEN + "\n")
    assert gs.status() == {"state": "signed_in", "login": "", "source": "file"}


def test_wam_private_and_the_backup_rules_agree_on_the_file(box):
    from wam.backup import box as backup_box
    assert private.token_file() == gs.token_path()
    assert {"github-token", "github-login"} <= backup_box.PER_MACHINE   # neither is ever captured or replaced


# ---------------------------------------------------------------- HTTP: routes and the owner gate

OWNER = "owner@x.com"
ORIGIN = "https://box.tail1.ts.net:8796"
GOOD = {"Tailscale-User-Login": OWNER, "Origin": ORIGIN, "X-Wizard-Store": "1", "Content-Type": "application/json",
        "X-Forwarded-For": "100.64.0.9"}


@pytest.fixture
def listener(box, monkeypatch, tmp_path):
    monkeypatch.setattr(si, "peer_uid", lambda p: 0)
    monkeypatch.setattr(si, "owner_login", lambda: OWNER)
    monkeypatch.setattr(si, "served_origins", lambda h: {ORIGIN})
    cfg = type("C", (), {"home": tmp_path / "wizhome", "index": "x"})()
    cfg.home.mkdir()
    (cfg.home / "links.json").write_text(json.dumps({"host": "box.tail1.ts.net"}))
    monkeypatch.setattr(cli.StoreHandler, "cfg", cfg, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.StoreHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def call(method, path, headers=None, body=None):
        raw = json.dumps({} if body is None else body) if method == "POST" else None
        h = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        h.request(method, path, raw, {**(GOOD if headers is None else headers),
                                      **({"Content-Length": str(len(raw))} if raw is not None else {})})
        r = h.getresponse()
        text = r.read().decode()
        return r.status, (json.loads(text) if text else {}), text
    yield call
    srv.shutdown()


def test_routes_status_start_signout_over_http(listener, box):
    box.polls = [{"access_token": TOKEN}]
    code, st, raw = listener("GET", "/_github")
    assert code == 200 and st == {"state": "signed_out", "login": "", "source": ""}
    code, st, raw1 = listener("POST", "/_github/start")
    assert code == 200 and st["state"] == "pending" and st["user_code"] == USER_CODE
    _wait_state("signed_in")
    code, st, raw2 = listener("GET", "/_github")
    assert code == 200 and st == {"state": "signed_in", "login": "octo-friend", "source": "file"}
    code, st, raw3 = listener("POST", "/_github/signout")
    assert code == 200 and st["state"] == "signed_out" and not gs.token_path().exists()
    _no_secret(raw, raw1, raw2, raw3)
    assert DEVICE not in raw1


def test_start_not_configured_is_409_over_http(listener, monkeypatch):
    monkeypatch.delenv("WAM_GITHUB_CLIENT_ID")
    code, st, _ = listener("POST", "/_github/start")
    assert code == 409 and st["state"] == "not_configured"


def test_unknown_paths_methods_and_bodies(listener):
    assert listener("GET", "/_github/start")[0] == 405
    assert listener("POST", "/_github")[0] == 405
    for bad in ("/_github/other", "/_github/start/x", "/_github/../money"):
        assert listener("POST", bad)[0] == 404, bad
    assert listener("POST", "/_github/start", body={"client_id": "evil"})[0] == 400   # nothing is taken from the browser
    assert listener("POST", "/_github/signout", body=[1])[0] == 400


def test_the_owner_gate_refuses_forged_cross_origin_and_box_posts(listener, box):
    box.polls = [{"access_token": TOKEN}]
    gs.token_path().parent.mkdir(parents=True)
    gs.token_path().write_text(TOKEN + "\n")
    cases = {
        "from the box itself": ({**GOOD, "X-Forwarded-For": "100.64.0.1"}, "this Wizard itself"),
        "cross-origin": ({**GOOD, "Origin": "https://evil.example"}, "wrong origin"),
        "no store header": ({k: v for k, v in GOOD.items() if k != "X-Wizard-Store"}, "bad request"),
        "form post": ({**GOOD, "Content-Type": "application/x-www-form-urlencoded"}, "bad request"),
        "not the owner": ({**GOOD, "Tailscale-User-Login": "guest@x.com"}, "owner"),
        "no login": ({k: v for k, v in GOOD.items() if k != "Tailscale-User-Login"}, "owner"),
    }
    for name, (hdrs, why) in cases.items():
        for path in ("/_github/start", "/_github/signout"):
            code, body, raw = listener("POST", path, headers=hdrs)
            assert code == 403 and why in body["error"], (name, path, body)
            _no_secret(raw)
    assert box.calls == [] and gs.token_path().exists() and gs._flow is None   # nothing ran, nothing was deleted
    assert listener("GET", "/_github", headers={**GOOD, "Tailscale-User-Login": "guest@x.com"})[0] == 403


def test_the_gateway_listener_does_not_serve_these_routes(box, monkeypatch, tmp_path):
    assert not si.gateway_path_ok("/_github") and not si.gateway_path_ok("/_github/start")
    assert not si.gateway_path_ok("/_github/signout")


# ---------------------------------------------------------------- the token is never anywhere it should not be

def test_the_token_never_reaches_a_response_log_or_exception(box, listener, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []
    box.polls = [{"error": "authorization_pending"}, {"error": "slow_down"}, {"access_token": TOKEN}]
    seen.append(listener("POST", "/_github/start")[2])
    _wait_state("signed_in")
    seen.append(listener("GET", "/_github")[2])
    # a failing save: the OSError text must not be echoed
    monkeypatch.setattr(gs, "_write_private", lambda *a: (_ for _ in ()).throw(OSError(f"disk full writing {TOKEN}")))
    gs.signout()
    box.polls = [{"access_token": TOKEN}]
    seen.append(listener("POST", "/_github/start")[2])
    _wait_state("error")
    seen.append(listener("GET", "/_github")[2])
    # a handler crash answers with the exception type only
    real_status = gs.status
    monkeypatch.setattr(gs, "status", lambda: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN} {DEVICE}")))
    code, body, raw = listener("GET", "/_github")
    monkeypatch.setattr(gs, "status", real_status)
    assert code == 500 and "RuntimeError" in body["error"]
    seen.append(raw)
    out = capsys.readouterr()
    _no_secret(*seen, out.out, out.err, caplog.text)
    assert "boom" not in raw


def test_secret_never_in_the_state_repr(box):
    box.polls = [{"access_token": TOKEN}]
    gs.start()
    _wait_state("signed_in")
    _no_secret(repr(gs.status()), repr(gs._flow), repr(gs._login_cache.keys()))


def test_github_redirects_are_not_followed(box):
    class R(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.end_headers()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        gs.API_URL = f"http://127.0.0.1:{srv.server_address[1]}"
        assert gs._user_login(TOKEN) == ""                            # a redirect is a failure, never followed
    finally:
        srv.shutdown()
        gs.API_URL = box.url


def test_oversized_github_answers_are_refused(box, monkeypatch):
    monkeypatch.setattr(gs, "MAX_BODY", 20)
    box.device_reply = (200, {"device_code": DEVICE, "user_code": USER_CODE, "expires_in": 900,
                              "verification_uri": f"{box.url}/login/device"})
    assert gs.start()[1]["state"] == "error"


# ---------------------------------------------------------------- feature switch archimedes/github-signin

def test_switch_off_by_default_start_is_409_and_makes_no_request(box, monkeypatch):
    monkeypatch.setattr(gs, "switch_on", lambda: False)
    assert gs.status() == {"state": "switched_off", "login": "", "source": ""}
    assert gs.start() == (409, {"state": "switched_off", "login": "", "source": ""})
    assert box.calls == [] and gs._flow is None


def test_switch_off_still_reports_an_existing_login_and_signout_works(box, monkeypatch):
    monkeypatch.setattr(gs, "switch_on", lambda: False)
    gs.token_path().parent.mkdir(parents=True, exist_ok=True)
    gs.token_path().write_text(TOKEN + "\n")
    assert gs.status() == {"state": "signed_in", "login": "octo-friend", "source": "file"}
    assert gs.signout()["state"] == "switched_off" and not gs.token_path().exists()


def test_switch_uses_the_box_feature_switch_and_is_off_when_unreadable(monkeypatch):
    from wam import feature_switches
    seen = []
    monkeypatch.setattr(feature_switches, "is_on", lambda tid, *a, **k: seen.append(tid) or False)
    assert gs.switch_on() is False and seen == ["archimedes/github-signin"]
    monkeypatch.undo()
    monkeypatch.setenv("WIZARD_OS_HOME", "/nonexistent-wizard-home")
    assert gs.switch_on() is False


def test_switch_off_start_is_409_over_http(listener, box, monkeypatch):
    monkeypatch.setattr(gs, "switch_on", lambda: False)
    code, body, _ = listener("POST", "/_github/start")
    assert code == 409 and body["state"] == "switched_off" and box.calls == []
