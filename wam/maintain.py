"""System maintenance on every Wizard OS box: one nightly run the platform owns, not an agent cron job.

    wam maintain                      status: the owner's settings, the last run, what is pending
    wam maintain run [--wait-quiet S] the nightly run (the timer calls this)
    wam maintain setup                install the fixed root helper for OS patches (installer; needs sudo once)
    wam maintain --os-security off|check|patch
    wam maintain history [-n N]

Slice 1 owns OS security patches (wam/os_patch.py: any Linux, through its own package manager and security channel)
and the run's history, written for Home > Resources > Health. Backups (`wam backup`), Wizard OS / Hermes / app
updates (`wam updates`, `wam hermes`, `wam app update`) join this same run, lock and history in later slices.

OS security setting (~/.wizard-os/maintenance.json, kept across reinstalls; the installer never writes it):
- "check" (default): look every night and show what is pending in Health. Changes nothing.
- "patch": also install pending security fixes, only while the box is quiet, and never restart a service or reboot.
  On Debian/Ubuntu it pauses the distribution's own unattended-upgrades (which may restart services through
  needrestart) and does the same security patching itself without restarts; "check"/"off" hand it back.
- "off": no OS check at all.
Automatic service restarts and reboots are not offered yet (James, 23 Oct 2026: not until they are proven to never
break running work and always come back online). Held platform packages and pending reboots are reported.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import os_patch

MODES = ("off", "check", "patch")
DEFAULT_MODE = "check"
HELPER = Path("/usr/local/libexec/wizard-os/os-patch")
SUDOERS = Path("/etc/sudoers.d/wizard-os-maintenance")
UNIT = "wizard-os-maintain"
HISTORY_KEEP = 400
STALE_SECONDS = 3 * 86400


def _home(home: Path | None = None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def _dir(home: Path | None = None) -> Path:
    return _home(home) / "maintenance"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _save(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(doc, out, indent=2)
            out.write("\n"); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def settings(home: Path | None = None) -> dict:
    s = _load(_home(home) / "maintenance.json")
    if s.get("os_security") not in MODES:
        s["os_security"] = DEFAULT_MODE
    return s


# ---- root helper -------------------------------------------------------------------------------------------------

def _source() -> Path:
    return Path(os_patch.__file__).resolve()


def _sha(p: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def helper_state() -> str:
    """root: running as root; ready: the installed helper matches this wam; stale: an older helper; missing."""
    if os.geteuid() == 0:
        return "root"
    if not HELPER.exists():
        return "missing"
    return "ready" if _sha(HELPER) == _sha(_source()) else "stale"


def _helper(action: list[str]) -> list[str] | None:
    st = helper_state()
    if st == "root":
        return [sys.executable, str(_source()), *action]
    if st in ("ready", "stale"):
        return ["sudo", "-n", str(HELPER), *action]
    return None


def call_helper(action: list[str], runner=subprocess.run, timeout: int = 4000) -> dict:
    argv = _helper(action)
    if argv is None:
        return {"state": "refused", "reason": "The OS patch helper is not installed (wam maintain setup)."}
    try:
        p = runner(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "error", "reason": f"OS patch helper failed to run: {exc}"}
    try:
        doc = json.loads(p.stdout)
        if isinstance(doc, dict):
            return doc
    except ValueError:
        pass
    return {"state": "error", "reason": f"OS patch helper exited {p.returncode}: {(p.stderr or p.stdout).strip()[-400:]}"}


def setup(runner=subprocess.run, user: str | None = None) -> dict:
    """Install or refresh the root-owned helper plus one sudoers line that allows only it. Needs passwordless sudo
    (fresh boxes have it from get.sh) or root; otherwise reports the one command an owner would run."""
    root = os.geteuid() == 0
    user = user or (os.environ.get("SUDO_USER") if root else None) or (None if root else (os.environ.get("USER") or
            subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()))
    if root and not user:
        return {"helper": "root", "changed": False}   # a root-run box calls os_patch directly
    if _sha(HELPER) == _sha(_source()) and (SUDOERS.exists() or not root):
        return {"helper": "ready", "changed": False}
    rule = f"{user} ALL=(root) NOPASSWD: {HELPER} check, {HELPER} check --refresh, {HELPER} apply, {HELPER} native-off, {HELPER} native-on\n"
    with tempfile.TemporaryDirectory() as td:
        sud = Path(td) / "sudoers"
        sud.write_text(rule)
        sud.chmod(0o644)
        script = (f"set -e; install -d -o root -g root -m 0755 {HELPER.parent}; "
                  f"install -o root -g root -m 0755 {_source()} {HELPER}; "
                  f"visudo -cqf {sud}; install -o root -g root -m 0440 {sud} {SUDOERS}")
        p = runner(["sh", "-c", script] if root else ["sudo", "-n", "sh", "-c", script],
                   capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return {"helper": helper_state(), "changed": False, "state": "needs_root_once",
                "reason": (p.stderr or p.stdout).strip()[-300:] or "sudo needs a password",
                "command": f"sudo {shutil.which('wam') or 'wam'} maintain setup"}
    return {"helper": "ready" if _sha(HELPER) == _sha(_source()) else helper_state(), "changed": True, "user": user}


# ---- settings, timer ---------------------------------------------------------------------------------------------

def _timer_files(unit_dir: Path, wam: str) -> dict[Path, str]:
    return {
        unit_dir / f"{UNIT}.service": (
            "[Unit]\nDescription=Wizard OS nightly maintenance (wam maintain run)\n\n"
            "[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n"
            "Nice=10\nIOSchedulingClass=idle\nTimeoutStartSec=3h\n"
            f"ExecStart={wam} maintain run\n"),
        unit_dir / f"{UNIT}.timer": (
            "[Unit]\nDescription=Wizard OS nightly maintenance\n\n"
            # after the 00:00 backup, before the 03:30 Wizard OS update window (box-local time)
            "[Timer]\nOnCalendar=*-*-* 02:00\nRandomizedDelaySec=45m\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def ensure_timer(home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    on = settings(home)["os_security"] != "off"
    ud = unit_dir or Path.home() / ".config/systemd/user"
    files = _timer_files(ud, str(Path.home() / ".local/bin/wam"))
    run = (lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)) if systemctl \
        else (lambda *a: None)
    if on:
        ud.mkdir(parents=True, exist_ok=True)
        for p, text in files.items():
            if not p.exists() or p.read_text() != text:
                p.write_text(text)
        run("daemon-reload"); run("enable", "--now", f"{UNIT}.timer")
    else:
        run("disable", "--now", f"{UNIT}.timer")
        for p in files:
            p.unlink(missing_ok=True)
        run("daemon-reload")
    return {"timer": on}


def set_os_security(mode: str, home: Path | None = None, runner=subprocess.run, unit_dir: Path | None = None,
                    systemctl: bool = True) -> dict:
    if mode not in MODES:
        return {"error": f"os-security must be one of {', '.join(MODES)}"}
    native = call_helper(["native-off" if mode == "patch" else "native-on"], runner)
    if mode == "patch" and native.get("state") in ("refused", "error"):
        return {"error": "Patching needs the OS patch helper first.", "helper": native}
    s = settings(home)
    s.update(os_security=mode, changed_at=_now())
    _save(_home(home) / "maintenance.json", s)
    return {"os_security": mode, "native": native, **ensure_timer(home, unit_dir, systemctl)}


# ---- the run -----------------------------------------------------------------------------------------------------

@contextmanager
def _lease(home: Path):
    """The box's one maintenance lease. Also refuses while a Hermes update holds its own lock."""
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "maintenance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    other = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (home / "hermes-update.lock").exists():
            other = os.open(home / "hermes-update.lock", os.O_RDWR)
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if other is not None:
            os.close(other)
        os.close(fd)   # never unlink a flock inode


