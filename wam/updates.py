"""Owner-chosen updates: Automatic or Ask me.

A box updates the way it always has: re-running the installer moves it to the release that `releases/stable`
names, in the mode the box already has (get.sh). This module adds only the two things an owner needs on top:

- a check: which release runs here (~/.wizard-os/release.json) vs the current stable, so a shell can show
  "Update available" with an Update button;
- a choice, saved in ~/.wizard-os/updates.json: "ask" (default; nothing runs by itself) or "auto" (a daily
  systemd user timer applies a new stable at night).

Applying = the documented agent-safe detached rerun (`os.sh | bash -s start`), never a second update path.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

MODES = ("ask", "auto")
DEFAULT_MODE = "ask"          # an owner opts in to automatic updates; nothing changes on a box until then
RELEASE_BASE = "https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases"
INSTALLER = "https://aiwizards.com/os.sh"
UNIT = "wizard-os-update"
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,40}")


def _home(home: Path | None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def mode(home: Path | None = None) -> str:
    try:
        m = json.loads((_home(home) / "updates.json").read_text()).get("mode")
    except (OSError, ValueError, AttributeError):
        m = None
    return m if m in MODES else DEFAULT_MODE


def installed(home: Path | None = None) -> str | None:
    """The release this box was last installed/updated to; None for an unpinned (developer) box."""
    try:
        rel = json.loads((_home(home) / "release.json").read_text()).get("release")
    except (OSError, ValueError, AttributeError):
        return None
    return rel if isinstance(rel, str) and VERSION.fullmatch(rel) else None


def latest(base: str | None = None, timeout: float = 10) -> str | None:
    base = (base or os.environ.get("WIZARD_RELEASE_BASE") or RELEASE_BASE).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/stable", timeout=timeout) as r:
            v = r.read(64).decode().strip()
    except (OSError, ValueError):
        return None
    return v if VERSION.fullmatch(v) else None


def _key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.+-]", v))


def status(home: Path | None = None, base: str | None = None) -> dict:
    have, want = installed(home), latest(base)
    newer = bool(want) and (have is None or _key(want) > _key(have))
    return {"mode": mode(home), "installed": have, "latest": want, "update_available": newer,
            "checked": want is not None}


def _timer_files(unit_dir: Path, wam: str) -> dict[Path, str]:
    return {
        unit_dir / f"{UNIT}.service": (
            "[Unit]\nDescription=Wizard OS automatic update (owner chose Automatic)\n\n"
            f"[Service]\nType=oneshot\nExecStart={wam} updates --apply-if-auto\n"),
        unit_dir / f"{UNIT}.timer": (
            "[Unit]\nDescription=Nightly Wizard OS update check\n\n"
            "[Timer]\nOnCalendar=*-*-* 03:30\nRandomizedDelaySec=1h\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def set_mode(new: str, home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    """Save the owner's choice; Automatic installs the nightly timer, Ask me removes it."""
    if new not in MODES:
        raise SystemExit(f"updates mode must be one of {', '.join(MODES)}")
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    tmp = h / "updates.json.tmp"
    tmp.write_text(json.dumps({"mode": new}) + "\n")
    tmp.replace(h / "updates.json")
    ud = unit_dir or Path.home() / ".config/systemd/user"
    files = _timer_files(ud, str(Path.home() / ".local/bin/wam"))
    run = (lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)) if systemctl \
        else (lambda *a: None)
    if new == "auto":
        ud.mkdir(parents=True, exist_ok=True)
        for p, text in files.items():
            p.write_text(text)
        run("daemon-reload"); run("enable", "--now", f"{UNIT}.timer")
    else:
        run("disable", "--now", f"{UNIT}.timer")
        for p in files:
            p.unlink(missing_ok=True)
        run("daemon-reload")
    return {"mode": new}


def apply(home: Path | None = None, base: str | None = None, runner=subprocess.run) -> dict:
    """Start the update now: the installer's own detached, agent-safe rerun. Progress: `os.sh | bash -s status`."""
    st = status(home, base)
    if not st["checked"]:
        return {**st, "started": False, "error": "could not read the current stable release; try again later"}
    if not st["update_available"]:
        return {**st, "started": False}
    r = runner(["bash", "-c", f"curl -fsSL {INSTALLER} | bash -s start"], capture_output=True, text=True, timeout=600)
    ok = r.returncode == 0
    return {**st, "started": ok, **({} if ok else {"error": (r.stderr or r.stdout or "").strip()[-300:]})}


def apply_if_auto(home: Path | None = None, base: str | None = None, runner=subprocess.run) -> dict:
    if mode(home) != "auto":       # the timer outlived a switch back to Ask me: do nothing
        return {"mode": mode(home), "started": False}
    return apply(home, base, runner)
