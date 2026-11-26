"""Backup health for Home > Resources > Health (wizard-ux#1743, switch wizard/backup-health).

A stock box has no watchdog writing Health rows, so the shell never showed how the box's own backup is doing. This
reads what `wam backup` already writes (<wizard home>/backup.json and backup-status.json) and gives the shell, at
/wizard-os-home/store-install/_backup-health (StoreHandler, owner only), the same row shape as wizard-ux
tools/system-health.mjs: {key, label, status, value, detail}.

  Backup row (only while scheduled backups are on): green "Last backup 11h ago, checked, 4 kept"; amber after 36 h,
  when the last run wasn't checked or carries warnings (in plain words); red after 48 h, when the last run failed
  (with its error) or when no backup ever succeeded. A weekly schedule gets 7x those ages.
  Main agent row (#885): while the last backup left the box's main agent out, with an `include` action. Include it
  runs the disk estimate `wam alongside` shows (#960) with the host Hermes counted in, and only when it fits turns
  alongside mode on (the same code as the CLI), so the next backup includes it; otherwise it says why, with numbers.

After every backup run (switch on) the owner gets one Notifications message the first time the main agent is left
out; it is forgotten once a backup includes it again, so a later lapse is told again.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SWITCH = "wizard/backup-health"
STORE_PATH = "_backup-health"     # under /wizard-os-home/store-install/ (StoreHandler)
STATE = "backup-health.json"      # <wizard home>: which left-out main agent the owner was already told about
AMBER_HOURS, RED_HOURS = 36, 48
VIEW = "/wizard-os-home/?view=backup-health"   # wizard-ux opens Home > Resources > Health there
NOTE_TITLE = "Your main agent isn't in your backup"


def switch_on(home: Path | None = None) -> bool:
    """Whether the owner turned wizard/backup-health on (tests monkeypatch this)."""
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home, timeout=4)


def _load(p: Path) -> dict:
    try:
        v = json.loads(Path(p).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _age_hours(stamp, now: float) -> float | None:
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        return max(0.0, (now - float(stamp)) / 3600)
    try:
        from datetime import datetime
        return max(0.0, (now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()) / 3600)
    except (TypeError, ValueError):
        return None


def ago(hours: float) -> str:
    if hours < 1:
        m = int(hours * 60)
        return "just now" if m < 2 else f"{m} min ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)} days ago"


def _clip(s, n: int = 300) -> str:
    return " ".join(str(s or "").split())[:n]


def main_agent(st: dict | None) -> dict | None:
    """The main agent the last backup left out, from its warnings (#885 / #960), else None."""
    from .backup.box import MAIN_AGENT_LEFT_OUT
    from .backup.capture import MAIN_AGENT_NO_ROOM
    for w in (st or {}).get("warnings") or []:
        w = str(w)
        if w.startswith(MAIN_AGENT_LEFT_OUT):
            return {"state": "left_out", "path": w[len(MAIN_AGENT_LEFT_OUT):].split(" (", 1)[0].strip()}
        if w.startswith(MAIN_AGENT_NO_ROOM):
            return {"state": "no_room", "path": w[len(MAIN_AGENT_NO_ROOM):].split(" needs about", 1)[0].strip(),
                    "detail": w[len(MAIN_AGENT_NO_ROOM):]}
    return None


def plain_warning(w) -> str:
    """One backup warning in plain words."""
    from .backup.box import MAIN_AGENT_LEFT_OUT
    from .backup.capture import MAIN_AGENT_NO_ROOM
    w = str(w)
    if w.startswith(MAIN_AGENT_LEFT_OUT):
        return "Your main agent (its memory, skills and chats) isn't in this backup."
    if w.startswith(MAIN_AGENT_NO_ROOM):
        return "Your main agent was left out of this backup: not enough free disk."
    return _clip(w, 200)


def backup_row(home: Path, now: float | None = None) -> dict | None:
    """The Backup row, or None while scheduled backups are off (or not set up)."""
    now = time.time() if now is None else now
    cfg = _load(Path(home) / "backup.json")
    if not cfg.get("target") or not cfg.get("enabled"):
        return None
    st = _load(Path(home) / "backup-status.json")
    k = 7 if cfg.get("every") == "weekly" else 1
    row = {"key": "backups", "label": "Backup"}
    age = _age_hours(st.get("last_success_at"), now)
    if age is None:
        err = _clip(st.get("error"))
        return {**row, "status": "error", "value": "No backup yet",
                "detail": f"No backup has finished yet. {err}".strip() if err else
                "No backup has finished yet. The first one runs tonight."}
    when = ago(age)
    if st.get("ok") is False:
        return {**row, "status": "error", "value": "Last backup failed",
                "detail": f"{_clip(st.get('error'), 240) or 'The last backup did not finish.'} Last good backup {when}."}
    if age > RED_HOURS * k:
        return {**row, "status": "error", "value": f"Last backup {when}",
                "detail": "Backups have stopped succeeding."}
    kept = st.get("snapshots")
    kept = f", {kept} kept" if isinstance(kept, int) and not isinstance(kept, bool) and kept > 0 else ""
    warnings = [plain_warning(w) for w in st.get("warnings") or []]
    if age > AMBER_HOURS * k:
        return {**row, "status": "warn", "value": f"Last backup {when}",
                "detail": " ".join(["The nightly backup is late.", *warnings])}
    if st.get("verified") is not True:
        return {**row, "status": "warn", "value": f"Last backup {when}, not checked{kept}",
                "detail": " ".join(["The last backup was not checked after it ran.", *warnings])}
    if warnings:
        n = len(warnings)
        return {**row, "status": "warn", "value": f"Last backup {when}, {n} warning{'s' if n > 1 else ''}",
                "detail": " ".join(warnings)}
    return {**row, "status": "ok", "value": f"Last backup {when}, checked{kept}", "detail": ""}


def main_agent_row(home: Path) -> dict | None:
    """The Main agent row while the last backup left it out (stays until a backup includes it)."""
    from . import alongside
    ma = main_agent(_load(Path(home) / "backup-status.json"))
    if not ma or not _load(Path(home) / "backup.json").get("enabled"):
        return None
    row = {"key": "main-agent-backup", "label": "Main agent"}
    if ma["state"] == "no_room":
        return {**row, "status": "warn", "value": "not in backup",
                "detail": "Your main agent was left out of the last backup: " + _clip(ma.get("detail"), 240)}
    if alongside.load(Path(home)):
        return {**row, "status": "ok", "value": "in the next backup",
                "detail": "Turned on. Your next nightly backup includes your main agent."}
    return {**row, "status": "warn", "value": "not in backup",
            "detail": "Your main agent (its memory, skills and every chat) isn't in your backup. "
                      "Include it checks there is room on the disk first.",
            "action": {"id": "include-main-agent", "label": "Include it"}}


def status(home: Path, now: float | None = None) -> dict:
    rows = [r for r in (backup_row(home, now), main_agent_row(home)) if r]
    return {"available": True, "checks": rows,
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))}


