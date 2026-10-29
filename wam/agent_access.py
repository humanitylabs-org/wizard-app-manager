"""Which apps each agent may use: the owner's switches in Connections → Agents, and `wam agents`.

An agent here is one configured instance: a name (Oscar), the harness it runs on (Hermes) and its model. Miguel's box
has one, the main agent; the Agent Service's agents registry (wizard-os spec/AGENTS.md) holds any others.

Where it lives and who enforces it (docs/AGENT_ACCESS.md). The Agent Service keeps each agent's app lists and its
gateway (`/wizard/mcp`, the only way an agent reaches app tools) enforces them on every call:

    main agent      <Agent Service data>/wizard-os/agents/.main-agent.json   {"schema": 1, "wizard_apps": [...], "agent_apps": [...]}
    other agents    <Agent Service data>/wizard-os/agents/<id>/agent.json    permissions.wizard_apps / agent_apps

An app turned off for an agent is gone from that agent's tool list, and a call to it is refused at once with
"Oscar is not allowed to use Money. The owner can turn it on in Connections." This module never writes those files: it
changes them through the Agent Service's owner API on the box's loopback (`PUT /wizard/v1/apps/<app>/agents`), so the
Service's own locking applies and it tells the agent's open sessions that the tool list changed.

Owner only. The Connections page reaches this through the owner listener (wam serve, the same gate as every other
Connections change: the owner's tailnet device, never the box itself or an app); the CLI is the owner on the server.
Agents and apps can't change their own access through either.

Defaults: everything on. The main agent without stored lists uses every installed app (private apps need a tick), and
a new standard agent starts with every installed app on (Agent Service, Miguel 29 Oct 2026).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

AGENT_ID = re.compile(r"[a-z][a-z0-9-]{0,30}")
APP_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAIN = "hermes"   # the Agent Service's id for the main agent (its gateway token is the gateway's own)
HARNESSES = {"hermes": "Hermes"}


class AccessError(RuntimeError):
    """The Agent Service refused or couldn't be reached; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def service_origin(home: Path | None) -> str | None:
    from .updates import service_origin as origin
    return origin(home)


