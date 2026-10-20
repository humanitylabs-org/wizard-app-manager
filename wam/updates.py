"""Owner-chosen updates: Automatic (default) or Ask me.

A box updates the way it always has: re-running the installer moves it to the release that `releases/stable`
names, in the mode the box already has (get.sh). This module adds only the two things an owner needs on top:

- a check: which release runs here (~/.wizard-os/release.json) vs the current stable, so a shell can show
  "Update available" with an Update button;
- a choice, saved in ~/.wizard-os/updates.json: "auto" (the default for every box whose owner never chose: a
  nightly systemd user timer applies a new stable) or "ask" (nothing runs by itself). An explicit choice is
  always kept; the installer never writes updates.json.

Applying = the documented agent-safe detached rerun (`os.sh | bash -s start`), never a second update path.

An unpinned box (no release in release.json: a developer checkout, WAM_REF, or a box from before releases) is
"updated by hand": it never reports an update and refuses to apply one unless explicitly forced, so neither the
Update button nor Automatic can move a developer box onto stable pins.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

MODES = ("ask", "auto")
DEFAULT_MODE = "auto"         # boxes whose owner never chose update automatically; an explicit "ask" is kept
RELEASE_BASE = "https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases"
INSTALLER = "https://aiwizards.com/os.sh"
UNIT = "wizard-os-update"
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,40}")
CACHE_SECONDS = 600           # a shell polling for "Update available" stays cheap (one fetch per ~10 minutes)
MISS_SECONDS = 60             # an unreachable stable pointer is retried sooner
_latest: dict[str, tuple[float, str | None]] = {}


def _home(home: Path | None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def _choice(home: Path | None) -> str | None:
    """The owner's explicit choice, or None when they never chose."""
    try:
        m = json.loads((_home(home) / "updates.json").read_text()).get("mode")
    except (OSError, ValueError, AttributeError):
        return None
    return m if m in MODES else None


def mode(home: Path | None = None) -> str:
    return _choice(home) or DEFAULT_MODE


def installed(home: Path | None = None) -> str | None:
    """The release this box was last installed/updated to; None for an unpinned (developer) box."""
    try:
        rel = json.loads((_home(home) / "release.json").read_text()).get("release")
    except (OSError, ValueError, AttributeError):
        return None
    return rel if isinstance(rel, str) and VERSION.fullmatch(rel) else None


def latest(base: str | None = None, timeout: float = 10, fresh: bool = False, now=time.monotonic) -> str | None:
    """The release `releases/stable` names, cached ~10 minutes (`fresh` skips the cache: apply / the timer)."""
    base = (base or os.environ.get("WIZARD_RELEASE_BASE") or RELEASE_BASE).rstrip("/")
    hit = _latest.get(base)
    if hit and not fresh and now() - hit[0] < (CACHE_SECONDS if hit[1] else MISS_SECONDS):
        return hit[1]
    try:
        with urllib.request.urlopen(f"{base}/stable", timeout=timeout) as r:
            v = r.read(64).decode().strip()
    except (OSError, ValueError):
        v = None
    v = v if v and VERSION.fullmatch(v) else None
    _latest[base] = (now(), v)
    return v


def _key(v: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.+-]", v))


def installing(home: Path | None = None) -> bool:
    """An installer run (e.g. an update just started) is still going (install-status.json, written by get.sh)."""
    try:
        return json.loads((_home(home) / "install-status.json").read_text()).get("state") == "running"
    except (OSError, ValueError, AttributeError):
        return False


def status(home: Path | None = None, base: str | None = None, fresh: bool = False) -> dict:
    have, want = installed(home), latest(base, fresh=fresh)
    unpinned = have is None
    newer = bool(want) and not unpinned and _key(want) > _key(have)
    return {"mode": mode(home), "chosen": _choice(home) is not None, "installed": have, "latest": want,
            "update_available": newer, "unpinned": unpinned, "checked": want is not None,
            "installing": installing(home)}


