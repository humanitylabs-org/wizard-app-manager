"""`wam serve`: make the owner's connection decisions take effect within seconds, and tell the owner about new ones.

The App Manager (packages/backend wizard-app-grants.ts) owns the connection policy and its state (wizard-grants.json):
declared connections are approved at install and when an update adds them, apps may ask for one at runtime, the owner's
Disconnect is sticky, and connections to a sensitive app wait for the owner. The owner decides in the Connections view
(the App Manager's owner-authenticated API) or with `wam grants`. This watcher polls that state; when the connections
change it reconciles the app networks live (no restarts) and refreshes the registry, and it delivers the App Manager's
notices ("Carlos Campaigns now connects to Resend") through the owner's Notifications app, the box's notification path.
Without a Notifications app the notices stay in the Connections view only.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

POLL = 3.0
NOTICE_MAX_AGE = 24 * 3600   # older undelivered notices (e.g. Notifications installed later) are marked, not sent


def signature(state: dict | None) -> str:
    """What the networks depend on: grants, the owner's Disconnects, pending requests, sensitive apps (not notices)."""
    if state is None:
        return ""
    keep = {k: sorted(json.dumps({x: g.get(x) for x in ("kind", "from", "to", "approved")}, sort_keys=True)
                      for g in state.get(k) or [] if isinstance(g, dict)) for k in ("grants", "denied", "pending")}
    keep["sensitive"] = sorted(state.get("sensitive") or [])
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()


def notifications_mcp(home: Path) -> str | None:
    """The installed Notifications app's MCP URL (the provider of the `notifications` slot in apps.json)."""
    try:
        reg = json.loads((home / "apps.json").read_text())
    except (OSError, ValueError):
        return None
    for a in reg.get("apps") or []:
        if not isinstance(a, dict) or "notifications" not in (a.get("provides") or []):
            continue
        url = ((a.get("mcp") or {}) if isinstance(a.get("mcp"), dict) else {}).get("url")
        if isinstance(url, str) and url.startswith("http://127.0.0.1:"):
            return url
    return None


def _mcp_call(url: str, tool: str, args: dict, timeout: int = 20) -> dict:
    """Minimal streamable-HTTP MCP client: initialize, then one tools/call."""
    sid = None

    def rpc(method, params=None, i=None):
        nonlocal sid
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            h["Mcp-Session-Id"] = sid
        body = {"jsonrpc": "2.0", "method": method, **({"id": i} if i is not None else {}),
                **({"params": params} if params is not None else {})}
        with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id") or sid
            text = r.read().decode()
        if i is None:
            return None
        if text.startswith(("event:", "data:")):
            text = [ln[5:] for ln in text.splitlines() if ln.startswith("data:")][-1]
        return json.loads(text)
    rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "wam", "version": "1"}}, 1)
    rpc("notifications/initialized")
    res = rpc("tools/call", {"name": tool, "arguments": args}, 2) or {}
    if res.get("error") or (res.get("result") or {}).get("isError"):
        raise RuntimeError(str(res.get("error") or res.get("result"))[:200])
    return res.get("result") or {}


def notice_message(n: dict) -> dict:
    pending = n.get("kind") == "pending"
    return {"title": "Connection needs your OK" if pending else "App connection added",
            "message": str(n.get("text") or "")[:500],
            "priority": "high" if pending else "low",
            "tags": "lock" if pending else "link"}


def deliver(c, rt, state: dict | None, send=None, now=None) -> list[str]:
    """Send undelivered notices to Notifications, then mark them delivered in the App Manager. Returns sent ids."""
    if not state:
        return []
    now = now or time.time()
    todo = [n for n in state.get("notices") or [] if isinstance(n, dict) and not n.get("delivered") and isinstance(n.get("id"), str)]
    if not todo:
        return []
    url = notifications_mcp(c.home)
    if send is None and url is None:
        return []   # no Notifications app: the notices stay in the Connections view
    send = send or (lambda n: _mcp_call(str(url), "notify", notice_message(n)))
    done, sent = [], []
    for n in todo:
        if now - float(n.get("at") or 0) > NOTICE_MAX_AGE:
            done.append(n["id"])
            continue
        try:
            send(n)
        except Exception as e:  # noqa: BLE001 - try again next round
            print(f"connections: notice not delivered ({str(e)[:160]})", file=sys.stderr, flush=True)
            break
        done.append(n["id"])
        sent.append(n["id"])
    if done:
        try:
            rt.req("POST", "/apps/wizard/connection-notices", {"delivered": done})
        except Exception as e:  # noqa: BLE001
            print(f"connections: notices not marked delivered ({str(e)[:160]})", file=sys.stderr, flush=True)
    return sent


def step(c, rt, last: str, apply_fn, registry_fn) -> str:
    """One poll: reconcile when the connections changed, then deliver notices. Returns the new signature."""
    from . import network
    state = network.connection_state(c, rt)
    sig = signature(state)
    if state is not None and sig != last:
        if last:   # the first poll only learns the state; the start-up reconcile already applied it
            apply_fn()
            try:
                registry_fn()
            except Exception as e:  # noqa: BLE001
                print(f"connections: registry not refreshed ({str(e)[:160]})", file=sys.stderr, flush=True)
    deliver(c, rt, state)
    return sig if state is not None else last


def start(c, apply_fn, registry_fn, stop: threading.Event | None = None) -> None:
    def loop():
        last = ""
        while not (stop and stop.is_set()):
            try:
                last = step(c, c.runtipi(), last, apply_fn, registry_fn)
            except Exception as e:  # noqa: BLE001 - the App Manager restarting: try again
                print(f"connections: {str(e)[:200]}", file=sys.stderr, flush=True)
            time.sleep(POLL)
    threading.Thread(target=loop, name="wam-connections-watch", daemon=True).start()
    print("app connections: owner decisions apply within seconds; notices go to Notifications", flush=True)
