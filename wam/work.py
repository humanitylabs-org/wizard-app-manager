"""What is running on this box, by kind, with names an owner recognises.

Kinds and the release impact (update_impact level) that stops each:
  agent    chat replies, agent turns, sub-agents, scheduled runs, kanban cards       a chat reply: service; else: hermes
  task     background processes the agent started; App Manager jobs                  agent's: hermes; ours: service
  unknown  a source that couldn't be read: never read as idle                         hermes

Services (servers meant to keep running) are not listed here: an update never waits for them (plan Phase 7).
Every reader is read-only and cheap; inventory() is what `wam work` prints and update decisions use.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

LEVELS = ("none", "ui", "service", "hermes", "os")


def hermes_home(home: Path | None = None) -> Path | None:
    """This box's Hermes data: HERMES_HOME, an alongside box's hermes_home, the Runtipi hermes-agent app's data
    (fresh boxes, the Mac VM), else ~/.hermes. None when none of them exists."""
    from .updates import _home
    h = _home(home)
    candidates = []
    if os.environ.get("HERMES_HOME"):
        candidates.append(Path(os.environ["HERMES_HOME"]))
    try:
        al = json.loads((h / "alongside.json").read_text())
        if isinstance(al, dict) and al.get("hermes_home"):
            candidates.append(Path(al["hermes_home"]))
    except (OSError, ValueError):
        pass
    root = Path(os.environ.get("RUNTIPI_ROOT", Path.home() / "runtipi")).expanduser()
    candidates += sorted((root / "app-data").glob("*/hermes-agent/data"))
    candidates.append(Path.home() / ".hermes")
    for c in candidates:
        c = c.expanduser()
        if (c / "state.db").exists() or (c / "gateway_state.json").exists():
            return c
    return None


def _item(kind: str, name: str, source: str, stops_at: str, since: float | None = None, **extra) -> dict:
    return {"kind": kind, "name": name, "source": source, "stops_at": stops_at, "since": since, **extra}


def _unknown(source: str, why: str) -> dict:
    return _item("unknown", f"Couldn't read {source}", source, "hermes", why=why)


def _db(path: Path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3))


def _has(db, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _short(command: str | None, cwd: str | None = None, limit: int = 60) -> str:
    """A background command as a name: the command line cut short (secrets usually sit in env, not argv; the full
    command is never shown beyond `limit`), plus the folder it runs in."""
    text = " ".join(str(command or "").split())
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    where = Path(cwd).name if cwd else ""
    return f"{text} (in {where})" if where else text


def agent_service(home: Path | None) -> list[dict]:
    from . import updates
    service = updates.service_origin(home)
    if not service:
        return []
    state = updates.busy(service)
    if state is None:
        return [_unknown("the Agent Service", "its /wizard/health queue didn't answer")]
    return [_item("agent", "A chat reply", "agent-service", "service")] if state else []


def gateway(hh: Path) -> list[dict]:
    p = hh / "gateway_state.json"
    if not p.exists():
        return []
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        return [_unknown("Hermes gateway state", "unreadable")]
    work = st.get("active_work")
    if not work:
        return []
    return [_item("agent", "Hermes is working", "gateway", "hermes", detail=str(work)[:200])]


def state_db(hh: Path, now: float) -> list[dict]:
    p = hh / "state.db"
    if not p.exists():
        return []
    out = []
    try:
        with _db(p) as db:
            titles = _has(db, "sessions")
            if _has(db, "session_turn_leases"):
                for conv, acquired in db.execute(
                        "SELECT conversation_id, acquired_at FROM session_turn_leases WHERE expires_at > ?", (now,)):
                    title = None
                    if titles:
                        row = db.execute("SELECT title FROM sessions WHERE id=?", (conv,)).fetchone()
                        title = row[0] if row else None
                    out.append(_item("agent", f"Replying in “{title}”" if title else "Replying in a chat",
                                     "turn", "hermes", acquired))
            if _has(db, "async_delegations"):
                for _id, dispatched in db.execute("SELECT delegation_id, dispatched_at FROM async_delegations "
                                                  "WHERE state IN ('pending','running')"):
                    out.append(_item("agent", "A helper agent", "delegation", "hermes", dispatched))
    except (OSError, sqlite3.Error) as e:
        return [_unknown("Hermes state.db", str(e))]
    return out


def cron(hh: Path) -> list[dict]:
    p = hh / "cron/executions.db"
    if not p.exists():
        return []
    names = {}
    try:
        jobs = json.loads((hh / "cron/jobs.json").read_text())
        names = {j.get("id"): j.get("name") or j.get("id") for j in (jobs.get("jobs") or []) if isinstance(j, dict)}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with _db(p) as db:
            if not _has(db, "executions"):
                return []
            rows = db.execute("SELECT job_id, started_at FROM executions "
                              "WHERE status IN ('claimed','running') AND finished_at IS NULL").fetchall()
    except (OSError, sqlite3.Error) as e:
        return [_unknown("Hermes scheduled jobs", str(e))]
    return [_item("agent", f"Scheduled job “{names.get(job) or job}”", "cron", "hermes", None, started=started)
            for job, started in rows]


def kanban(hh: Path) -> list[dict]:
    p = hh / "kanban.db"
    if not p.exists():
        return []
    try:
        with _db(p) as db:
            if not _has(db, "tasks"):
                return []
            rows = db.execute("SELECT title FROM tasks WHERE status='running'").fetchall()
    except (OSError, sqlite3.Error) as e:
        return [_unknown("Hermes kanban", str(e))]
    return [_item("agent", f"Task “{title}”", "kanban", "hermes") for (title,) in rows]


def processes(hh: Path) -> list[dict]:
    """Background processes the agent started (Hermes' process checkpoint, rewritten as they start and end).
    They run inside the Hermes engine, so anything that restarts Hermes stops them."""
    p = hh / "processes.json"
    if not p.exists():
        return []
    try:
        entries = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [_unknown("Hermes background processes", "processes.json unreadable")]
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not e.get("pid") or e.get("exited"):
            continue
        out.append(_item("task", _short(e.get("command"), e.get("cwd")), "process", "hermes", e.get("started_at"),
                         session=e.get("session_id")))
    return out


def _held(lock: Path) -> bool:
    """True while another process holds this flock (never creates the file)."""
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def manager_jobs(home: Path | None) -> list[dict]:
    from . import updates
    h = updates._home(home)
    out = []
    if updates.install_state(home) == "running":
        out.append(_item("task", "Wizard OS update", "installer", "service"))
    # Not maintenance.lock: nightly maintenance is what starts a Linux box's Automatic update. `wam backup` keeps its
    # lock elsewhere (follow-up: list a running backup too).
    for lock, name in (("app-updates.lock", "App updates"), ("hermes-update.lock", "Hermes update"),
                       ("app-rename.lock", "Renaming an app")):
        if _held(h / lock):
            out.append(_item("task", name, lock, "service"))
    return out


_cache: dict = {}


def inventory(home: Path | None = None, now: float | None = None, max_age: float = 10) -> list[dict]:
    """Everything running, newest reading at most `max_age` seconds old."""
    now = time.time() if now is None else now
    key = str(home)
    hit = _cache.get(key)
    if hit and now - hit[0] < max_age:
        return hit[1]
    items = agent_service(home) + manager_jobs(home)
    hh = hermes_home(home)
    if hh is not None:
        items += gateway(hh) + state_db(hh, now) + cron(hh) + kanban(hh) + processes(hh)
    _cache[key] = (now, items)
    return items


def blocking(items: list[dict], interrupts: str | None) -> list[dict]:
    """The items an update that stops `interrupts` would cut off (unknown counts as hermes)."""
    if not interrupts or interrupts == "none":
        return []
    level = LEVELS.index(interrupts) if interrupts in LEVELS else LEVELS.index("hermes")
    return [i for i in items if LEVELS.index(i.get("stops_at", "hermes")) <= level]
