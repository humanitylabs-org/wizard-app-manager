"""`wam doctor`: does the Agent Service's gateway hold a grant key for every app with tools? (#387)

The gateway reads its app keys from `<WIZARD_OS_HOME>/grants/agent-service.json` (`{"outgoing": {"<app id>": "<key>"}}`,
spec/PERMISSIONS.md layer 2) and sends `X-Wizard-Grant`. Without a key the app treats every agent as unknown and its
tools silently vanish, while Settings still shows the owner's chosen level. Nothing in the App Manager writes that file
yet (the producer is separate work on #387), so this check only makes the gap visible. Read-only: it never creates,
changes or prints a key; it reports app ids only.

Behind the off-by-default feature switch `wizard/doctor-gateway-keys` (Settings › Try new features, For testers).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SWITCH = "wizard/doctor-gateway-keys"
NAME = "agent connections: gateway holds a key for every app with tools"
KEY = re.compile(r"[0-9a-f]{64}")
AGENT_SERVICE_APPS = ("wizard-agent-service", "wizard-shell")   # the gateway itself (and the pre-split bundle)


def path(home: Path | None, runtipi_root: Path | None, store: str = "wizardapps") -> Path | None:
    """The gateway file under the Agent Service's own WIZARD_OS_HOME (its data mount, e.g. /state/wizard-os inside
    the container = app-data/<store>/wizard-agent-service/data/wizard-os on the host). The installed service's own data
    folder wins, even before its agents/ folder exists, so a legacy ~/.wizard-os/agents can't redirect the check to the
    wrong file; otherwise the same lookup as its agents (kinds.agents_dir). None when no Agent Service data is here."""
    from .kinds import agents_dir
    if runtipi_root:
        for app in AGENT_SERVICE_APPS:
            data = Path(runtipi_root) / "app-data" / store / app / "data"
            try:
                if data.is_dir():
                    return data / "wizard-os" / "grants" / "agent-service.json"
            except OSError:
                continue
    d = agents_dir(home, runtipi_root, store)
    if d:
        return d.parent / "grants" / "agent-service.json"
    return None


def _read(p: Path, runtipi_root: Path | None, rt_read=None) -> bytes | None:
    """The file's bytes, None when it does not exist. It is 0600 and owned by the Agent Service's user (often root),
    so when the CLI user can't open it, read it through the runtipi container like app_rename does."""
    try:
        return p.read_bytes() if p.is_file() else None
    except PermissionError:
        pass
    if rt_read is None:
        from .app_rename import rt_read
    try:
        if runtipi_root is None:
            raise ValueError
        inner = "/data/" + str(p.relative_to(Path(runtipi_root)))
    except ValueError:
        raise PermissionError(f"can't read {p}")
    return rt_read(inner)


def check(reg: dict, home: Path | None, runtipi_root: Path | None, rt_read=None) -> tuple[str, bool, str] | None:
    """One doctor row, or None when there is nothing to check (no Agent Service installed or no app with tools)."""
    apps = [a for a in (reg or {}).get("apps") or [] if isinstance(a, dict)]
    if not any(a.get("id") in AGENT_SERVICE_APPS for a in apps):
        return None
    tools = sorted({a["id"] for a in apps if a.get("mcp") and a.get("id") and a["id"] not in AGENT_SERVICE_APPS})
    if not tools:
        return None
    p = path(home, runtipi_root)
    if p is None:
        return (NAME, False, "can't find the Agent Service's data folder")
    try:
        raw = _read(p, runtipi_root, rt_read)
    except Exception as e:  # noqa: BLE001 - a doctor row, never a crash
        return (NAME, False, f"can't read {p}: {str(e)[:120]}")
    keys: dict = {}
    if raw is not None:
        try:
            doc = json.loads(raw)
            og = doc.get("outgoing") if isinstance(doc, dict) else None
            keys = og if isinstance(og, dict) else {}
        except ValueError:
            return (NAME, False, f"{p} is not valid JSON")
    missing = [i for i in tools if not (isinstance(keys.get(i), str) and KEY.fullmatch(keys[i]))]
    if not missing:
        return (NAME, True, f"{len(tools)} apps ({p})")
    where = "no gateway key file" if raw is None else "no valid key"
    return (NAME, False, f"{where} for {', '.join(missing)[:300]}: agents can't use these apps' tools even though "
                         f"Settings shows a level (#387; {p})")