def busy_reasons(home: Path) -> list[str]:
    from . import hermes_update
    hh = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    try:
        al = _load(home / "alongside.json")
        hh = Path(al.get("hermes_home") or hh).expanduser()
    except (TypeError, ValueError):
        pass
    return hermes_update.busy(home, hh)


def wait_quiet(home: Path, seconds: float, is_busy=None, sleep=time.sleep, clock=time.monotonic) -> list[str]:
    """[] once the box is quiet twice in a row (closing the check-then-act race), else the last busy reasons."""
    probe = is_busy or (lambda: busy_reasons(home))
    deadline, quiet = clock() + seconds, 0
    while True:
        reasons = probe()
        quiet = 0 if reasons else quiet + 1
        if quiet >= 2:
            return []
        if reasons and clock() >= deadline:
            return reasons
        sleep(min(60, max(1, deadline - clock())) if reasons else 5)   # a quiet probe is always confirmed once more


def os_step(mode: str, home: Path, runner=subprocess.run, wait: float = 1800, is_busy=None, sleep=time.sleep) -> dict:
    if mode == "off":
        return {"state": "off"}
    if mode == "check":
        if _helper(["check"]) is not None:
            return call_helper(["check", "--refresh"], runner)
        return os_patch.check(refresh=False)          # unprivileged: the system's own nightly list refresh
    reasons = wait_quiet(home, wait, is_busy, sleep)
    if reasons:
        return {"state": "deferred", "reason": " ".join(reasons) + " Trying again next night; nothing was changed."}
    return call_helper(["apply"], runner)


