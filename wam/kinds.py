"""Which group each item on the Connections map sits in: Agents, Apps, Connectors or System.

The group comes from the fields a wizard.md already has, by one rule (docs/APP_NETWORKS.md, "Groups on the map"):

1. `kind:` in wizard.md (agent | app | connector), when an app wants to say it itself. Optional.
2. `type: agent` (or `category: agent`): an agent.
3. The active shell and Agent Service (the gateway core, network.plan): system. Only the core is System: those items
   can't be cut off, so `kind: system` on any other app is ignored.
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
            "system": w.get("system") is True, **({"kind": k} if k in KINDS else {}),
            # `privacy: private` (registry.py): the main agent gets it only once the owner ticks it in Agents
            **({"private": True} if str(w.get("privacy", "")).strip().lower() == "private" else {})}


def kind_of(meta: dict | None, core: bool = False) -> str:
    """The group of one app (see the module docstring). System is the gateway core only: those items can't be cut
    off, so no other app can put itself there (`kind: system` elsewhere is ignored). The core is System unless it is
    the agent. Without wizard.md fields: system for the core, else app."""
    m = meta if isinstance(meta, dict) else {}
    own = m.get("kind") if m.get("kind") in KINDS else None
    agent = own == "agent" or (own is None and (m.get("type") == "agent" or m.get("category") == "agent"))
    if core:
        return "agent" if agent else "system"
    if agent:
        return "agent"
    if own in ("app", "connector"):
        return own
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


def agents_dir(home: Path | None, runtipi_root: Path | None, store: str = "wizardapps") -> Path | None:
    """Where the Agent Service keeps its agents (its own app data; the same order as wam backup's agents_root):
    app-data/<store>/wizard-agent-service/data/wizard-os/agents, the pre-split bundle, the Wizard home, a shared
    app-data/wizard-os. The first that exists, else None."""
    from .slots import BUNDLE_FILLS
    cands = []
    if runtipi_root:
        r = Path(runtipi_root)
        cands += [r / "app-data" / store / app / "data" / "wizard-os" / "agents"
                  for app in ("wizard-agent-service", *[a for a, f in BUNDLE_FILLS.items() if "agent-service" in f])]
    if home:
        cands.append(Path(home) / "agents")
    if runtipi_root:
        cands.append(Path(runtipi_root) / "app-data" / "wizard-os" / "agents")
    for d in cands:
        try:
            if d.is_dir():
                return d
        except OSError:
            continue
    return None


def _read(f: Path):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def _apps(perms) -> list[str] | None:
    """The app ids an agent may use (Agent Service: wizard_apps + agent_apps), or None when it doesn't say."""
    if not isinstance(perms, dict) or not any(isinstance(perms.get(k), list) for k in ("wizard_apps", "agent_apps")):
        return None
    return sorted({str(x) for k in ("wizard_apps", "agent_apps") for x in (perms.get(k) or []) if isinstance(x, str)})


def main_agent_apps(root: Path | None) -> list[str] | None:
    """The main agent's app lists (`.main-agent.json`, Agent Service #58). None: absent, i.e. every installed app."""
    st = _read(root / ".main-agent.json") if root else None
    return _apps(st) if isinstance(st, dict) and st.get("schema") == 1 else None


def agents(home: Path | str | None, runtipi_root: Path | str | None = None) -> list[dict]:
    """The agents this box knows about: the host's own agent on an alongside box (alongside.json) and the Agent
    Service's agents registry (`agents/<id>/agent.json`, spec/AGENTS.md). Each: {id, name, main, private, apps, app}.
    `apps`: the app ids it may use (the gateway enforces them), None = every installed app. `app`: the installed app
    that runs it (`runtime.app_id`), so the map shows that agent once."""
    out: list[dict] = []
    root = agents_dir(Path(home) if home else None, Path(runtipi_root) if runtipi_root else None)
    ext = None
    if home:
        try:
            from . import alongside
            ext = (alongside.external_slots(Path(home)) or {}).get("agent")
        except Exception:  # noqa: BLE001 - no alongside state: no host agent
            ext = None
    if isinstance(ext, dict) and ext.get("id"):
        out.append({"id": str(ext["id"]), "name": str(ext.get("name") or ext["id"]), "main": True, "private": False,
                    "apps": main_agent_apps(root), "app": None})
    for f in sorted(root.glob("*/agent.json")) if root else []:
        a = _read(f)
        if not isinstance(a, dict) or a.get("schema") != 1 or not AGENT_ID.fullmatch(str(a.get("id") or "")):
            continue
        if a["id"] == "hermes" and any(x["main"] for x in out):
            continue   # the main agent, already listed as the host's agent
        rt = a.get("runtime") if isinstance(a.get("runtime"), dict) else {}
        out.append({"id": a["id"], "name": str(a.get("label") or a["id"]), "main": a["id"] == "hermes",
                    "private": a.get("privacy") == "private", "apps": _apps(a.get("permissions")),
                    "app": str(rt["app_id"]) if isinstance(rt.get("app_id"), str) else None})
    return out
