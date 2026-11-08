"""Store "Sign in to GitHub": GitHub's device flow, run by the App Manager so a friend can install private beta apps.

Served by the Store listener (`store_install.py`, loopback :7791) under `/wizard-os-home/store-install/_github`, so
every request already passed its gate: Tailscale Serve as the box owner, and for a POST also another tailnet device,
the shell's own origin, JSON and `X-Wizard-Store: 1`. The private-gateway listener (:7792) never serves these paths.

  GET  _github          {state, login, source, user_code?, verification_uri?, expires_in?, interval?}
  POST _github/start    {}  begin the flow (409 when the feature switch is off or this Wizard has no client id)
  POST _github/signout  {}  delete the saved login and end any flow in progress

`state` is signed_out | pending | signed_in | expired | denied | error | not_configured | switched_off. `source` is `file` (the
sign-in this module saved, removable) or `env` (a token from the environment or the `gh` CLI: signed in, but Sign out
cannot remove it). `login` is the GitHub username and only while signed in.

The device flow runs in one background thread started by `start`; the browser only polls the status. The device
code never leaves this process. On approval the access token is written, atomically, to the file `private.token()`
already reads (`~/.wizard-os/github-token`, 0600, folder 0700) and only the username goes to a sidecar
(`~/.wizard-os/github-login`). The token appears in no HTTP response, log line, error text or receipt, and is only ever
used by the App Manager and the catalog adapter on this box, for read calls.

Client id (a public value, no secret): env `WAM_GITHUB_CLIENT_ID`, then `app-manager.json` `github_client_id`. Without
one the state is `not_configured`. GitHub OAuth Apps have no read-only scope for private repositories, so the flow asks for
`repo` (to read private app repos) and `read:packages` (to pull private images): see docs/github-signin.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import private, registry

STATES = ("signed_out", "pending", "signed_in", "expired", "denied", "error", "not_configured", "switched_off")
SWITCH = "archimedes/github-signin"   # feature switch (Settings > Try new features / Tracker): off by default
SCOPE = "repo read:packages"
GITHUB_URL = "https://github.com"          # device flow endpoints (tests point these at a local server)
API_URL = "https://api.github.com"         # GET /user for the username
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
USER_CODE = re.compile(r"[A-Z0-9-]{4,16}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
TOKEN = re.compile(r"[A-Za-z0-9_.-]{10,255}")
ERROR_CODE = re.compile(r"[a-z_]{1,40}")
MAX_BODY = 64 * 1024
TIMEOUT = 15
MAX_POLL_FAILURES = 5       # consecutive network failures while polling before the flow ends as `error`
SLOW_DOWN = 5               # GitHub: add five seconds to the interval on slow_down
LOGIN_TTL = 600             # how long an env/`gh` token's username is remembered (in memory, by digest)
GH_TTL = 30                 # how long the `gh auth token` answer is remembered

_lock = threading.Lock()         # guards _flow and the token/sidecar files
_start_lock = threading.Lock()   # one device-code request at a time
_flow: "Flow | None" = None
_gh_cache: tuple[float, str] | None = None
_login_cache: dict[str, tuple[float, str]] = {}
_clock = time.monotonic
TIME_SCALE = 1.0            # tests shrink the waits between polls; production never changes it


class Flow(dict):
    """The flow in progress. Holds the device code, so its repr says nothing but the state."""

    def __repr__(self) -> str:
        return f"<github sign-in {self.get('state')}>"


class _Net(Exception):
    """A GitHub call failed in transit. Carries no detail: nothing about a request may reach a log or a response."""


def log(msg: str) -> None:
    sys.stderr.write(f"wam github sign-in: {msg}\n")


# ---------------------------------------------------------------- config and files

def switch_on() -> bool:
    """Whether the owner turned on the `archimedes/github-signin` feature switch. Off: no new sign-in can start
    (status `switched_off`); an existing login still reads as signed in and Sign out still works."""
    from . import feature_switches
    return feature_switches.is_on(SWITCH)


def client_id() -> str:
    cid = (os.environ.get("WAM_GITHUB_CLIENT_ID") or "").strip()
    if not cid:
        try:
            cfg = json.loads((registry.home() / "app-manager.json").read_text())
            cid = str(cfg.get("github_client_id") or "").strip() if isinstance(cfg, dict) else ""
        except (OSError, ValueError):
            cid = ""
    return cid if CLIENT_ID.fullmatch(cid) else ""


def token_path() -> Path:
    return private.token_file()


def login_path() -> Path:
    return token_path().with_name("github-login")


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: a temp file made 0600 from its first byte, synced, then renamed over the target."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _gh_token() -> str:
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _gh_cached() -> str:
    global _gh_cache
    now = _clock()
    if _gh_cache is None or now - _gh_cache[0] > GH_TTL:
        _gh_cache = (now, _gh_token())
    return _gh_cache[1]


def saved() -> str:
    try:
        return token_path().read_text().strip()
    except OSError:
        return ""


def current() -> tuple[str, str]:
    """(source, token) in the order `private.token()` uses: environment, the saved file, then the `gh` CLI."""
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return "env", os.environ[k]
    tok = saved()
    if tok:
        return "file", tok
    tok = _gh_cached()
    return ("env", tok) if tok else ("", "")


# ---------------------------------------------------------------- GitHub calls

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _call(req: urllib.request.Request) -> tuple[int, dict]:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            raw, code = r.read(MAX_BODY + 1), r.status
    except urllib.error.HTTPError as e:
        try:
            raw, code = e.read(MAX_BODY + 1), e.code
        except OSError:
            raise _Net() from None
    except (OSError, ValueError):
        raise _Net() from None
    if len(raw) > MAX_BODY:
        raise _Net()
    try:
        data = json.loads(raw)
    except ValueError:
        raise _Net() from None
    return code, data if isinstance(data, dict) else {}


def _post(url: str, form: dict) -> tuple[int, dict]:
    return _call(urllib.request.Request(
        url, data=urllib.parse.urlencode(form).encode(),
        headers={"Accept": "application/json", "User-Agent": "wizard-app-manager",
                 "Content-Type": "application/x-www-form-urlencoded"}, method="POST"))


def _user_login(tok: str) -> str | None:
    """The GitHub username for a token. '' when GitHub answered but gave none; None when it rejected the token."""
    try:
        code, data = _call(urllib.request.Request(f"{API_URL}/user", headers={
            "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "User-Agent": "wizard-app-manager"}))
    except _Net:
        return ""
    if code == 401:
        return None
    login = data.get("login") if code == 200 else ""
    return login if isinstance(login, str) and LOGIN.fullmatch(login) else ""


def _login_for(tok: str) -> str:
    """Username of a token this module did not write (environment, `gh`, or a hand-placed file), remembered by digest."""
    key, now = hashlib.sha256(tok.encode()).hexdigest(), _clock()
    hit = _login_cache.get(key)
    if hit and now - hit[0] < LOGIN_TTL:
        return hit[1]
    login = _user_login(tok) or ""
    _login_cache.clear()
    _login_cache[key] = (now, login)
    return login


def _sidecar_login() -> str:
    try:
        v = login_path().read_text(errors="replace").strip()
    except OSError:
        return ""
    return v if LOGIN.fullmatch(v) else ""


# ---------------------------------------------------------------- status

def status() -> dict:
    """What the Store may show. Never contains a token or the device code."""
    with _lock:   # the same lock the final save holds: a half-finished save is never seen
        source, tok = current()
        sidecar = _sidecar_login() if source == "file" else ""
        flow = _flow
        if not tok and flow:
            if flow["state"] == "pending" and _clock() >= flow["expires_at"]:
                flow["state"] = "expired"
                flow["stop"].set()
            out = {"state": flow["state"], "login": "", "source": ""}
            if flow["state"] == "pending":
                out.update(user_code=flow["user_code"], verification_uri=flow["verification_uri"],
                           expires_in=max(0, int(flow["expires_at"] - _clock())), interval=flow["interval"])
            return out
    if tok:
        return {"state": "signed_in", "login": sidecar or _login_for(tok), "source": source}
    if not switch_on():
        return {"state": "switched_off", "login": "", "source": ""}
    return {"state": "signed_out" if client_id() else "not_configured", "login": "", "source": ""}


# ---------------------------------------------------------------- the flow

def start() -> tuple[int, dict]:
    """Begin the device flow. Idempotent while one is pending. 409 when this Wizard has no GitHub client id."""
    global _flow
    with _start_lock:
        st = status()
        if st["state"] in ("signed_in", "pending"):
            return 200, st
        if not switch_on():
            return 409, {"state": "switched_off", "login": "", "source": ""}
        cid = client_id()
        if not cid:
            return 409, {"state": "not_configured", "login": "", "source": ""}
        try:
            code, data = _post(f"{GITHUB_URL}/login/device/code", {"client_id": cid, "scope": SCOPE})
            parsed = _parse_code(code, data)
        except _Net:
            log("could not reach GitHub to start sign-in")
            parsed = None
        if not parsed:
            return 200, _set_failed("error")
        flow = Flow(**parsed, state="pending", stop=threading.Event(), client_id=cid)
        with _lock:
            if _flow:
                _flow["stop"].set()
            _flow = flow
        threading.Thread(target=_run, args=(flow,), name="github-signin", daemon=True).start()
        return 200, status()


def _parse_code(code: int, data: dict) -> dict | None:
    err = data.get("error")
    if code != 200 or err:
        log(f"GitHub refused to start sign-in ({err if isinstance(err, str) and ERROR_CODE.fullmatch(err) else code})")
        return None
    dc, uc, uri = data.get("device_code"), data.get("user_code"), data.get("verification_uri")
    ei, iv = data.get("expires_in"), data.get("interval", 5)
    if (not isinstance(dc, str) or not 8 <= len(dc) <= 256 or not isinstance(uc, str) or not USER_CODE.fullmatch(uc)
            or not isinstance(uri, str) or not uri.startswith(GITHUB_URL + "/")
            or not isinstance(ei, int) or isinstance(ei, bool) or not isinstance(iv, int) or isinstance(iv, bool)):
        log("GitHub's answer to start sign-in was not understood")
        return None
    return {"device_code": dc, "user_code": uc, "verification_uri": uri,
            "expires_at": _clock() + max(1, min(ei, 3600)), "interval": max(1, min(iv, 60))}


def _set_failed(state: str) -> dict:
    global _flow
    with _lock:
        if _flow:
            _flow["stop"].set()
        _flow = Flow(state=state, stop=threading.Event())
    return {"state": state, "login": "", "source": ""}


def _pause(flow: dict, seconds: float) -> bool:
    """Wait; True when the flow was ended meanwhile (sign out or a newer flow)."""
    return flow["stop"].wait(seconds * TIME_SCALE)


def _end(flow: dict, state: str) -> None:
    with _lock:
        if _flow is flow and not flow["stop"].is_set():
            flow["state"] = state
            flow["stop"].set()


def _run(flow: dict) -> None:
    try:
        _poll(flow)
    except Exception as e:  # noqa: BLE001 - never leak detail: the type name only
        log(f"sign-in stopped ({type(e).__name__})")
        _end(flow, "error")


def _poll(flow: dict) -> None:
    failures = 0
    while True:
        if _pause(flow, flow["interval"]):
            return
        if _clock() >= flow["expires_at"]:
            return _end(flow, "expired")
        try:
            _, data = _post(f"{GITHUB_URL}/login/oauth/access_token",
                            {"client_id": flow["client_id"], "device_code": flow["device_code"], "grant_type": GRANT})
        except _Net:
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                log("lost contact with GitHub; sign-in stopped")
                return _end(flow, "error")
            continue
        failures = 0
        tok, err = data.get("access_token"), data.get("error")
        if isinstance(tok, str):
            return _finish(flow, tok)
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            nxt = data.get("interval")
            flow["interval"] = (min(nxt, 60) if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt > flow["interval"]
                                else flow["interval"] + SLOW_DOWN)
            continue
        if err == "expired_token":
            return _end(flow, "expired")
        if err == "access_denied":
            return _end(flow, "denied")
        log(f"GitHub ended sign-in ({err if isinstance(err, str) and ERROR_CODE.fullmatch(err) else 'unexpected answer'})")
        return _end(flow, "error")


def _finish(flow: dict, tok: str) -> None:
    global _flow
    if not TOKEN.fullmatch(tok):
        log("GitHub's access token was not in the expected form; sign-in stopped")
        return _end(flow, "error")
    login = _user_login(tok)
    if login is None:
        log("GitHub did not accept the new access token; sign-in stopped")
        return _end(flow, "error")
    with _lock:
        if _flow is not flow or flow["stop"].is_set():   # signed out (or restarted) while GitHub was answering
            return
        try:
            # The username first: the token's arrival is what makes the box "signed in", and it must find it.
            _remove(login_path())
            if login:
                _write_private(login_path(), login + "\n")
            _write_private(token_path(), tok + "\n")
        except OSError as e:
            log(f"could not save the GitHub login ({type(e).__name__})")
            flow["state"] = "error"
            flow["stop"].set()
            return
        flow["stop"].set()
        _flow = None


def signout() -> dict:
    """Delete the saved login and username, end any flow in progress. An environment token stays (source `env`)."""
    global _flow, _gh_cache
    with _lock:
        if _flow:
            _flow["stop"].set()
        _flow = None
        _gh_cache = None
        _login_cache.clear()
        for p in (token_path(), login_path()):
            _remove(p)
    return status()
