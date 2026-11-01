"""Which group each item on the Connections map sits in: Agents, Apps, Agent apps or Connectors.

The group comes from the fields a wizard.md already has, by one rule (docs/APP_NETWORKS.md, "Groups on the map"):

1. The owner's box-level choice (`app-kinds.json` in the Wizard home, see `box_kinds`). Optional, owner-editable.
2. `kind:` in wizard.md (agent | app | agent-app | connector), when an app wants to say it itself. Optional.
3. `type: agent` (or `category: agent`): an agent.
4. `type: external` (it connects the box to an outside service: a bank, Cloudflare, an AI provider): a connector.
5. `type: view`: an app (a screen is the whole point).
6. No `view:` at all: an agent app (a tool mainly for the agent; the owner watches what it does), except Wizard's own
   core (the Agent Service), which links the agent to the apps: a connector.
7. `category: system` with a view (its screen only sets it up, e.g. a music source's sign-in): a connector.
8. Anything else: an app.

System is not a group. Wizard's own core (the active shell and Agent Service, network.plan's gateway core) sits in the
group the rule gives it and carries a "comes with Wizard" badge (`builtin`); it can't be cut off.

No app id is listed anywhere: a new app lands in the right group from its own wizard.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

KINDS = ("agent", "app", "agent-app", "connector")
BOX_KINDS = "app-kinds.json"   # the owner's own choices, in the Wizard home: {"kinds": {"<app id>": "<kind>"}}
AGENT_ID = re.compile(r"[a-z][a-z0-9-]{0,30}")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def fields(w: dict | None) -> dict:
    """The wizard.md fields the rule reads, kept with each app the planner reads (network.read_app)."""
    w = w if isinstance(w, dict) else {}
    k = str(w.get("kind") or "").strip().lower()
    return {"type": w.get("type"), "category": w.get("category"), "view": bool(w.get("view")),
            "system": w.get("system") is True, **({"kind": k} if k in KINDS else {}),
            # `privacy: private` (registry.py): the main agent gets it only once the owner ticks it in Agents
            **({"private": True} if str(w.get("privacy", "")).strip().lower() == "private" else {})}


def kind_of(meta: dict | None, core: bool = False, box: str | None = None) -> str:
    """The group of one app (see the module docstring). `box`: the owner's box-level choice for it (box_kinds), which
    wins over wizard.md. `core`: Wizard's own core (shell, Agent Service): grouped by the same rule, except that a core
    item without a screen links the agent to the apps (a connector). Without wizard.md fields: app."""
    if box in KINDS:
        return box
    m = meta if isinstance(meta, dict) else {}
    own = m.get("kind") if m.get("kind") in KINDS else None
    if own:
        return own
    if m.get("type") == "agent" or m.get("category") == "agent":
        return "agent"
    if not m:
        return "app"
    if m.get("type") == "external":
        return "connector"
    if m.get("type") == "view":
        return "app"
    if not m.get("view"):
        return "connector" if core else "agent-app"
    if m.get("category") == "system":
        return "connector"
    return "app"


def box_kinds(home: Path | str | None) -> dict[str, str]:
    """The owner's box-level groups: `<Wizard home>/app-kinds.json`, `{"kinds": {"<app id>": "agent-app", ...}}`.
    For an app whose wizard.md the owner can't change (someone else's repo). Bad ids and values are ignored; a missing
    or unreadable file is no choices."""
    doc = _read(Path(home) / BOX_KINDS) if home else None
    got = doc.get("kinds") if isinstance(doc, dict) else None
    if not isinstance(got, dict):
        return {}
    return {k: v for k, v in got.items() if isinstance(k, str) and _ID.fullmatch(k) and v in KINDS}


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
                    "private": a.get("privacy") == "private",
                    # the main agent's lists live in .main-agent.json (Agent Service #58), not in a registry entry
                    "apps": main_agent_apps(root) if a["id"] == "hermes" else _apps(a.get("permissions")),
                    "app": str(rt["app_id"]) if isinstance(rt.get("app_id"), str) else None})
    return out
