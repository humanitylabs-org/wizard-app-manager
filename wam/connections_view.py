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
  GET  api/connections/restore                       what Restore defaults would change, in plain words
  POST api/connections/restore                       {confirm: true, plan}: Restore defaults (connections_reset.py), live
  GET  api/agents                                    the agents and the apps each may use (agent_access.py)
  POST api/agents/<agent>/access                     {apps: [ids], on: bool}: the owner's switch / Turn all on

Every request passes the listener's owner gate first (tailscaled peer, the owner's Tailscale login); a POST also
passes `check_request` (another tailnet device, the shell's origin, JSON with X-Wizard-Store: 1). A change is then made
through Runtipi's owner API, exactly as `wam grants` does, and `wam serve` applies it to Docker within seconds; an
agent's access goes through the Agent Service's owner API on the box's loopback, which its gateway enforces on the
agent's next call. Nothing else of Runtipi's or the Agent Service's API is reachable here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PREFIX = "_connections"
VIEWS = Path(__file__).resolve().parent.parent / "wizard-views"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
FILES = {"index.html", "standalone.js", "wizard-apps.js", "connections-map.js", "runtipi-open.js", "wux.css", "wizard-apps.css",
         "standalone.css"}
# Shared Wizard pieces the map reuses from the shell on the same origin (wizard-ux docs/COMPONENTS.md): the kit
# tokens and look, its one pop-up (WzSheet) and the app glyphs (WizSigil). Linked before the page's own styles, so
# the App Manager's rules win a tie; on a shell without them the view falls back to letters and a plain dialog.
SHARED = (b'<link rel="stylesheet" href="/wizard-os-home/kit/wizard-tokens.css" data-wz-shared="tokens">\n  '
          b'<link rel="stylesheet" href="/wizard-os-home/kit/wizard-ui.css" data-wz-shared="kit">\n  '
          b'<link rel="stylesheet" href="/wizard-os-home/core/sigils.css">\n  '
          b'<script defer src="/wizard-os-home/kit/wizard-sheet.js"></script>\n  '
          b'<script defer src="/wizard-os-home/core/sigils.js"></script>\n  ')
APP = r"([a-z0-9][a-z0-9-]{1,40})"
TO = re.compile(r"(?:slot:)?[a-z0-9][a-z0-9-]{1,40}")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; "
       "base-uri 'none'; form-action 'none'")


def page() -> bytes:
    """index.html showing only Connections, wired to this listener (standalone.js reads the data- attributes)."""
    b = (VIEWS / "index.html").read_bytes().replace(b'<link rel="stylesheet" href="wux.css">', SHARED + b'<link rel="stylesheet" href="wux.css">', 1)
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


AGENT_ACCESS = re.compile(r"api/agents/([a-z][a-z0-9-]{0,30})/access")


def agents(home) -> dict:
    """GET api/agents: the agents and what each may use; {"service": False, "why"} when the Agent Service can't say."""
    from . import agent_access
    try:
        return agent_access.instances(home)
    except agent_access.AccessError as e:
        return {"service": False, "why": str(e)[:200], "agents": []}


def agent_change(home, sub: str, raw: bytes) -> dict:
    """POST api/agents/<agent>/access {apps, on}. ValueError: refused (400); AccessError: the Agent Service's answer."""
    from . import agent_access
    m = AGENT_ACCESS.fullmatch(sub)
    if not m:
        raise LookupError(sub)
    body = _body(raw)
    if set(body) != {"apps", "on"} or not isinstance(body["on"], bool) or not isinstance(body["apps"], list):
        raise ValueError('send {"apps": ["<app id>", ...], "on": true|false}')
    return agent_access.set_access(home, m[1], body["apps"], body["on"])


def restore_plan(c, rt) -> dict:
    """GET api/connections/restore: what Restore defaults would change ({changes: [{label, change, text}], plan: id})."""
    from . import connections_reset, network
    changes = connections_reset.plan(network.connection_state(c, rt), network.report(c, rt))
    return {"changes": changes, "plan": connections_reset.plan_id(changes)}


def restore(c, rt, raw: bytes, reconcile=None) -> dict:
    """POST api/connections/restore {confirm: true, plan: <id from GET>}: Restore defaults now (owner only, after the
    confirm dialog). A plan that no longer matches changes nothing."""
    from . import connections_reset
    body = _body(raw)
    if set(body) != {"confirm", "plan"} or body["confirm"] is not True or not isinstance(body["plan"], str):
        raise ValueError('send {"confirm": true, "plan": "<plan from GET api/connections/restore>"}')
    return connections_reset.apply(c, rt, reconcile, expect=body["plan"])
