"""Which group each item on the Connections map sits in: Agents, Apps, Connectors or System.

The group comes from the fields a wizard.md already has, by one rule (docs/APP_NETWORKS.md, "Groups on the map"):

1. `kind:` in wizard.md (agent | app | connector | system), when an app wants to say it itself. Optional.
2. `type: agent` (or `category: agent`): an agent.
3. The active shell, Agent Service and agent (the gateway core, network.plan): system. `category: interface` too.
4. `type: external` (it connects the box to an outside service: a bank, Cloudflare, an AI provider): a connector.
5. `type: view`: an app (a screen is the whole point).
6. No `view:` at all: a connector (a background service only apps and the agent call).
7. `category: system` with a view (its screen only sets it up, e.g. a music source's sign-in): a connector.
8. Anything else: an app.

No app id is listed anywhere: a new app lands in the right group from its own wizard.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

KINDS = ("agent", "app", "connector", "system")
AGENT_ID = re.compile(r"[a-z][a-z0-9-]{0,30}")


def fields(w: dict | None) -> dict:
    """The wizard.md fields the rule reads, kept with each app the planner reads (network.read_app)."""
    w = w if isinstance(w, dict) else {}
    k = str(w.get("kind") or "").strip().lower()
    return {"type": w.get("type"), "category": w.get("category"), "view": bool(w.get("view")),
            "system": w.get("system") is True, **({"kind": k} if k in KINDS else {})}


def kind_of(meta: dict | None, core: bool = False) -> str:
    """The group of one app (see the module docstring). Without wizard.md fields: system for the core, else app."""
    m = meta if isinstance(meta, dict) else {}
    if m.get("kind") in KINDS:
        return m["kind"]
    if m.get("type") == "agent" or m.get("category") == "agent":
        return "agent"
    if core or m.get("category") == "interface":
        return "system"
    if not m:
        return "app"
    if m.get("type") == "external":
        return "connector"
    if m.get("type") == "view":
        return "app"
    if not m.get("view"):
        return "connector"
    if m.get("category") == "system":
        return "connector"
    return "app"


def agents(home: Path | str | None) -> list[dict]:
    """The agents this box knows about that are not apps: the host's own agent on an alongside box
    (alongside.json), and the Agent Service's agents registry (`<home>/agents/<id>/agent.json`, spec/AGENTS.md).
    Each: {id, name, main, private, limited}. `limited`: its agent.json lists the apps it may use (the gateway
    enforces that list); otherwise it can use every app and connector."""
    if not home:
        return []
    home = Path(home)
    out: list[dict] = []
    try:
        from . import alongside
        ext = (alongside.external_slots(home) or {}).get("agent")
    except Exception:  # noqa: BLE001 - no alongside state: no host agent
        ext = None
    if isinstance(ext, dict) and ext.get("id"):
        out.append({"id": str(ext["id"]), "name": str(ext.get("name") or ext["id"]), "main": True, "private": False,
                    "limited": False})
    root = home / "agents"
    for f in sorted(root.glob("*/agent.json")) if root.is_dir() else []:
        try:
            a = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(a, dict) or a.get("schema") != 1 or not AGENT_ID.fullmatch(str(a.get("id") or "")):
            continue
        if a["id"] == "hermes" and any(x["main"] for x in out):
            continue   # the main agent, already listed as the host's agent
        perms = a.get("permissions") if isinstance(a.get("permissions"), dict) else {}
        out.append({"id": a["id"], "name": str(a.get("label") or a["id"]), "main": a["id"] == "hermes",
                    "private": a.get("privacy") == "private", "limited": isinstance(perms.get("wizard_apps"), list)})
    return out
