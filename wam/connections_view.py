"""Connections on the shell's origin: the App Manager's Connections view with its owner controls (Open / Declared,
Disconnect / Reconnect, Approve / Deny, Sensitive), behind the owner listener (store_install.py).

The view (`renderConnections`, wizard-views/) needs the owner's connection API. Runtipi has it, behind its own
sign-in, on the App Manager's box-only port; the loopback page on `wam serve` (:7790/views/) has no owner path and
is read-only. Here the same view is served at `/wizard-os-home/store-install/_connections/` on the shell's tailnet
origin, so the owner opens it from Wizard on his phone, and its API calls go to this listener:

  GET  api/connections                               the live report (as :7790/api/connections)
  GET  api/apps/wizard/connections                   the owner's decisions, + whether this device may change them
  POST api/apps/<id>:wizardapps/app-grants           {to, approve}: Reconnect/Approve (true), Disconnect/Deny (false)
  POST api/apps/<id>:wizardapps/sensitive            {sensitive}
  POST api/apps/wizard/connection-mode               {mode: open|declared}

Every request passes the listener's owner gate first (tailscaled peer, the owner's Tailscale login); a POST also
passes `check_request` (another tailnet device, the shell's origin, JSON with X-Wizard-Store: 1). A change is then made
through Runtipi's owner API, exactly as `wam grants` does, and `wam serve` applies it to Docker within seconds.
Nothing else of Runtipi's API is reachable here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PREFIX = "_connections"
VIEWS = Path(__file__).resolve().parent.parent / "wizard-views"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
FILES = {"index.html", "standalone.js", "wizard-apps.js", "runtipi-open.js", "wux.css", "wizard-apps.css", "standalone.css"}
APP = r"([a-z0-9][a-z0-9-]{1,40})"
TO = re.compile(r"(?:slot:)?[a-z0-9][a-z0-9-]{1,40}")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; "
       "base-uri 'none'; form-action 'none'")


def page() -> bytes:
    """index.html showing only Connections, wired to this listener (standalone.js reads the data- attributes)."""
    b = (VIEWS / "index.html").read_bytes()
    return b.replace(b'<body class="wux">', b'<body class="wux" data-only="connections" data-api="api" '
                     b'data-connections="api/connections" data-owner-header="X-Wizard-Store">', 1)


def asset(name: str) -> tuple[bytes, str] | None:
    if name not in FILES:
        return None
    f = VIEWS / name
    if not f.is_file():
        return None
    return (page() if name == "index.html" else f.read_bytes()), TYPES[f.suffix]


def _body(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("send a JSON object") from None
    if not isinstance(body, dict):
        raise ValueError("send a JSON object")
    return body


def change(rt, sub: str, raw: bytes) -> dict:
    """One owner decision, validated here and made through Runtipi's owner API. ValueError: refused (400)."""
    body = _body(raw)
    if sub == "api/apps/wizard/connection-mode":
        if set(body) != {"mode"} or body["mode"] not in ("open", "declared"):
            raise ValueError('send {"mode": "open"|"declared"}')
        return rt.req("POST", "/apps/wizard/connection-mode", {"mode": body["mode"]})
    m = re.fullmatch(rf"api/apps/{APP}:wizardapps/(app-grants|sensitive)", sub)
    if not m:
        raise LookupError(sub)
    app, what = m[1], m[2]
    if what == "sensitive":
        if set(body) != {"sensitive"} or not isinstance(body["sensitive"], bool):
            raise ValueError('send {"sensitive": true|false}')
        return rt.req("POST", f"/apps/{app}:wizardapps/sensitive", {"sensitive": body["sensitive"]})
    if set(body) != {"to", "approve"} or not isinstance(body["approve"], bool) \
            or not isinstance(body["to"], str) or not TO.fullmatch(body["to"]) or body["to"] == app:
        raise ValueError('send {"to": "<app id or slot:name>", "approve": true|false}')
    return rt.req("POST", f"/apps/{app}:wizardapps/app-grants", {"to": body["to"], "approve": body["approve"]})


def state(rt, writes_why: str | None) -> dict:
    """The owner's decisions, and whether this device may change them (a change from the box itself is refused)."""
    st = rt.req("GET", "/apps/wizard/connections")
    if not isinstance(st, dict):
        raise ValueError("no connection state")
    return {**st, "owner_writes": writes_why is None, **({"owner_writes_why": writes_why} if writes_why else {})}
