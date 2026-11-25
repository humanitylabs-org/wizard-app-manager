"""Anonymous Try new features counts (wizard-app-manager#701, wizard-os docs/APP_SWITCHES.md "Try new features").

Apps that keep feedback list `tried` (turned on at least once on this box) and `vote` ("up", "down" or null) for each
switch in their wizard.app-switches/1 listing; the owner sets them in the app's Settings, in the shared Try new
features section. Behind the box switch `wizard/try-feature-counts` (off by default), the hourly app-update tick sends
those two facts for every app switch that was tried or voted on, so the Tracker can show `Tried by N · 👍 N · 👎 N`:

    POST {relay}/v1/switch-feedback
    {"contract": "wizard.switch-feedback/1", "box": "<32 hex, random, made once on this box>",
     "switches": [{"id": "<app id>:<switch id>", "issues": ["owner/repo#N", ...], "tried": true, "vote": "up"}]}

Nothing else leaves the box: no names, descriptions, owner, hostname, Wizard version or app content. `box` is a random
id kept in `<Wizard home>/switch-feedback.json`, only so the relay can replace this box's previous row instead of
counting it twice (each send is this box's whole current state). {relay} is the Feedback app's relay
(`FEEDBACK_RELAY_URL`, else the same default the Feedback app uses). A send happens only when the state changed since
the last accepted one, or once a day. With the switch off nothing is read or sent. `wam switch-feedback --dry-run`
prints exactly what would be sent.

Two sources, because the App Manager only calls an app as the owner when it holds the app's owner-proof key
(wizard-os#133; most boxes have owner proof off): the catalog it reads server-side, and what the owner's own browser
read from the other apps. The shell's `wizard/try-feature-counts` package (wizard-ux) reads each app's switch listing
the way the Tracker's Try it does (same origin, the owner's Serve login) and posts it to the owner-only route
`/wizard-os-home/store-install/_switch-feedback` (`record`), which keeps the rows of installed apps that declare
switches and sends right away. A server-side read wins over a browser one for the same app.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SWITCH = "wizard/try-feature-counts"
CONTRACT = "wizard.switch-feedback/1"
PATH = "/v1/switch-feedback"
DEFAULT_RELAY = "https://wizardos-feedback-relay.ai-wizards-previews.workers.dev"  # wizardos-feedback DEFAULT_RELAY_URL
STATE = "switch-feedback.json"
BOX = re.compile(r"[0-9a-f]{32}")
RESEND_S = 24 * 3600
TIMEOUT = 15
MAX_ROWS = 200
_lock = threading.Lock()


def _home(home) -> Path:
    from .updates import _home as h
    return h(home)


def switch_on(home=None) -> bool:
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home, timeout=4)


def relay_url() -> str:
    url = (os.environ.get("FEEDBACK_RELAY_URL") or DEFAULT_RELAY).strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?|http://(127\.0\.0\.1|localhost)(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?", url):
        raise ValueError("FEEDBACK_RELAY_URL must be an https:// address (or http://127.0.0.1 for a local test)")
    return url


def _state(home) -> dict:
    try:
        s = json.loads((_home(home) / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def _save(home, s: dict) -> None:
    f = _home(home) / STATE
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(s, fh, indent=2)
    os.replace(tmp, f)


def _update(home, **changes) -> None:
    with _lock:
        s = _state(home)
        s.update(changes)
        _save(home, s)


def box_id(home=None) -> str:
    """This box's random id for de-duplication (made once, kept in the Wizard home; no identity in it)."""
    with _lock:
        s = _state(home)
        if not BOX.fullmatch(str(s.get("box") or "")):
            s["box"] = secrets.token_hex(16)
            _save(home, s)
        return s["box"]


def rows(switches: list[dict]) -> list[dict]:
    """Only {id, issues, tried, vote} of app switches that were tried or voted on, from the box's switch catalog."""
    out = []
    for r in switches or []:
        if not isinstance(r, dict) or r.get("kind") != "app":
            continue
        tried, vote = r.get("tried") is True, r.get("vote") if r.get("vote") in ("up", "down") else None
        if not tried and vote is None:
            continue
        out.append({"id": str(r["id"]), "issues": [str(i) for i in r.get("issues") or []][:12],
                    "tried": tried, "vote": vote})
    return sorted(out, key=lambda x: x["id"])[:MAX_ROWS]


def _browser_rows(home, declared_ids, served) -> list[dict]:
    """Rows the owner's browser reported (record), for installed apps the server-side read didn't answer."""
    seen = _state(home).get("browser")
    out = []
    for app, entry in (seen.items() if isinstance(seen, dict) else []):
        if app in served or (declared_ids is not None and app not in declared_ids) or not isinstance(entry, dict):
            continue
        out += [dict(r, kind="app") for r in entry.get("rows") or [] if isinstance(r, dict)]
    return out


