"""Graceful Agent Service restarts (wizard-app-manager#642, the App Manager half of wizard-agent-service#480).

Off by default: feature switch `wizard/graceful-restart` (For testers, Settings > Try new features). With it off
nothing here runs and every restart stops the Agent Service straight away, as before.

With it on, every restart of the `wizard-agent-service` app that the App Manager starts (`wam app restart`, an app
update or a WizardOS release moving it, a settings change that recreates it) goes through the Agent Service restart
gate (wizard-agent-service docs/graceful-restart.md, which needs `WIZARD_GRACEFUL_RESTART=1` on the service):

1. `POST /wizard/v1/restart` holds new sends (saved, launched after the restart) and lets running turns finish;
   `GET` is polled until `phase` is `ready`, then the restart runs. An Agent Service without the gate (404) or one
   that does not answer is restarted as before: there is nothing to wait for.
2. `overdue` (turns still running past the wait limit): the owner is told what still runs (Notifications app, and
   `wam agent-restart` / `GET /api/agent-restart`) and chooses `wait` (POST again with a new limit) or `now`.
   `cancel` calls the restart off. With no answer within ASK_SECONDS the restart is called off: nothing changes,
   so a turn is never cut without the owner's yes.
3. After the restart `/wizard/health` must answer. For an update that fails it, the backup the update just made is
   restored (the previous release), and health is checked again.
4. Called off (owner `cancel`, no answer, or the restart could not start): `DELETE /wizard/v1/restart` launches the
   held sends at once.

The owner's view and answer live in <wizard-os home>/agent-restart.json:
  {"phase", "reason", "wait_until", "turns": [...], "since", "choice": {"action": "wait"|"now"|"cancel", "minutes"}}
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SWITCH = "wizard/graceful-restart"
APP = "wizard-agent-service"
ROUTE = "/wizard/v1/restart"
STATE = "agent-restart.json"
WAIT_S = 600                 # the service's own default wait limit
POLL_S = 3                   # well inside the service's 2-minute hold lease
ASK_SECONDS = int(os.environ.get("WAM_GRACEFUL_RESTART_ASK_S") or 1800)
HEALTH_SECONDS = 180
ACTIONS = ("wait", "now", "cancel")


class CalledOff(RuntimeError):
    """The restart did not happen: the owner called it off, or nobody answered while turns still ran."""


class NotUpdated(SystemExit):
    """An update called off at the gate: nothing changed (a SystemExit like every other refused app_update)."""


def switch_on(home: Path | None = None) -> bool:
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home)


def applies(urn_or_id: str, home: Path | None = None, store: str = "wizardapps") -> bool:
    app, _, st = urn_or_id.partition(":")
    return app == APP and (st or store) == store and switch_on(home)


def _home(home: Path | None) -> Path:
    from .updates import _home as h
    return h(home)


def _origin(home: Path | None) -> str | None:
    from .updates import service_origin
    return service_origin(home)


# ---------------------------------------------------------------- HTTP (loopback, owner)

def http(method: str, url: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict]:
    """(status, json). 0 when the service does not answer at all."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - loopback origin from the registry
            return r.status, json.loads(r.read(262144) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(65536) or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, {}


# ---------------------------------------------------------------- the owner's view and answer

def read_state(home: Path | None = None) -> dict:
    try:
        doc = json.loads((_home(home) / STATE).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(home: Path | None, doc: dict) -> None:
    p = _home(home) / STATE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(STATE + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n")
        tmp.replace(p)
    except OSError:
        pass


def _clear_state(home: Path | None) -> None:
    try:
        (_home(home) / STATE).unlink()
    except OSError:
        pass


def choose(action: str, minutes: int | None = None, home: Path | None = None) -> dict:
    """The owner's answer to an overdue restart: wait longer, restart now, or call it off."""
    if action not in ACTIONS:
        raise ValueError("choose wait, now or cancel")
    doc = read_state(home)
    if not doc:
        raise ValueError("no Agent Service restart is waiting")
    choice = {"action": action, "at": time.time()}
    if action == "wait":
        try:
            m = int(minutes if minutes is not None else WAIT_S // 60)
        except (TypeError, ValueError):
            raise ValueError("minutes must be a whole number") from None
        choice["minutes"] = max(1, min(m, 240))
    doc["choice"] = choice
    _write_state(home, doc)
    return doc


def status(home: Path | None = None) -> dict:
    doc = read_state(home)
    return {"switch": SWITCH, "on": switch_on(home), "restart": doc or None}


def _describe(turns) -> str:
    rows = []
    for t in turns[:5] if isinstance(turns, list) else []:
        if isinstance(t, dict):
            mins = int(t.get("running_s") or 0) // 60
            rows.append(f"{t.get('agent_id') or 'agent'} ({mins} min)")
    return ", ".join(rows) or "a chat"


def _tell_owner(c, st: dict) -> None:
    try:
        from .app_updates import _notify
        _notify(c, "Agent restart waiting",
                f"The Agent Service wants to restart ({st.get('reason') or 'update'}) but {_describe(st.get('turns'))} "
                "is still running. Choose: wait longer, restart now, or cancel (`wam agent-restart wait|now|cancel`).")
    except Exception:  # noqa: BLE001 - telling the owner never fails the restart
        pass


def _tty_choice(st: dict) -> dict | None:
    if not (sys.stdin and sys.stdin.isatty()):
        return None
    try:
        got = input(f"The Agent Service is still running {_describe(st.get('turns'))}. "
                    "Wait 10 more minutes (w), restart now (n) or cancel (c)? [w/n/c] ").strip().lower()
    except (EOFError, OSError):
        return None
    return {"w": {"action": "wait", "minutes": 10}, "n": {"action": "now"}, "c": {"action": "cancel"}}.get(got[:1])


# ---------------------------------------------------------------- the gate

def gate(c, reason: str, *, home: Path | None = None, origin: str | None = None, req=http, sleep=time.sleep,
         clock=time.monotonic, ask=_tty_choice, tell=_tell_owner, wait_s: int = WAIT_S,
         ask_seconds: int | None = None) -> dict:
    """Hold the Agent Service and wait until nothing runs (or the owner says restart now).

    Returns {"state": "ready"|"now"|"unsupported"|"unreachable", "origin"}. Raises CalledOff (after releasing the
    hold) when the restart must not happen."""
    home = home if home is not None else getattr(c, "home", None)
    origin = origin or _origin(home)
    if not origin:
        return {"state": "unreachable", "origin": None}
    url = origin.rstrip("/") + ROUTE
    code, st = req("POST", url, {"reason": reason, "wait_s": wait_s})
    if code == 0:
        return {"state": "unreachable", "origin": origin}
    if code == 404:
        return {"state": "unsupported", "origin": origin}   # the service's own switch is off, or an older service
    if code != 200:
        return {"state": "unsupported", "origin": origin, "error": f"HTTP {code}"}
    limit = ASK_SECONDS if ask_seconds is None else ask_seconds
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    overdue_at = None
    _clear_state(home)
    try:
        while True:
            phase = st.get("phase")
            if phase == "ready" and not st.get("active_unknown"):
                return {"state": "ready", "origin": origin}
            if phase == "overdue":
                pick = None
                if overdue_at is None:   # tell the owner once per overdue spell; a terminal is asked right here
                    overdue_at = clock()
                    doc = {"phase": "overdue", "reason": reason, "since": since, "wait_until": st.get("wait_until"),
                           "turns": st.get("turns") or [], "active": st.get("active")}
                    _write_state(home, doc)
                    tell(c, doc)
                    pick = ask(st)
                pick = pick or read_state(home).get("choice")
                action = (pick or {}).get("action")
                if action == "now":
                    return {"state": "now", "origin": origin}
                if action == "cancel":
                    raise CalledOff("the owner called the Agent Service restart off")
                if action == "wait":
                    mins = int((pick or {}).get("minutes") or WAIT_S // 60)
                    code, st = req("POST", url, {"reason": reason, "wait_s": mins * 60})
                    overdue_at = None
                    _clear_state(home)
                    if code == 0:
                        return {"state": "unreachable", "origin": origin}
                    continue
                if clock() - overdue_at >= limit:
                    raise CalledOff("chats were still running and nobody chose to restart; nothing changed")
            sleep(POLL_S)
            code, st = req("GET", url)   # also renews the hold's lease
            if code == 0:
                return {"state": "unreachable", "origin": origin}   # it stopped by itself: nothing left to drain
            if code == 404:
                return {"state": "unsupported", "origin": origin}   # restarted without the gate meanwhile
    except BaseException:
        release(origin, req=req)
        raise
    finally:
        _clear_state(home)


def release(origin: str | None, req=http) -> None:
    """Call the restart off: held sends launch at once. Best effort (the hold's lease ends it anyway)."""
    if origin:
        req("DELETE", origin.rstrip("/") + ROUTE)


def healthy(origin: str | None, *, req=http, sleep=time.sleep, clock=time.monotonic,
            seconds: float = HEALTH_SECONDS) -> bool:
    """Whether /wizard/health answers 200 within `seconds` after the restart."""
    if not origin:
        return False
    deadline = clock() + seconds
    while True:
        code, _ = req("GET", origin.rstrip("/") + "/wizard/health")
        if code == 200:
            return True
        if clock() >= deadline:
            return False
        sleep(5)


def restart(c, urn: str, do_restart, *, reason: str = "restart", home: Path | None = None, **kw) -> dict:
    """Run `do_restart()` (the plain restart, returns the app_lifecycle result) through the gate, then health check.
    A plain restart keeps its image, so there is nothing to roll back to: a failed health check is reported."""
    home = home if home is not None else getattr(c, "home", None)
    try:
        g = gate(c, reason, home=home, **{k: v for k, v in kw.items() if k in ("origin", "req", "sleep", "clock", "ask",
                                                                                 "tell", "ask_seconds")})
    except CalledOff as e:
        return {"id": urn.split(":", 1)[0], "urn": urn, "action": "restart", "status": "running",
                "error": f"not restarted: {e}", "graceful": "called_off"}
    try:
        res = do_restart()
    except BaseException:
        release(g.get("origin"), req=kw.get("req", http))
        raise
    res = {**res, "graceful": g["state"]}
    if not res.get("error") and g.get("origin") and not healthy(g["origin"], **{k: v for k, v in kw.items()
                                                                                 if k in ("req", "sleep", "clock")}):
        res["error"] = "the Agent Service did not pass its health check after the restart"
    return res


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def update(c, urn: str, do_update, *, backup: bool = True, home: Path | None = None, rt=None, rollback=None,
           **kw) -> dict:
    """Run `do_update()` (cli._app_update) through the gate, then health check; a failed check restores the backup the
    update just made (the previous release/image) and checks health again."""
    home = home if home is not None else getattr(c, "home", None)
    rt = rt or c.runtipi()
    before = ((rt.app(urn) or {}).get("app") or {})
    was_running = before.get("status") == "running"
    pick = {k: v for k, v in kw.items() if k in ("origin", "req", "sleep", "clock", "ask", "tell", "ask_seconds")}
    try:
        g = gate(c, "update", home=home, **pick) if was_running else {"state": "not_running", "origin": None}
    except CalledOff as e:
        raise NotUpdated(f"{urn.split(':', 1)[0]} not updated: {e}") from None
    started_ms = int(time.time() * 1000)
    try:
        res = do_update()
    except BaseException:
        release(g.get("origin"), req=kw.get("req", http))
        raise
    res = {**res, "graceful": g["state"]}
    if not was_running:
        return res
    hk = {k: v for k, v in kw.items() if k in ("req", "sleep", "clock")}
    origin = kw.get("origin") or _origin(home) or g.get("origin")
    problem = res.get("error") or (None if healthy(origin, **hk) else
                                   "the Agent Service did not pass its health check after the update")
    if not problem:
        return res
    moved = _int(res.get("version")) is not None and _int(res.get("version")) != _int(before.get("version"))
    if not (backup and moved and _int(before.get("version")) is not None):
        res["error"] = problem
        return res
    print(f"{urn}: {problem}; rolling back to version {before.get('version')}", file=sys.stderr, flush=True)
    if rollback is None:
        from .app_updates import rollback
    try:
        rb = rollback(c, rt, urn, started_ms, {"status": "running", "from": _int(before.get("version"))})
    except Exception as e:  # noqa: BLE001
        rb = {"rolled_back": False, "error": f"{type(e).__name__}: {e}"[:300]}
    res["rollback"] = rb
    if rb.get("rolled_back"):
        rb["healthy"] = healthy(_origin(home) or origin, **hk)
        res["error"] = (f"{problem}; rolled back to version {before.get('version')}"
                        + ("" if rb["healthy"] else ", which does not pass its health check either"))
    else:
        res["error"] = f"{problem}; the rollback did not work ({rb.get('error')})"
    return res