def _req(origin: str, method: str, path: str, body: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{origin}{path}", data=data, method=method,
                               headers={"Accept": "application/json",
                                        **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}")
        except ValueError:
            why = {}
        detail = (why.get("detail") or why.get("error")) if isinstance(why, dict) else None
        detail = detail or f"HTTP {e.code}"
        raise AccessError(f"the Agent Service refused: {str(detail)[:200]}", 409 if e.code in (400, 409) else 502) from None
    except (OSError, urllib.error.URLError) as e:
        raise AccessError(f"can't reach the Agent Service ({str(e)[:120]})", 503) from None
    return json.loads(raw) if raw else {}


def _host_model(home: Path | None) -> tuple[str, str]:
    """The host Hermes's own model on an alongside box (its config.yaml `model.default` / `provider`)."""
    from . import alongside
    st = alongside.load(Path(home)) if home else None
    hh = ((st or {}).get("agent") or {}).get("hermes_home")
    if not hh:
        return "", ""
    try:
        import yaml
        cfg = yaml.safe_load((Path(hh) / "config.yaml").read_text()) or {}
    except Exception:  # noqa: BLE001 - no model shown, nothing else changes
        return "", ""
    m = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(m, str):
        return m[:80], ""
    if isinstance(m, dict):
        return str(m.get("default") or m.get("model") or "")[:80], str(m.get("provider") or "")[:40]
    return "", ""


def main_label(home: Path | None) -> str:
    """The owner's name for his main agent on an alongside box (`alongside.json` agent.label, e.g. "Oscar"); the
    Agent Service gets the same name as WIZARD_MAIN_AGENT_LABEL. '' when he hasn't named it."""
    from . import alongside
    st = alongside.load(Path(home)) if home else None
    label = ((st or {}).get("agent") or {}).get("label")
    return re.sub(r"[\x00-\x1f]", "", str(label)).strip()[:60] if isinstance(label, str) else ""


def _ids(v) -> list[str]:
    return [x for x in v if isinstance(x, str) and APP_ID.fullmatch(x)] if isinstance(v, list) else []


def instances(home: Path | None) -> dict:
    """The agents and what each may use, from the Agent Service:
    {"service": True, "agents": [{id, name, harness, harness_id, model, provider, main, private, on: [app ids],
    known: [app ids the agent could use]}]}. `known`: every app the Agent Service can give an agent (its
    agent-options lists); `on`: the ones this agent may use now. Raises AccessError when it can't be read."""
    origin = service_origin(home)
    if not origin:
        raise AccessError("this box has no Agent Service", 503)
    got = _req(origin, "GET", "/wizard/v1/agents")
    opts = _req(origin, "GET", "/wizard/v1/agent-options")
    known = sorted({a.get("id") for k in ("wizard_apps", "agent_apps") for a in (opts.get(k) or [])
                    if isinstance(a, dict) and isinstance(a.get("id"), str) and APP_ID.fullmatch(a["id"])})
    label = main_label(home)
    out = []
    for a in got.get("agents") or []:
        if not isinstance(a, dict) or not AGENT_ID.fullmatch(str(a.get("id") or "")) or a.get("role") == "recovery":
            continue
        perms = a.get("permissions") if isinstance(a.get("permissions"), dict) else {}
        model = a.get("model") if isinstance(a.get("model"), dict) else {}
        main = a["id"] == MAIN
        name, prov = str(model.get("model") or "")[:80], str(model.get("provider") or "")[:40]
        if main and prov in ("", "host"):
            name, prov = _host_model(home)
        harness = str(a.get("harness") or "hermes")
        out.append({
            "id": a["id"],
            "name": (label if main and label else str(a.get("label") or a["id"]))[:60],
            "harness": HARNESSES.get(harness, harness[:1].upper() + harness[1:30]), "harness_id": harness[:30],
            "model": name, "provider": prov, "main": main, "private": a.get("privacy") == "private",
            "on": sorted(set(_ids(perms.get("wizard_apps"))) | set(_ids(perms.get("agent_apps")))),
            "known": known,
        })
    out.sort(key=lambda x: (not x["main"], x["name"].lower()))
    return {"service": True, "agents": out}


def set_access(home: Path | None, agent: str, apps: list[str], on: bool) -> dict:
    """Turn `apps` on or off for one agent (the owner's switch, or Turn all on for a group). Each app goes through
    the Agent Service's `PUT /wizard/v1/apps/<app>/agents`, which sets exactly which agents may use it, so another
    agent's access never changes. Returns the fresh instances(). ValueError: a bad request (nothing changed)."""
    if not isinstance(agent, str) or not AGENT_ID.fullmatch(agent):
        raise ValueError("bad agent id")
    if not isinstance(apps, list) or not apps or len(apps) > 128 or not all(isinstance(x, str) and APP_ID.fullmatch(x) for x in apps):
        raise ValueError("send the app ids to change")
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    origin = service_origin(home)
    if not origin:
        raise AccessError("this box has no Agent Service", 503)
    agents = {a.get("id") for a in (_req(origin, "GET", "/wizard/v1/agents").get("agents") or []) if isinstance(a, dict)}
    if agent not in agents:
        raise ValueError(f"no agent called {agent}")
    for app in dict.fromkeys(apps):
        cur = _req(origin, "GET", f"/wizard/v1/apps/{app}/agents")
        ids = [a["id"] for a in cur.get("agents") or [] if isinstance(a, dict) and a.get("enabled") and isinstance(a.get("id"), str)]
        want = sorted(set(ids) | {agent}) if on else sorted(set(ids) - {agent})
        if sorted(ids) != want:
            _req(origin, "PUT", f"/wizard/v1/apps/{app}/agents", {"agents": want})
    return instances(home)


def resolve(home: Path | None, agent: str) -> str:
    """An agent by id or by name, as the owner types it (`wam agents access Oscar money off`)."""
    try:
        listed = instances(home)["agents"]
    except AccessError:
        return agent
    for a in listed:
        if agent == a["id"] or agent.lower() == a["name"].lower():
            return a["id"]
    return agent


def main(argv: list[str]) -> int:
    """wam agents [list] | wam agents access <agent> <app> on|off"""
    import argparse
    import sys
    from .cli import Config
    p = argparse.ArgumentParser(prog="wam agents", description="Agents and the apps each may use (Connections → Agents).")
    sub = p.add_subparsers(dest="action")
    sub.add_parser("list", help="each agent, its harness and model, and the apps it may use")
    acc = sub.add_parser("access", help="turn one app on or off for one agent (owner only)")
    acc.add_argument("agent", help="agent id or name (hermes = the main agent)")
    acc.add_argument("app", help="app id")
    acc.add_argument("state", choices=("on", "off"))
    a = p.parse_args(argv)
    home = Config().home
    try:
        if a.action == "access":
            agent = resolve(home, a.agent)
            got = set_access(home, agent, [a.app], a.state == "on")
            me = next((x for x in got["agents"] if x["id"] == agent), None)
            print(json.dumps({"agent": agent, "app": a.app, "on": bool(me and a.app in me["on"])}, indent=2))
            return 0
        for x in instances(home)["agents"]:
            off = [k for k in x["known"] if k not in x["on"]]
            model = f" ({x['model']})" if x["model"] else ""
            print(f"{x['name']} · {x['harness']}{model}  [{x['id']}]  can use {len(x['on'])} of {len(x['known'])}"
                  + (f"; off: {', '.join(off)}" if off else ""))
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except AccessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