def payload(home=None, app_rows=None, declared_ids=None, info=None) -> dict:
    """What leaves the box. `info` (a dict) is filled with where each app's rows came from, for `wam switch-feedback`."""
    from . import app_switches
    if app_rows is None:
        app_rows, status = app_switches.listing(home)
        served = {s["id"] for s in status if s.get("ok")}
        declared_ids = {a["id"] for a in app_switches.declared(_home(home))}
    else:
        served = {r.get("app") for r in app_rows if isinstance(r, dict)}
    browser = _browser_rows(home, declared_ids, served)
    if info is not None:
        from_browser = sorted({r["id"].split(":", 1)[0] for r in browser})
        info.update(read_here=sorted(a for a in served if a), from_browser=from_browser,
                    waiting_for_browser=sorted((declared_ids or set()) - served - set(from_browser)))
        if info["waiting_for_browser"]:
            info["note"] = ("apps without an owner-proof key are read by the owner's browser: the shell's "
                            "wizard/try-feature-counts package posts them to store-install/_switch-feedback")
    return {"contract": CONTRACT, "box": box_id(home), "switches": rows(list(app_rows) + browser)}


def record(home, body, *, declared_ids=None, now=None) -> dict:
    """The owner's browser reports the switch listings it read: {"apps": [{"id": "<app>", "switches": [<listing rows>]}]}.
    Kept only while the switch is on, only for installed apps that declare switches, only {id, issues, tried, vote}."""
    from . import app_switches as aps
    if not isinstance(body, dict) or not isinstance(body.get("apps"), list) or len(body["apps"]) > 100:
        raise ValueError('send {"apps": [{"id": "<app id>", "switches": [...]}]}')
    if declared_ids is None:
        declared_ids = {a["id"] for a in aps.declared(_home(home))}
    now = time.time() if now is None else now
    got = {}
    for a in body["apps"]:
        if not isinstance(a, dict) or not aps.APP_ID.fullmatch(str(a.get("id") or "")) or a["id"] not in declared_ids \
                or not isinstance(a.get("switches"), list):
            continue
        rs = []
        for s in a["switches"][:MAX_ROWS]:
            if not isinstance(s, dict) or not aps.SWITCH_ID.fullmatch(str(s.get("id") or "")):
                continue
            rs.append({"id": f"{a['id']}:{s['id']}", "app": a["id"],
                       "issues": [str(i) for i in (s.get("issues") or []) if isinstance(i, str) and aps.ISSUE.fullmatch(i)][:12],
                       "tried": s.get("tried") is True, "vote": s.get("vote") if s.get("vote") in ("up", "down") else None})
        got[a["id"]] = {"at": now, "rows": rs}
    with _lock:
        st = _state(home)
        prev = st.get("browser") if isinstance(st.get("browser"), dict) else {}
        st["browser"] = {k: v for k, v in {**prev, **got}.items() if k in declared_ids}
        _save(home, st)
    return {"recorded": sorted(got)}


def _digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _post(url: str, doc: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(doc).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "wizard-app-manager"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:          # noqa: S310 - https relay (or local test)
        return r.status


def tick(home=None, *, force=False, dry_run=False, on=None, now=None, post=_post, app_rows=None) -> dict:
    """Send this box's counts when the switch is on and they changed (or a day passed). Never raises."""
    try:
        if not (switch_on(home) if on is None else on):
            return {"switch": SWITCH, "on": False, "sent": False}
        info = {}
        doc = payload(home, app_rows, info=info)
        url = relay_url() + PATH
        if dry_run:
            return {"switch": SWITCH, "on": True, "sent": False, "dry_run": True, "url": url, "would_send": doc, **info}
        s, now = _state(home), time.time() if now is None else now
        digest = _digest(doc)
        if not force and s.get("sent") == digest and now - float(s.get("sent_at") or 0) < RESEND_S:
            return {"switch": SWITCH, "on": True, "sent": False, "unchanged": True, "switches": len(doc["switches"])}
        try:
            code = post(url, doc)
        except urllib.error.HTTPError as e:
            code = e.code
        except (OSError, ValueError) as e:
            _update(home, error=str(e)[:200], tried_at=now)
            return {"switch": SWITCH, "on": True, "sent": False, "error": str(e)[:200]}
        if 200 <= code < 300:
            _update(home, sent=digest, sent_at=now, error=None, tried_at=now)
            return {"switch": SWITCH, "on": True, "sent": True, "switches": len(doc["switches"])}
        _update(home, error=f"relay answered HTTP {code}", tried_at=now)
        return {"switch": SWITCH, "on": True, "sent": False, "error": f"relay answered HTTP {code}"}
    except Exception as e:                  # noqa: BLE001 - counts never break the update tick
        return {"switch": SWITCH, "sent": False, "error": str(e)[:200]}
