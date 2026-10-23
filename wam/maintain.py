"""System maintenance on every Wizard OS box: one nightly run the platform owns, not an agent cron job.

    wam maintain                      status: the owner's settings, the last run, what is pending
    wam maintain run [--wait-quiet S] the nightly run (the timer calls this)
    wam maintain setup                install the fixed root helper for OS patches (installer; needs sudo once)
    wam maintain --os-security off|check|patch
    wam maintain history [-n N]

One run, in order: (1) backups: `wam backup` keeps its own schedule and lock; this run catches up a missed backup
and reports whether the last one succeeded and was read back; (2) OS security patches (wam/os_patch.py: any Linux,
through its own package manager and security channel); (3) the owner's Automatic Wizard OS update (stable releases
only; this timer replaces the separate 03:30 one). Hermes updates stay explicit (`wam hermes update`). History goes
to ~/.wizard-os/maintenance and Home > Resources > Health.

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
    """The nightly maintenance timer is always on: it owns the box's nightly work (backup check, OS security, the
    Automatic Wizard OS update). The separate Wizard OS update timer is retired once this one exists; `wam updates`
    sees this timer and never reinstalls its own."""
    ud = unit_dir or Path.home() / ".config/systemd/user"
    files = _timer_files(ud, str(Path.home() / ".local/bin/wam"))
    run = (lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)) if systemctl \
        else (lambda *a: None)
    ud.mkdir(parents=True, exist_ok=True)
    for p, text in files.items():
        if not p.exists() or p.read_text() != text:
            p.write_text(text)
    retired = False
    from . import updates
    old_timer = ud / f"{updates.UNIT}.timer"
    if old_timer.exists():
        run("disable", "--now", f"{updates.UNIT}.timer")
        old_timer.unlink(missing_ok=True)
        (ud / f"{updates.UNIT}.service").unlink(missing_ok=True)
        retired = True
    run("daemon-reload"); run("enable", "--now", f"{UNIT}.timer")
    return {"timer": True, "retired_update_timer": retired}


def timer_installed(unit_dir: Path | None = None) -> bool:
    return ((unit_dir or Path.home() / ".config/systemd/user") / f"{UNIT}.timer").exists()


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


BACKUP_DUE_HOURS = 26      # a daily backup older than this was missed (box asleep, timer lost): catch it up
BACKUP_STALE_HOURS = 50   # Health: no successful backup in two days is an error
READ_BACK_DAYS = 8        # Health: the weekly read-back of backed-up data is overdue


def _age_hours(stamp, now: float) -> float | None:
    if isinstance(stamp, (int, float)):
        return (now - float(stamp)) / 3600
    try:
        return (now - calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600
    except (ValueError, OverflowError):
        return None


def backup_state(home: Path) -> dict:
    """What `wam backup` (wam/backup, its own timer and lock) last did. No engine on this wam: not installed."""
    try:
        from .backup import schedule as _sched  # noqa: F401
    except ImportError:
        return {"state": "not_installed"}
    cfg = _load(home / "backup.json")
    st = _load(home / "backup-status.json")
    if not cfg.get("target"):
        return {"state": "not_set_up"}
    if not cfg.get("enabled"):
        return {"state": "off", "status": st}
    return {"state": "on", "status": st, "every": cfg.get("every")}


def backup_step(home: Path, runner=subprocess.run, now: float | None = None) -> dict:
    """Catch up a missed scheduled backup; never a second copy of the backup schedule. The backup engine keeps its
    own lock, so a backup already running makes this a no-op."""
    now = time.time() if now is None else now
    b = backup_state(home)
    if b["state"] != "on":
        return b
    st = b.get("status") or {}
    age = _age_hours(st.get("last_success_at"), now)
    due = BACKUP_DUE_HOURS * (7 if b.get("every") == "weekly" else 1)
    if age is not None and age < due:
        return {**b, "ran": False, "age_hours": round(age, 1)}
    wam = shutil.which("wam") or str(Path.home() / ".local/bin/wam")
    try:
        p = runner([wam, "backup", "run", "--scheduled", "--json"], capture_output=True, text=True, timeout=4 * 3600)
        rc = p.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return {**b, "ran": True, "state": "error", "reason": f"catch-up backup failed to start: {exc}"}
    b = backup_state(home)
    ok = rc == 0 and (b.get("status") or {}).get("ok") is not False
    return {**b, "ran": True, "exit_code": rc, "result": "ok" if ok else "failed"}


def updates_step(home: Path, quiet: bool) -> dict:
    """The owner's Automatic Wizard OS update (stable, promoted releases only), formerly its own 03:30 timer.
    Ask me: nothing. Hermes updates stay explicit (`wam hermes update`, never automatic)."""
    from . import updates
    try:
        if updates.mode(home) != "auto":
            return {"state": "ask", "mode": "ask"}
        if not quiet:
            return {"state": "deferred", "reason": "The box was busy; the update waits for the next quiet night."}
        res = updates.apply_if_auto(home)
    except Exception as exc:   # an update check must never sink the nightly run
        return {"state": "error", "reason": f"update check failed: {exc}"[:400]}
    if res.get("error"):
        return {"state": "error", "reason": str(res["error"])[:400], "installed": res.get("installed")}
    if res.get("started"):
        return {"state": "started", "to": res.get("latest"), "from": res.get("installed")}
    return {"state": res.get("skipped") or "current", "installed": res.get("installed"), "note": res.get("note")}


def run(home: Path | None = None, runner=subprocess.run, wait: float = 1800, is_busy=None, sleep=time.sleep,
        with_updates: bool = True) -> dict:
    h = _home(home)
    started = _now()
    try:
        with _lease(h):
            mode = settings(h)["os_security"]
            t0 = time.monotonic()
            bk = backup_step(h, runner)
            os_res = os_step(mode, h, runner, wait, is_busy, sleep)
            quiet = os_res.get("state") != "deferred" and not (is_busy or (lambda: busy_reasons(h)))()
            up = updates_step(h, quiet) if with_updates else {"state": "skipped"}
            rec = {"run_id": f"{started}-{os.getpid()}", "started_at": started, "finished_at": _now(),
                   "seconds": round(time.monotonic() - t0, 1), "os_security": mode, "backup": _slim_backup(bk),
                   "os": os_res, "updates": up, "state": _overall(os_res, bk, up)}
    except BlockingIOError:
        rec = {"run_id": f"{started}-{os.getpid()}", "started_at": started, "finished_at": _now(), "state": "busy",
               "reason": "Another maintenance task holds the box's maintenance lease; nothing was changed."}
    _record(h, rec)
    return rec


def _slim_backup(b: dict) -> dict:
    st = b.get("status") or {}
    keep = ("ok", "error", "last_success_at", "finished_at", "snapshot", "read_data", "check", "repository_bytes")
    return {k: v for k, v in {**b, "status": {k: st.get(k) for k in keep if k in st} or None}.items() if v is not None}


def _overall(os_res: dict, bk: dict | None = None, up: dict | None = None) -> str:
    st = os_res.get("state")
    o = {"applied": "ok", "current": "ok", "pending": "ok", "off": "ok", "held": "ok", "report_only": "ok",
         "deferred": "deferred"}.get(str(st), "error")
    if (bk or {}).get("state") == "error" or (bk or {}).get("result") == "failed" or (up or {}).get("state") == "error":
        return "error"
    return o


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
    now = time.time() if now is None else now
    mode = settings(home)["os_security"]
    items = []
    b = _backup_item(backup_state(_home(home)), now)
    if b:
        items.append(b)
    if mode != "off":
        items.append(_os_item(mode, last, now))
    u = _updates_item(last)
    if u:
        items.append(u)
    return {"generated_at": _now(), "items": items}


def _backup_item(b: dict, now: float) -> dict | None:
    item = {"key": "backups", "label": "Backups"}
    s = b["state"]
    if s == "not_installed":
        return None
    if s == "not_set_up":
        return {**item, "status": "warn", "value": "not set up", "detail": "Nothing is backed up off this box yet."}
    if s == "off":
        return {**item, "status": "warn", "value": "off", "detail": "Scheduled backups are switched off."}
    st = b.get("status") or {}
    age = _age_hours(st.get("last_success_at"), now)
    if age is None:
        return {**item, "status": "error" if st.get("error") else "warn", "value": "no backup yet",
                "detail": str(st.get("error") or "The first scheduled backup has not finished.")[:300]}
    when = f"{int(age)}h ago" if age < 48 else f"{int(age // 24)} days ago"
    if age > BACKUP_STALE_HOURS * (7 if b.get("every") == "weekly" else 1):
        return {**item, "status": "error", "value": f"last {when}",
                "detail": str(st.get("error") or "Backups have stopped succeeding.")[:300]}
    if st.get("ok") is False:
        return {**item, "status": "warn", "value": "last run failed",
                "detail": f"Last good backup {when}. {str(st.get('error') or '')[:200]}".strip()}
    rd = st.get("read_data") or {}
    rd_age = _age_hours(rd.get("at") if isinstance(rd, dict) else None, now)
    if rd_age is None or not rd.get("ok") or rd_age > READ_BACK_DAYS * 24:
        return {**item, "status": "warn", "value": f"backed up {when}", "detail": "Not read back this week yet."}
    return {**item, "status": "ok", "value": f"verified, {when}", "detail": ""}


def _updates_item(last: dict) -> dict | None:
    u = (last or {}).get("updates") or {}
    st = u.get("state")
    item = {"key": "wizard-updates", "label": "Wizard OS updates", "quiet": True}
    if st == "error":
        return {**item, "status": "error", "value": "update failed", "detail": str(u.get("reason"))[:300]}
    if st == "started":
        return {**item, "status": "ok", "value": f"updating to {u.get('to')}", "detail": ""}
    if st in ("deferred", "unpromoted"):
        return {**item, "status": "warn", "value": "waiting", "detail": str(u.get("reason") or u.get("note") or "")[:300]}
    return None


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