# ---- Include it ----------------------------------------------------------------------------------------------------

def _room(c, path: Path) -> dict:
    from .backup import capture
    from .backup.box import Box
    return capture.stream_room(Box(c.home, c.runtipi_root), host_home=path)


def include(c, room=None, turn_on=None) -> dict:
    """Include it: disk estimate first (the host Hermes counted in), then `wam alongside`. Never half-done: a
    refusal changes nothing on the box."""
    from . import alongside
    from .backup.box import human, host_main_agent_home
    if alongside.load(Path(c.home)):
        return {"ok": True, "state": "on", "message": "Already on. Your next nightly backup includes your main agent."}
    path = host_main_agent_home(os.environ)
    if not path:
        return {"ok": False, "state": "no_agent",
                "reason": "There is no main agent Hermes on this box to include, so there is nothing to turn on."}
    if not alongside.detect(Path(path))["found"]:
        return {"ok": False, "state": "no_hermes",
                "reason": f"The hermes command isn't installed for this box's owner, so {path} can't be included "
                          "yet. Ask your Wizard to check the Hermes install."}
    try:
        r = (room or _room)(c, Path(path))
    except Exception as e:  # noqa: BLE001 - no estimate, no change
        return {"ok": False, "state": "unchecked",
                "reason": f"Wizard couldn't check the free disk space ({_clip(e, 120)}). Nothing was changed; "
                          "try again later."}
    need, free = int(r.get("need") or 0), int(r.get("free") or 0)
    if not r.get("fits"):
        return {"ok": False, "state": "no_room", "need_bytes": need, "free_bytes": free,
                "reason": f"Not enough room yet: backing up your main agent needs {human(need)} free, "
                          f"{human(free)} free. Free up about {human(need - free)} of disk (old downloads, "
                          "caches or apps you don't use), then tap Include it again."}
    try:
        out = (turn_on or _turn_on)(c, path)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - SystemExit: alongside.enable's refusals
        return {"ok": False, "state": "error",
                "reason": f"Turning it on didn't work ({_clip(e, 160)}). Nothing else was changed."}
    return {"ok": True, "state": "on", "need_bytes": need, "free_bytes": free, "next": out.get("next"),
            "message": f"Done. Your next nightly backup includes your main agent (it needs {human(need)} of "
                       f"the {human(free)} free)."}


def _turn_on(c, path: str) -> dict:
    from . import cli
    return cli.alongside_cmd(c, False, False, path, None)


# ---- tell the owner once -------------------------------------------------------------------------------------------

def _base_url(home: Path) -> str:
    net = _load(Path(home) / "settings.json").get("network") or {}
    u = net.get("base_url") if isinstance(net, dict) else None
    return u.rstrip("/") if isinstance(u, str) and u.startswith("https://") else ""


def _save(home: Path, st: dict) -> None:
    p = Path(home) / STATE
    tmp = p.with_name(f"{STATE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    os.replace(tmp, p)


def _notify(home: Path, title: str, message: str, link: str) -> bool:
    from . import app_updates
    from types import SimpleNamespace
    return app_updates._notify(SimpleNamespace(home=home), title, message, link=link, required=True)


def after_run(home: Path, st: dict | None, notify=None, on=None) -> str:
    """Called after every backup run. Tells the owner once when the main agent is left out; forgets it when a backup
    includes it again. Returns what it did (for tests and the log)."""
    home = Path(home)
    told = _load(home / STATE)
    ma = main_agent(st)
    if not ma and not told.get("main_agent"):
        return "nothing"                      # the common case reads no switch (no network)
    if not (on or switch_on)(home):
        return "off"
    if not ma:
        _save(home, {k: v for k, v in told.items() if k != "main_agent"})
        return "cleared"
    if told.get("main_agent") == ma["path"]:
        return "already told"
    msg = ("Last night's backup left out your main agent (its memory, skills and every chat). "
           + ("There wasn't enough free disk for it. " if ma["state"] == "no_room" else "")
           + "Open Home › Resources › Health to include it.")
    base = _base_url(home)
    if not (notify or _notify)(home, NOTE_TITLE, msg, base + VIEW if base else ""):
        return "not delivered"                # tried again after the next run
    _save(home, {**told, "main_agent": ma["path"], "told_at": int(time.time())})
    return "told"
