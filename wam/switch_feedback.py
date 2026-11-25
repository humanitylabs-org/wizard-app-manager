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
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
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


def box_id(home=None) -> str:
    """This box's random id for de-duplication (made once, kept in the Wizard home; no identity in it)."""
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


def payload(home=None, app_rows=None) -> dict:
    if app_rows is None:
        from . import app_switches
        app_rows, _ = app_switches.listing(home)
    return {"contract": CONTRACT, "box": box_id(home), "switches": rows(app_rows)}


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
        doc = payload(home, app_rows)
        url = relay_url() + PATH
        if dry_run:
            return {"switch": SWITCH, "on": True, "sent": False, "dry_run": True, "url": url, "would_send": doc}
        s, now = _state(home), time.time() if now is None else now
        digest = _digest(doc)
        if not force and s.get("sent") == digest and now - float(s.get("sent_at") or 0) < RESEND_S:
            return {"switch": SWITCH, "on": True, "sent": False, "unchanged": True, "switches": len(doc["switches"])}
        try:
            code = post(url, doc)
        except urllib.error.HTTPError as e:
            code = e.code
        except (OSError, ValueError) as e:
            s.update(error=str(e)[:200], tried_at=now)
            _save(home, s)
            return {"switch": SWITCH, "on": True, "sent": False, "error": str(e)[:200]}
        if 200 <= code < 300:
            s.update(sent=digest, sent_at=now, error=None, tried_at=now)
            _save(home, s)
            return {"switch": SWITCH, "on": True, "sent": True, "switches": len(doc["switches"])}
        s.update(error=f"relay answered HTTP {code}", tried_at=now)
        _save(home, s)
        return {"switch": SWITCH, "on": True, "sent": False, "error": f"relay answered HTTP {code}"}
    except Exception as e:                  # noqa: BLE001 - counts never break the update tick
        return {"switch": SWITCH, "sent": False, "error": str(e)[:200]}
