"""System app slots ("parking spots"): the capabilities every Wizard has, and which app fills each by default.

A slot is a job, not an app. Any app whose wizard.md `provides:` the slot can fill it, and the owner can swap it.
`defaults.json` (next to this file) names the default app per slot; `null` means the spot exists but no default app
is published yet. `wam bootstrap` installs the defaults for empty spots; `wam uninstall` won't empty a required one.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS_FILE = Path(__file__).with_name("defaults.json")


def load_defaults(path: Path = DEFAULTS_FILE) -> dict[str, dict]:
    from .release_pins import defaults
    return defaults(json.loads(path.read_text())["slots"])


# Apps that are no longer a slot default but are still installed on boxes and still updated from their repo.
# wizard-shell: the pre-split bundle (UX + Agent Service in one image), built from the wizard-agent-service repo.
RETIRED_REPOS = {"wizard-shell": "humanitylabs-org/wizard-agent-service"}


def repo_for(app_id: str, defaults: dict[str, dict] | None = None) -> str | None:
    """Repo (owner/name) a default app comes from, for defaults that aren't in the public index yet (private)."""
    defaults = load_defaults() if defaults is None else defaults
    return next((d.get("repo") for d in defaults.values() if d.get("app") == app_id and d.get("repo")),
                RETIRED_REPOS.get(app_id))


# The pre-split bundle is the shell and the Agent Service in one container: on a box that still runs it, both spots
# are filled, so `bootstrap` installs no second Agent Service beside it (the split is `wam shell-split`).
BUNDLE_FILLS = {"wizard-shell": ("shell", "agent-service")}


def providers(reg: dict) -> dict[str, list[str]]:
    """slot -> installed app ids that provide it (from apps.json)."""
    out: dict[str, list[str]] = {}
    for a in reg.get("apps") or []:
        for s in dict.fromkeys([*(a.get("provides") or []), *BUNDLE_FILLS.get(a["id"], ())]):
            out.setdefault(s, []).append(a["id"])
    return out


def table(reg: dict, defaults: dict[str, dict] | None = None) -> list[dict]:
    """Every slot, filled or empty: the parking-spot view the UX and `wam slots` show."""
    defaults = load_defaults() if defaults is None else defaults
    have = providers(reg)
    installed = {a["id"] for a in reg.get("apps") or []}
    chosen = {k: (v or {}).get("default") for k, v in (reg.get("slots") or {}).items()}
    external = reg.get("external") or {}
    rows = []
    for slot, d in defaults.items():
        if slot in external:   # filled from outside Runtipi (alongside mode: the host's own Hermes)
            e = external[slot]
            rows.append({"slot": slot, "name": d.get("name", slot), "required": bool(d.get("required")),
                         "default_app": d.get("app"), "installed": [], "active": e.get("id"), "state": "external",
                         "external": f"external: {e.get('name', e.get('id'))}", "note": d.get("note", "")})
            continue
        apps = have.get(slot, [])
        if not apps and d.get("app") in installed:   # a default that fills several spots before it's split
            apps = [d["app"]]
        rows.append({
            "slot": slot, "name": d.get("name", slot), "required": bool(d.get("required")),
            "default_app": d.get("app"), "installed": apps,
            "active": chosen.get(slot) if chosen.get(slot) in apps else (apps[0] if apps else None),
            "state": "filled" if apps else ("empty" if d.get("app") else "no default yet"),
            "note": d.get("note", ""),
        })
    return rows


def to_install(reg: dict, defaults: dict[str, dict] | None = None) -> list[str]:
    """Default apps to install so every spot that has a default is filled (an installed alternative counts)."""
    defaults = load_defaults() if defaults is None else defaults
    out: list[str] = []
    for row in table(reg, defaults):
        if row["state"] == "external":
            continue
        # Multi-provider slots are additive: Claude must not suppress the release's ChatGPT default.
        missing = row["default_app"] not in row["installed"] if defaults[row["slot"]].get("multi") else not row["installed"]
        if missing and row["default_app"] and row["default_app"] not in out:
            out.append(row["default_app"])
    return out


def blocks_uninstall(reg: dict, app_id: str, defaults: dict[str, dict] | None = None) -> list[str]:
    """Required slots that would be left empty if `app_id` were removed."""
    defaults = load_defaults() if defaults is None else defaults
    have = providers(reg)
    external = reg.get("external") or {}
    return [s for s, d in defaults.items() if d.get("required") and have.get(s) == [app_id] and s not in external]


def merge_into_registry(reg: dict, defaults: dict[str, dict] | None = None) -> dict:
    """apps.json gains `parking`: every slot including empty ones, so the UX can show placeholders."""
    reg["parking"] = [{k: r[k] for k in ("slot", "name", "required", "default_app", "active", "state", "external") if k in r}
                      for r in table(reg, defaults)]
    return reg