def run(home: Path | None = None, runner=subprocess.run, wait: float = 1800, is_busy=None, sleep=time.sleep) -> dict:
    h = _home(home)
    started = _now()
    try:
        with _lease(h):
            mode = settings(h)["os_security"]
            t0 = time.monotonic()
            os_res = os_step(mode, h, runner, wait, is_busy, sleep)
            rec = {"run_id": f"{started}-{os.getpid()}", "started_at": started, "finished_at": _now(),
                   "seconds": round(time.monotonic() - t0, 1), "os_security": mode, "os": os_res,
                   "state": _overall(os_res)}
    except BlockingIOError:
        rec = {"run_id": f"{started}-{os.getpid()}", "started_at": started, "finished_at": _now(), "state": "busy",
               "reason": "Another maintenance task holds the box's maintenance lease; nothing was changed."}
    _record(h, rec)
    return rec


def _overall(os_res: dict) -> str:
    st = os_res.get("state")
    return {"applied": "ok", "current": "ok", "pending": "ok", "off": "ok", "held": "ok", "report_only": "ok",
            "deferred": "deferred"}.get(str(st), "error")


def _record(h: Path, rec: dict) -> None:
    d = _dir(h)
    d.mkdir(parents=True, exist_ok=True)
    hist = d / "history.jsonl"
    try:
        lines = hist.read_text().splitlines()[-(HISTORY_KEEP - 1):]
    except OSError:
        lines = []
    lines.append(json.dumps(rec, separators=(",", ":")))
    tmp = hist.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, hist)
    _save(d / "last.json", rec)
    _save(d / "health.json", health(h, rec))


def history(home: Path | None = None, n: int = 20) -> list[dict]:
    try:
        lines = (_dir(home) / "history.jsonl").read_text().splitlines()
    except OSError:
        return []
    out = []
    for l in lines[-n:]:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


# ---- Health ------------------------------------------------------------------------------------------------------

def health(home: Path | None = None, last: dict | None = None, now: float | None = None) -> dict:
    """Home > Resources > Health rows (wizard-ux tools/system-health.mjs shape)."""
    last = last if last is not None else _load(_dir(home) / "last.json")
    mode = settings(home)["os_security"]
    items = []
    if mode != "off":
        items.append(_os_item(mode, last, time.time() if now is None else now))
    return {"generated_at": _now(), "items": items}


def _os_item(mode: str, last: dict, now: float) -> dict:
    item = {"key": "os-security", "label": "Security updates"}
    if not last:
        return {**item, "status": "warn", "value": "not checked yet", "detail": "The first nightly check has not run."}
    try:
        age = now - calendar.timegm(time.strptime(last.get("finished_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        age = STALE_SECONDS + 1
    if age > STALE_SECONDS:
        return {**item, "status": "error", "value": "not running", "detail": "No maintenance run in over 3 days."}
    o = last.get("os") or {}
    sec, held = o.get("security") or [], o.get("held") or []
    reboot = " A restart is needed to finish." if o.get("reboot_required") else ""
    st = o.get("state")
    if last.get("state") == "error" or st in ("error", "refused"):
        return {**item, "status": "error", "value": "needs attention", "detail": str(o.get("reason") or last.get("reason"))[:300]}
    if st == "unsupported":
        return {**item, "status": "warn", "value": "not supported", "detail": o.get("reason", "")}
    if st == "deferred":
        return {**item, "status": "warn", "value": "waiting for quiet", "detail": o.get("reason", "")[:300]}
    if st == "applied":
        n = len(o.get("installed") or [])
        return {**item, "status": "warn" if (reboot or held) else "ok", "value": f"{n} installed",
                "detail": (f"{len(held)} platform package(s) wait for a quiet restart." if held else "") + reboot}
    if sec:
        how = "Turn on automatic patching to install them." if mode == "check" else "Installing tonight."
        return {**item, "status": "warn", "value": f"{len(sec)} pending", "detail": f"{how}{reboot}"}
    if held:
        return {**item, "status": "warn", "value": f"{len(held)} held", "detail": "Platform packages wait for a quiet restart." + reboot}
    return {**item, "status": "warn" if reboot else "ok", "value": "up to date", "detail": reboot.strip() or o.get("distro", "")}


def status(home: Path | None = None) -> dict:
    s = settings(home)
    return {"os_security": s["os_security"], "helper": helper_state(), "last": _load(_dir(home) / "last.json") or None,
            "health": health(home)["items"]}


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wam maintain", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="status", choices=("status", "run", "setup", "history", "health"))
    ap.add_argument("--os-security", choices=MODES)
    ap.add_argument("--wait-quiet", type=float, default=1800)
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--ensure-timer", action="store_true", help="installer: make the nightly timer match the setting")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.os_security:
        res = set_os_security(a.os_security)
    elif a.ensure_timer:
        res = ensure_timer()
    elif a.action == "run":
        res = run(wait=a.wait_quiet)
    elif a.action == "setup":
        res = setup()
    elif a.action == "history":
        res = history(n=a.n)
    elif a.action == "health":
        res = health()
    else:
        res = status()
    print(json.dumps(res, indent=2))
    return 1 if isinstance(res, dict) and (res.get("error") or res.get("state") in ("error", "needs_root_once")) else 0