def _timer_files(unit_dir: Path, wam: str) -> dict[Path, str]:
    return {
        unit_dir / f"{UNIT}.service": (
            "[Unit]\nDescription=Wizard OS automatic update (owner setting: Automatic)\n\n"
            "[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n"
            # the installer's detached runner must outlive this oneshot when systemd-run is unavailable
            f"KillMode=process\nExecStart={wam} updates --apply-if-auto\n"),
        unit_dir / f"{UNIT}.timer": (
            "[Unit]\nDescription=Nightly Wizard OS update check\n\n"
            "[Timer]\nOnCalendar=*-*-* 03:30\nRandomizedDelaySec=1h\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def _systemctl(enabled: bool):
    if not enabled:
        return lambda *a: None
    return lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)


def ensure_timer(home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    """Make the nightly timer match the mode (Automatic: installed and enabled; Ask me: removed).

    Never writes updates.json, so a box whose owner never chose keeps following the default. The installer calls
    this on every install and rerun."""
    m = mode(home)
    ud = unit_dir or Path.home() / ".config/systemd/user"
    files = _timer_files(ud, str(Path.home() / ".local/bin/wam"))
    run = _systemctl(systemctl)
    if m == "auto":
        ud.mkdir(parents=True, exist_ok=True)
        for p, text in files.items():
            p.write_text(text)
        run("daemon-reload"); run("enable", "--now", f"{UNIT}.timer")
    else:
        run("disable", "--now", f"{UNIT}.timer")
        for p in files:
            p.unlink(missing_ok=True)
        run("daemon-reload")
    return {"mode": m, "timer": m == "auto"}


def set_mode(new: str, home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    """Save the owner's explicit choice; Automatic installs the nightly timer, Ask me removes it."""
    if new not in MODES:
        raise SystemExit(f"updates mode must be one of {', '.join(MODES)}")
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    tmp = h / "updates.json.tmp"
    tmp.write_text(json.dumps({"mode": new}) + "\n")
    tmp.replace(h / "updates.json")
    ensure_timer(h, unit_dir, systemctl)
    return {"mode": new, "chosen": True}


def apply(home: Path | None = None, base: str | None = None, runner=subprocess.run, force: bool = False) -> dict:
    """Start the update now: the installer's own detached, agent-safe rerun. Progress: `os.sh | bash -s status`.

    `force` is the only way to rerun the installer on an unpinned box (it then moves onto stable pins)."""
    st = status(home, base, fresh=True)
    if not st["checked"]:
        return {**st, "started": False, "error": "could not read the current stable release; try again later"}
    if st["installing"]:
        return {**st, "started": False, "note": "an install or update is already running"}
    if st["unpinned"] and not force:
        return {**st, "started": False,
                "error": "this box is unpinned (updated by hand); `wam updates --apply --force` moves it to stable"}
    if not st["update_available"] and not (st["unpinned"] and force):
        return {**st, "started": False}
    r = runner(["bash", "-c", f"curl -fsSL {INSTALLER} | bash -s start"], capture_output=True, text=True, timeout=600)
    ok = r.returncode == 0
    return {**st, "started": ok, **({} if ok else {"error": (r.stderr or r.stdout or "").strip()[-300:]})}


def busy(service: str | None, timeout: float = 5) -> bool | None:
    """True while the Agent Service runs or queues a chat run (its /wizard/health queue), None when unknown.

    Recording happens on the owner's device and reaches the box only as a finished upload, so the box has no
    "recording" signal; a sent recording becomes a queued/active run and is covered here."""
    if not service:
        return None
    try:
        with urllib.request.urlopen(f"{service.rstrip('/')}/wizard/health", timeout=timeout) as r:
            q = (json.loads(r.read(65536) or b"{}") or {}).get("queue")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(q, dict):
        return None
    try:
        return int(q.get("active") or 0) > 0 or int(q.get("queued") or 0) > 0
    except (TypeError, ValueError):
        return None


def service_origin(home: Path | None = None) -> str | None:
    """Loopback origin of this box's Agent Service (alongside.json service port, else the registry health URL)."""
    h = _home(home)
    try:
        port = int(((json.loads((h / "alongside.json").read_text()).get("service") or {}).get("port")) or 0)
        if port:
            return f"http://127.0.0.1:{port}"
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        for a in json.loads((h / "apps.json").read_text()).get("apps") or []:
            health = str(a.get("health") or "")
            if a.get("id") == "wizard-agent-service" and health.startswith("http://127.0.0.1:"):
                return health.rsplit("/wizard/health", 1)[0]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def apply_if_auto(home: Path | None = None, base: str | None = None, runner=subprocess.run, is_busy=None) -> dict:
    """The nightly timer: apply only in Automatic, never on an unpinned box, and not while the agent is working
    (skipped; the next nightly window tries again)."""
    if mode(home) != "auto":       # the timer outlived a switch back to Ask me: do nothing
        return {"mode": mode(home), "started": False}
    st = status(home, base, fresh=True)
    if not st["update_available"]:
        return {**st, "started": False}
    if (is_busy or (lambda: busy(service_origin(home))))():
        return {**st, "started": False, "skipped": "busy", "note": "a chat run is active; trying again next window"}
    return apply(home, base, runner)
