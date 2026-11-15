"""Apps a failed update left stopped (#730).

An update that times out or can't roll back (#711) leaves an app that was running `stopped` on its old version.
Runtipi then only knows `stopped`, so the next update keeps it stopped (wasRunningBeforeUpdate=false) and `wam recover`
skips it. This module tells such an app apart from one the owner stopped, so the next update or recover starts it:

- wam records the app here (<wizard-os home>/update-stranded.json) when an update of a running app ends without it
  running again;
- Runtipi's own log (logs/app.log) shows, for boxes an earlier update already broke, an update failure after the app's
  last start, with no stop by the owner since.

An owner's Stop always wins: any `Stopping app <urn>` after the failure (or after wam's record) means it stays stopped.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

STATE = "update-stranded.json"
TAIL_BYTES = 8 * 1024 * 1024
# Failures where Runtipi sets `stopped` whatever the app was before (no rollback to the old state).
HARD = ("RPC response timed out", "Recovery failed", "Failed to process update result")


def _home(home) -> Path:
    if home:
        return Path(home)
    from . import registry
    return Path(registry.home())


def _load(home) -> dict:
    try:
        s = json.loads((_home(home) / STATE).read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(home, s: dict) -> None:
    h = _home(home)
    try:
        if not s:
            (h / STATE).unlink(missing_ok=True)
            return
        h.mkdir(parents=True, exist_ok=True)
        tmp = h / (STATE + ".tmp")
        tmp.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
        tmp.replace(h / STATE)
    except OSError:
        pass   # a record we can't write never fails an update


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def mark(home, urn: str, reason: str = "") -> None:
    s = _load(home)
    s[urn] = {"at": _now(), "reason": str(reason)[:200]}
    _save(home, s)


def clear(home, urn: str) -> None:
    s = _load(home)
    if urn in s:
        del s[urn]
        _save(home, s)


def marked(home) -> dict:
    return _load(home)


def log_files(runtipi_root) -> list[Path]:
    """Runtipi's app.log: RUNTIPI_LOGS_PATH from its .env, else the compose defaults (<root>/logs, .internal/logs)."""
    root = Path(runtipi_root)
    bases = []
    try:
        for line in (root / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "RUNTIPI_LOGS_PATH" and v.strip():
                p = Path(v.strip().strip("'\""))
                bases.append(p if p.is_absolute() else root / p)
    except OSError:
        pass
    bases += [root, root / ".internal"]
    out = []
    for b in bases:
        f = b / "logs" / "app.log"
        if f.is_file() and f not in out:
            out.append(f)
    return out


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def events(text: str, urn: str) -> list[tuple[str, str]]:
    """(timestamp, kind) for one app from Runtipi's log, oldest first. kind: started, stopping, failed, failed_hard."""
    u = re.escape(urn)
    end = r"(?![\w.-]|:\w)"
    pats = [
        ("started", re.compile(rf"> \s*App {u}{end} started\b")),
        ("stopping", re.compile(rf"> \s*Stopping app {u}{end}")),
        ("failed", re.compile(rf"> \s*Failed to (?:update app|process update result for app) {u}{end}")),
    ]
    out = []
    for line in text.splitlines():
        if urn not in line:
            continue
        for kind, rx in pats:
            if rx.search(line):
                if kind == "failed" and any(h in line for h in HARD):
                    kind = "failed_hard"
                out.append((line.split(" - ", 1)[0].strip(), kind))
                break
    return out


def from_log(evs: list[tuple[str, str]]) -> tuple[bool, str | None]:
    """(left stopped by a failed update, timestamp of the owner's last Stop or None)."""
    running = None      # unknown until the log shows a start or a stop
    stranded = False
    last_stop = None
    for ts, kind in evs:
        if kind == "started":
            running, stranded = True, False
        elif kind == "stopping":
            running, stranded, last_stop = False, False, ts
        elif running is True or (running is None and kind == "failed_hard"):
            stranded = True
    return stranded, last_stop


def stranded(c, urn: str, status: str | None) -> bool:
    """True when this `stopped` app was running before an update that failed to bring it back, and the owner has not
    stopped it since. Never raises: anything unreadable counts as \"stopped on purpose\"."""
    if status != "stopped":
        return False
    home = getattr(c, "home", None)
    try:
        evs = []
        for f in log_files(c.runtipi_root):
            evs = events(_tail(f), urn)
            if evs:
                break
        by_log, last_stop = from_log(evs)
        rec = _load(home).get(urn)
        if rec and last_stop and last_stop > str(rec.get("at") or ""):
            clear(home, urn)    # the owner stopped it after the failed update: it stays stopped
            rec = None
        return bool(by_log or rec)
    except Exception:  # noqa: BLE001 - a broken log never starts an app the owner stopped
        return False
