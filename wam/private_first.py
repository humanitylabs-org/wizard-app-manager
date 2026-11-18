"""Private first at install time (wizard-app-manager#759, wizard-os#676, DECISIONS #115).

The Agent Service decides "new installs start private" from the shell tweak `wizard/private-first-setup` in the
owner's Look file. A fresh box has no Look file when first-run setup runs, so the Agent Service also reads its env
`WIZARD_PRIVATE_FIRST`: `1` forces it on, `0` forces it off, unset leaves the tweak in charge (spec/AGENTS.md
section 10).

This module carries the installer's choice into that env:

    curl -fsSL aiwizards.com/os.sh | sudo WIZARD_PRIVATE_FIRST=1 bash
    wam private-first on|off|clear|status

The choice is recorded in <wizard home>/private-first.json and written as its own marked block
(`x-wizard-private-first`) into the Agent Service app's Runtipi user-config override, so it is kept across app
updates and alongside rewrites, and it is in place before `wam bootstrap` first creates the container. Nothing is
recorded unless someone asks: without it every box behaves exactly as before.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import browser_boundary as _bb

VAR = "WIZARD_PRIVATE_FIRST"
MARK = "x-wizard-private-first"
STATE = "private-first.json"
ROLES = ("service", "bundle")   # the apps that run an Agent Service (alongside.role_of)
_ON = {"1", "on", "true", "yes"}
_OFF = {"0", "off", "false", "no"}


def parse(value) -> str | None:
    """'1' / '0' for a clear on / off, None for unset or anything else (never guess an owner's privacy choice)."""
    v = str(value or "").strip().lower()
    return "1" if v in _ON else "0" if v in _OFF else None


def state_path(home) -> Path:
    return Path(home) / STATE


def choice(home) -> str | None:
    """The recorded choice: '1', '0' or None (none recorded)."""
    try:
        v = json.loads(state_path(home).read_text()).get("value")
    except (OSError, ValueError, AttributeError):
        return None
    return v if v in ("1", "0") else None


def record(home, value: str | None) -> bool:
    """Record '1' / '0', or with None forget the choice. True when the file changed."""
    p = state_path(home)
    if value is None:
        if not p.is_file():
            return False
        p.unlink()
        return True
    if choice(home) == value:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"value": value, "issue": "humanitylabs-org/wizard-app-manager#759"}, f)
        f.write("\n")
    tmp.replace(p)
    return True


def overlay(service: str, value: str | None) -> dict | None:
    """The block for the Agent Service's main service, or None (no block) when nothing is recorded."""
    if value not in ("1", "0"):
        return None
    return {"service": service, "environment": {VAR: value}, "volumes": []}


def override_path(runtipi_root, store: str, app_id: str) -> Path:
    return _bb.override_path(runtipi_root, store, app_id)


def sync_app(home, runtipi_root, store: str, app_id: str) -> bool:
    """Bring one Agent Service app's override in line with the recorded choice. True when the file changed."""
    return _bb.write_overlay(override_path(runtipi_root, store, app_id), overlay(app_id, choice(home)), MARK)


def set_box(home, runtipi_root, store: str, app_ids, value: str | None, dry_run: bool = False) -> dict:
    """`wam private-first on|off|clear`: check every app's override first (a malformed one refuses the whole change,
    nothing written), then record the choice and write each block. Restarting the apps is the caller's."""
    block_of = {a: overlay(a, value) for a in app_ids}
    plans = {a: _bb.render(override_path(runtipi_root, store, a), block_of[a], MARK) for a in app_ids}
    changed = sorted(a for a, r in plans.items() if r is not None)
    if not dry_run:
        record(home, value)
        for a in changed:
            _bb.write_overlay(override_path(runtipi_root, store, a), block_of[a], MARK)
    return {"value": value, "changed": changed, "dry_run": dry_run}


def status_one(runtipi_root, store: str, app_id: str) -> str | None:
    """The value written in that app's override block, or None when it has no block."""
    from .network import load_yaml
    p = override_path(runtipi_root, store, app_id)
    try:
        text = p.read_text()
        if MARK not in text:
            return None
        doc = load_yaml(text) or {}
        env = ((doc.get("services") or {}).get((doc.get(MARK) or {}).get("service")) or {}).get("environment") or {}
        v = env.get(VAR)
        return None if v is None else str(v)
    except Exception:   # noqa: BLE001 - an unreadable override reports "no block", never crashes status
        return None


def status(home, runtipi_root, store: str, app_ids) -> dict:
    return {"value": choice(home), "apps": {a: status_one(runtipi_root, store, a) for a in app_ids}}
