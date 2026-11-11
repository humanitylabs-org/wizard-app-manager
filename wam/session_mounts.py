"""Keep the --share-sessions file mounts live (wizard-app-manager#577).

In alongside mode with `--share-sessions` the Agent Service gets Hermes's state.db, state.db-wal and state.db-shm as
three single-file bind mounts (alongside.session_state_mounts). A file bind mount pins the inode it was created with.
When Hermes closes its database (a Hermes restart), SQLite deletes -wal/-shm and makes new ones on reopen, so the
container keeps reading the old, unlinked files: archiving looked like it did nothing and new threads didn't show
until someone restarted the Agent Service.

A small systemd user timer (`wizard-session-mounts.timer`, every minute) runs `wam alongside --refresh-mounts`. That
compares the inodes recorded in the override with the host files (alongside.stale_session_mounts). Only when they
differ AND all three host files exist again (a missing source path makes Docker create a root-owned directory in the
Hermes home, or fail to start) it rewrites the override and restarts the Agent Service, which recreates its container
on the live files. Fresh mounts cost one stat per file and touch nothing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import alongside

UNIT = "wizard-session-mounts"


def plan(override_text: str, hermes_home: str) -> dict:
    """What a refresh would do: {"state": "fresh" | "waiting" | "stale", "stale": [names]}.

    fresh: the mounts match the host files (or nothing is shared). waiting: Hermes replaced a file but not all three
    exist right now (Hermes is between close and reopen), so a restart would mount a missing path. stale: restart."""
    stale = alongside.stale_session_mounts(override_text, hermes_home)
    if not stale:
        return {"state": "fresh", "stale": []}
    present = alongside.session_inodes(hermes_home)
    ready = all(n in present for n in alongside.SESSION_STATE_FILES)
    return {"state": "stale" if ready else "waiting", "stale": stale}


def _systemctl(*args: str):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def _quote(v) -> str:
    # systemd quoting, not shell quoting; keep spaces and literal percent signs.
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def ensure_timer(home: Path, runtipi_root: Path, on: bool, unit_dir: Path | None = None) -> str:
    """Install (on) or remove (off) the every-minute refresh timer. Never raises: the result is reported."""
    units = Path(unit_dir or Path.home() / ".config/systemd/user")
    svc, tmr = units / f"{UNIT}.service", units / f"{UNIT}.timer"
    try:
        if not on:
            if not tmr.exists() and not svc.exists():
                return "off"
            _systemctl("disable", "--now", f"{UNIT}.timer")
            tmr.unlink(missing_ok=True); svc.unlink(missing_ok=True)
            _systemctl("daemon-reload")
            return "removed"
        units.mkdir(parents=True, exist_ok=True)
        svc.write_text(
            "[Unit]\nDescription=Remount Hermes session files in the Agent Service after Hermes replaces them\n\n"
            "[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n"
            f"Environment={_quote('WIZARD_OS_HOME=' + str(home))}\n"
            f"Environment={_quote('RUNTIPI_ROOT=' + str(runtipi_root))}\n"
            f"ExecStart={_quote(Path.home() / '.local/bin/wam')} alongside --refresh-mounts\nTimeoutStartSec=6min\n")
        tmr.write_text(
            "[Unit]\nDescription=Keep the Agent Service's Hermes session mounts live\n\n"
            "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\nAccuracySec=10s\n\n"
            "[Install]\nWantedBy=timers.target\n")
        for args in (("daemon-reload",), ("enable", "--now", f"{UNIT}.timer")):
            r = _systemctl(*args)
            if r.returncode:
                return f"error: systemctl {' '.join(args)}: {(r.stderr or '').strip()[:160]}"
        return f"timer:{UNIT}.timer"
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {str(e)[:160] or type(e).__name__}"
