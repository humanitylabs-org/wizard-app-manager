"""System maintenance on every WizardOS box: one nightly run the platform owns, not an agent cron job.

    wam maintain                      status: the owner's settings, the last run, what is pending
    wam maintain run [--wait-quiet S] the nightly run (the timer calls this)
    wam maintain setup                install the fixed root helper for OS patches (installer; needs sudo once)
    wam maintain --os-security off|check|patch
    wam maintain history [-n N]

One run, in order: (1) backups: `wam backup` keeps its own schedule and lock; this run catches up a missed backup
and reports whether the last one succeeded and was read back; (2) OS security patches (wam/os_patch.py: any Linux,
through its own package manager and security channel); (3) the owner's Automatic WizardOS update (stable releases
only; this timer replaces the separate 03:30 one). Hermes updates stay explicit (`wam hermes update`). History goes
to ~/.wizard-os/maintenance and Home > Resources > Health.

OS security setting (~/.wizard-os/maintenance.json, kept across reinstalls; the installer never writes it):
- "check" (default): look every night and show what is pending in Health. Changes nothing.
- "patch": also install pending security fixes, only while the box is quiet, and never restart a service or reboot.
  On Debian/Ubuntu it pauses the distribution's own unattended-upgrades (which may restart services through
  needrestart) and does the same security patching itself without restarts; "check"/"off" hand it back.
- "off": no OS check at all.
Quiet restart lane (all default off / report-only; no installer changes these settings):
  --quiet-restarts off|auto  restart only needrestart-selected active systemd services, while quiet
  --held-platform off|patch  apt held security packages, only with os-security patch, while quiet
  --quiet-reboot off|auto    reboot-required only, while quiet; intent survives reboot and Health reconciles it
Attended acceptance is required before enabling on an owner box. Busy probes are not an atomic admission fence.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import stat
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
    from .quiet_restart import OPTIONS
    for key, choices in OPTIONS.items():
        if s.get(key) not in choices:
            s[key] = "off"
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


def _protected(path: Path, mode: int | None = None) -> bool:
    """Only root-owned regular bytes below root-owned, nonwritable, nonlinked directories are trusted."""
    try:
        for parent in reversed(path.parents):
            s = parent.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
                return False
        s = path.lstat()
        return stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022 \
            and (mode is None or stat.S_IMODE(s.st_mode) == mode)
    except OSError:
        return False


def helper_state() -> str:
    """root: running as root; ready: the installed helper matches this wam; stale: an older helper; missing."""
    if os.geteuid() == 0:
        return "root"
    if not HELPER.exists():
        return "missing"
    if not _protected(HELPER, 0o755) or not _protected(SUDOERS, 0o440):
        return "unsafe"
    return "ready" if _sha(HELPER) == _sha(_source()) else "stale"


def _install_paths_safe() -> bool:
    """Provision only through an existing protected root ancestry; never follow a destination link."""
    for path in (HELPER, SUDOERS):
        for parent in path.parents:
            try:
                s = parent.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
                return False
        if path.is_symlink() or (path.exists() and not _protected(path)):
            return False
    return True


def _helper(action: list[str]) -> list[str] | None:
    st = helper_state()
    if st == "root":
        return [sys.executable, "-I", str(_source()), *action]
    if st == "ready":
        return ["sudo", "-n", str(HELPER), *action]
    return None


def call_helper(action: list[str], runner=subprocess.run, timeout: int = 4000) -> dict:
    argv = _helper(action)
    if argv is None:
        return {"state": "refused", "reason": "The OS patch helper is not installed (wam maintain setup)."}
    try:
        p = runner(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"state": "error", "helper_may_be_running": True,
                "reason": "Root helper timed out; it may still be running. Further mutations are deferred."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "error", "reason": f"OS patch helper failed to run: {exc}"}
    try:
        doc = json.loads(p.stdout)
        if isinstance(doc, dict):
            if doc.get("state") == "busy":
                doc["state"] = "deferred"
            return doc
    except ValueError:
        pass
    return {"state": "error", "reason": f"OS patch helper exited {p.returncode}: {(p.stderr or p.stdout).strip()[-400:]}"}


LANE = Path("/etc/sudoers.d/wizard-os-maintenance-restart-lane")
def lane_ready() -> bool:
    return _protected(LANE, 0o440)


def _owner_home() -> Path:
    """Under sudo, owner choices stay in the invoking account's Wizard home, never /root."""
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user:
        try:
            return Path(pwd.getpwnam(user).pw_dir) / ".wizard-os"
        except KeyError:
            pass
    return _home(None)


def _owner_settings(update=None) -> dict | None:
    """Read (and optionally update) the owner's settings as that account, never as root.

    Returns the settings, or None when the owner-account write/read could not be completed.
    """
    path = _owner_home() / "maintenance.json"
    user = os.environ.get("SUDO_USER")

    def work() -> dict:
        cfg = _load(path)
        if update is not None:
            update(cfg)
            _save(path, cfg)
        return cfg

    if os.geteuid() != 0 or not user:
        return work()
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        return None
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(r)
            os.initgroups(user, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            os.write(w, json.dumps(work()).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    os.close(r)
    if os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) != 0:
        return None
    try:
        return json.loads(data or b"{}")
    except ValueError:
        return None


def setup(runner=subprocess.run, user: str | None = None, lane: bool = False, installer: bool = False) -> dict:
    """Install or refresh the root-owned helper plus one sudoers line that allows only it. Needs passwordless sudo
    (fresh boxes have it from get.sh) or root; otherwise reports the one command an owner would run."""
    root = os.geteuid() == 0
    owner = _owner_settings()
    if owner is None:
        return {"helper": helper_state(), "changed": False, "state": "error",
                "reason": "Could not read the owner's maintenance choices."}
    if installer and owner.get("helper_removed"):
        return {"helper": "removed", "changed": False, "state": "removed_by_owner",
                "reason": "The owner removed the root helper; rerun 'wam maintain setup' to opt in again."}
    user = user or (os.environ.get("SUDO_USER") if root else None) or (None if root else (os.environ.get("USER") or
            subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()))
    if root and not user:
        return {"helper": "root", "changed": False}   # a root-run box calls os_patch directly
    if not isinstance(user, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*[$]?", user):
        return {"state": "refused", "reason": "A local account name is required.", "changed": False}
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        return {"state": "refused", "reason": "The local account does not exist.", "changed": False}
    if not root and account.pw_uid != os.getuid():
        return {"state": "refused", "reason": "Setup may grant only the invoking account.", "changed": False}
    if not _install_paths_safe():
        return {"state": "refused", "reason": "Helper or sudoers destination has unsafe ancestry.", "changed": False}
    rule = (f"{user} ALL=(root) NOPASSWD: {HELPER} check, {HELPER} check --refresh, {HELPER} apply, "
            f"{HELPER} native-off, {HELPER} native-on, {HELPER} restart-check, {HELPER} remove\n")
    lane_rule = (f"{user} ALL=(root) NOPASSWD: {HELPER} restart-service *, {HELPER} apply-held, {HELPER} reboot\n")
    with tempfile.TemporaryDirectory() as td:
        sud = Path(td) / "sudoers"
        sud.write_text(rule)
        sud.chmod(0o644)
        lane_file = Path(td) / "lane"
        lane_file.write_text(lane_rule)
        lane_file.chmod(0o644)
        hp, sp = shlex.quote(str(HELPER.parent)), shlex.quote(str(SUDOERS.parent))
        lane_script = ""
        if lane:
            lane_script = (f"l=$(/usr/bin/mktemp {sp}/.wizard-os-lane.XXXXXX); trap '/usr/bin/rm -f -- \"$h\" \"$s\" \"$l\"' EXIT; "
                           f"/usr/bin/install -o root -g root -m 0440 {shlex.quote(str(lane_file))} \"$l\"; "
                           "/usr/sbin/visudo -cqf \"$l\"; ")
        script = (f"set -e; /usr/bin/install -d -o root -g root -m 0755 {hp}; "
                  f"h=$(/usr/bin/mktemp {hp}/.os-patch.XXXXXX); "
                  f"s=$(/usr/bin/mktemp {sp}/.wizard-os-maintenance.XXXXXX); "
                  "trap '/usr/bin/rm -f -- \"$h\" \"$s\"' EXIT; "
                  + lane_script +
                  f"/usr/bin/install -o root -g root -m 0755 {shlex.quote(str(_source()))} \"$h\"; "
                  f"/usr/bin/install -o root -g root -m 0440 {shlex.quote(str(sud))} \"$s\"; "
                  "/usr/sbin/visudo -cqf \"$s\"; "
                  f"/usr/bin/mv -fT -- \"$h\" {shlex.quote(str(HELPER))}; "
                  f"/usr/bin/mv -fT -- \"$s\" {shlex.quote(str(SUDOERS))}"
                  + (f"; /usr/bin/mv -fT -- \"$l\" {shlex.quote(str(LANE))}" if lane else ""))
        p = runner(["/bin/sh", "-c", script] if root else ["sudo", "-n", "/bin/sh", "-c", script],
                   capture_output=True, text=True, timeout=120,
                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C"})
    if p.returncode != 0:
        return {"helper": helper_state(), "changed": False, "state": "needs_root_once",
                "reason": (p.stderr or p.stdout).strip()[-300:] or "sudo needs a password",
                "command": f"sudo {shutil.which('wam') or 'wam'} maintain setup"}
    state = helper_state()
    if state not in ("ready", "root"):
        failure = {"state": "error", "reason": "Installed helper failed provenance verification."}
    elif _owner_settings(lambda cfg: cfg.pop("helper_removed", None)) is None:
        # Without this the old removal choice stays on disk and installer reruns skip every future refresh.
        failure = {"state": "error", "reason": "Helper installed, but the owner's opt-in could not be recorded; "
                                               "installer reruns will keep skipping it."}
    else:
        failure = {}
    return {"helper": state, "changed": True, "user": user, **failure}


def remove(runner=subprocess.run) -> dict:
    """Undo `setup`: delete the sudoers rule and the root helper. OS security then stays check-only (unprivileged);
    `wam maintain setup` puts them back. For a box whose owner never wants root maintenance installed."""
    if not SUDOERS.exists() and not HELPER.exists() and not LANE.exists():
        ok = _owner_settings(lambda cfg: cfg.update(helper_removed=True, os_security="check", quiet_restarts="off",
                                                    held_platform="off", quiet_reboot="off")) is not None
        return {"helper": "missing", "changed": False, **({} if ok else {"state": "error", "reason": "Could not record the removal choice."})}
    argv = _helper(["remove"])
    if argv is None:
        return {"helper": helper_state(), "changed": False, "state": "needs_root_once",
                "command": "sudo wam maintain remove"}
    p = runner(argv,
               capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        try:
            busy = json.loads(p.stdout or "{}")
        except ValueError:
            busy = {}
        if isinstance(busy, dict) and busy.get("state") == "busy":
            return {"helper": helper_state(), "changed": False, "state": "deferred", "reason": busy.get("reason", "Helper busy.")}
        return {"helper": helper_state(), "changed": False, "state": "needs_root_once",
                "reason": (p.stderr or p.stdout).strip()[-300:] or "sudo needs a password",
                "command": f"sudo {shutil.which('wam') or 'wam'} maintain remove"}
    gone = not SUDOERS.exists() and not HELPER.exists() and not LANE.exists()
    if gone:
        ok = _owner_settings(lambda cfg: cfg.update(helper_removed=True, changed_at=_now(), os_security="check",
                                                    quiet_restarts="off", held_platform="off", quiet_reboot="off")) is not None
        if not ok:
            return {"helper": "missing", "changed": True, "state": "error",
                    "reason": "Helper removed, but the owner's removal choice could not be recorded; do not rerun the installer until it is."}
    return {"helper": "missing" if gone else helper_state(), "changed": gone,
            **({} if gone else {"state": "error", "reason": "Helper removal did not verify."})}


# ---- settings, timer ---------------------------------------------------------------------------------------------

def _timer_files(unit_dir: Path, wam: str) -> dict[Path, str]:
    return {
        unit_dir / f"{UNIT}.service": (
            "[Unit]\nDescription=WizardOS nightly maintenance (wam maintain run)\n\n"
            "[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n"
            "Nice=10\nIOSchedulingClass=idle\nTimeoutStartSec=3h\n"
            f"ExecStart={wam} maintain run\n"),
        unit_dir / f"{UNIT}.timer": (
            "[Unit]\nDescription=WizardOS nightly maintenance\n\n"
            # after the 00:00 backup, before the 03:30 WizardOS update window (box-local time)
            "[Timer]\nOnCalendar=*-*-* 02:00\nRandomizedDelaySec=45m\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def ensure_timer(home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    """The nightly maintenance timer is always on: it owns the box's nightly work (backup check, OS security, the
    Automatic WizardOS update). The separate WizardOS update timer is retired once this one exists; `wam updates`
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
    if native.get("state") in ("refused", "error", "needs_root_once", "busy", "deferred") and (
            mode == "patch" or settings(home)["os_security"] == "patch"):
        return {"error": "Cannot safely change the native updater until the root helper is repaired.", "helper": native}
    s = settings(home)
    s.update(os_security=mode, changed_at=_now())
    _save(_home(home) / "maintenance.json", s)
    return {"os_security": mode, "native": native, **ensure_timer(home, unit_dir, systemctl)}


# ---- the run -----------------------------------------------------------------------------------------------------

@contextmanager
def _lease(home: Path):
    """The box's one maintenance lease. Also refuses while a Hermes update or the nightly app updates
    (wam/app_updates.py, 04:30 timer; both timers are Persistent, so they can fire together after a box was off) hold
    their own lock, and holds both locks for the whole run so neither starts underneath OS patching or a restart."""
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "maintenance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    others: list[int] = []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in ("hermes-update.lock", "app-updates.lock", "install.start.lock", "install.lock"):
            others.append(os.open(home / name, os.O_RDWR | os.O_CREAT, 0o600))
            fcntl.flock(others[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for other in others:
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
    """[] once quiet twice; this is not an atomic admission fence against new work."""
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
    base = is_busy or (lambda: busy_reasons(home))
    probe = lambda: (["The restored copy remains paused."] if restore_paused(home) else []) + list(base())
    reasons = wait_quiet(home, wait, probe, sleep)
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
    """The owner's Automatic WizardOS update (the box's channel release: promoted stable, or the newest Latest),
    formerly its own 03:30 timer.
    Ask me: nothing. Hermes updates stay explicit (`wam hermes update`, never automatic)."""
    from . import updates
    updates.report_failure(home)   # a failed update tells the team in either mode, busy or not (switch-gated, #524)
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


def restore_paused(home: Path) -> bool:
    """Fail closed for an unreadable, in-progress or unactivated restore; never resume the copied jobs."""
    last = home / "restores" / "last.json"
    if not last.exists():
        return False
    stamp = _load(last).get("stamp")
    if not isinstance(stamp, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", stamp):
        return True
    journal = _load(last.parent / stamp / "journal.json")
    return journal.get("status") != "undone" and not (
        journal.get("status") == "restored" and journal.get("activated_at"))


def run(home: Path | None = None, runner=subprocess.run, wait: float = 1800, is_busy=None, sleep=time.sleep,
        with_updates: bool = True) -> dict:
    h = _home(home)
    started = _now()
    if restore_paused(h):
        rec = {"run_id": f"{started}-{os.getpid()}", "started_at": started, "finished_at": _now(), "state": "deferred",
               "reason": "The restore is paused until owner activation or successful undo."}
        _record(h, rec)
        return rec
    try:
        with _lease(h):
            from . import quiet_restart as qr
            cfg = settings(h)
            mode = cfg["os_security"]
            t0 = time.monotonic()
            bk = qr.safe_step(backup_step, h, runner)
            os_res = qr.safe_step(os_step, mode, h, runner, wait, is_busy, sleep)
            # Launch updates only after releasing the installer/app-update locks below.
            up = {"state": "skipped"}
            # Updates may launch a long installer. Never restart underneath it.
            def restart_probe():
                from . import updates
                if restore_paused(h):
                    return ["The restored copy remains paused until owner activation."]
                if os_res.get("helper_may_be_running"):
                    return ["The root helper may still be running after a timeout."]
                reasons = list((is_busy or (lambda: busy_reasons(h)))())
                if updates.installing(h):
                    reasons.append("A WizardOS installer is running.")
                return reasons
            restarts = qr.safe_step(qr.run, h, os_res, runner, wait, restart_probe, sleep)
            if restarts.get("helper_may_be_running"):
                os_res["helper_may_be_running"] = True
            rec = {"run_id": f"{started}-{os.getpid()}", "started_at": started, "finished_at": _now(),
                   "seconds": round(time.monotonic() - t0, 1), "os_security": mode, "backup": _slim_backup(bk),
                   "os": os_res, "updates": up, "restarts": restarts, "state": _overall(os_res, bk, up)}
            parts = [restarts, *(restarts.get(k) or {} for k in ("held", "services", "reboot"))]
            if any(p.get("state") in qr.BAD for p in parts):
                rec["state"] = "error"
            elif rec["state"] == "ok" and any(p.get("state") == "deferred" for p in parts):
                rec["state"] = "deferred"
        if with_updates:
            def do_updates():
                intent = _load(_dir(h) / "reboot.json")
                reboot = (rec.get("restarts") or {}).get("reboot") or {}
                if restore_paused(h) or reboot.get("state") == "scheduled" or intent.get("state") in ("intent", "scheduled") \
                        or os_res.get("helper_may_be_running"):
                    return {"state": "deferred", "reason": "Restore activation, a pending reboot or the root helper must finish before an update."}
                quiet = os_res.get("state") != "deferred" and not (is_busy or (lambda: busy_reasons(h)))()
                return updates_step(h, quiet)
            rec["updates"] = qr.safe_step(do_updates)
            if rec["updates"].get("state") == "error":
                rec["state"] = "error"
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
    tmp = hist.with_name(f"history.{os.getpid()}.tmp")
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
    cfg = settings(home)
    mode = cfg["os_security"]
    items = []
    h = _helper_removed_item(cfg)
    if h:
        items.append(h)
    b = _backup_item(backup_state(_home(home)), now)
    if b:
        items.append(b)
    if mode != "off":
        items.append(_os_item(mode, last, now))
    u = _updates_item(last)
    if u:
        items.append(u)
    if mode != "patch" and os_patch.APT_NATIVE_OFF.exists():
        items.append({"key": "native-updater", "label": "System updater", "status": "warn", "value": "paused",
                      "detail": "Wizard paused the system's own security updater, but Wizard patching is not on."})
    from . import quiet_restart
    items.extend(quiet_restart.items(_home(home), last))
    return {"generated_at": _now(), "items": items}


def _helper_removed_item(cfg: dict) -> dict | None:
    """`helper_removed` travels in maintenance.json, but the helper is per machine (wizard-os spec/BOX_STATE.md).
    A restore onto a host whose installer already put the helper in place leaves the flag saying "removed" while a
    helper is installed here, so later installer runs stop refreshing it and patching stops after the next helper
    change. Never act as root on it; say so, and let the owner pick on this box."""
    if not cfg.get("helper_removed") or not (HELPER.exists() or HELPER.is_symlink()):
        return None
    return {"key": "maintenance-helper", "label": "Maintenance helper", "status": "warn", "value": "needs a choice",
            "detail": ("The root maintenance helper was removed earlier (maybe on the box this one was restored "
                       "from), but one is installed here, and updates no longer refresh it. Run 'wam maintain setup' "
                       "to keep it, or 'wam maintain remove' to remove it here too.")}


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
    item = {"key": "wizard-updates", "label": "WizardOS updates", "quiet": True}
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
    if not o and last.get("state") in ("busy", "deferred"):
        return {**item, "status": "warn", "value": "waiting", "detail": str(last.get("reason") or "Maintenance was deferred.")[:300]}
    sec, held = o.get("security") or [], o.get("held") or []
    reboot = " A restart is needed to finish." if o.get("reboot_required") else ""
    st = o.get("state")
    if st in ("error", "refused") or (not o and last.get("state") == "error"):
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


def set_restart_option(key: str, value: str, home: Path | None = None) -> dict:
    from .quiet_restart import OPTIONS
    if key not in OPTIONS or value not in OPTIONS[key]:
        return {"error": "Invalid quiet-restart setting."}
    if value != OPTIONS[key][0] and not lane_ready():
        return {"error": "Run an attended 'sudo wam maintain setup --restart-lane' before enabling this opt-in."}
    cfg = settings(home)
    cfg.update({key: value, "changed_at": _now()})
    _save(_home(home) / "maintenance.json", cfg)
    return {key: value, "note": "Opt-in automation; attended acceptance is required before enabling on an owner box."}


def status(home: Path | None = None) -> dict:
    s = settings(home)
    return {"settings": s, "os_security": s["os_security"], "helper": helper_state(), "last": _load(_dir(home) / "last.json") or None,
            "health": health(home)["items"]}


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wam maintain", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="status", choices=("status", "run", "setup", "remove", "history", "health"))
    ap.add_argument("--restart-lane", action="store_true", help="setup: attended opt-in sudoers for restart/reboot/held actions")
    ap.add_argument("--installer", action="store_true", help="setup: installer rerun; honours an owner's removal")
    ap.add_argument("--os-security", choices=MODES)
    from .quiet_restart import OPTIONS
    for key, choices in OPTIONS.items():
        ap.add_argument("--" + key.replace("_", "-"), choices=choices, help="Opt-in; default off (report only)")
    ap.add_argument("--wait-quiet", type=float, default=1800)
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--ensure-timer", action="store_true", help="installer: make the nightly timer match the setting")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    chosen = [(key, getattr(a, key)) for key in OPTIONS if getattr(a, key) is not None]
    if len(chosen) + bool(a.os_security) > 1:
        ap.error("Change one maintenance setting per command.")
    if a.wait_quiet < 0:
        ap.error("--wait-quiet must not be negative")
    if chosen:
        res = set_restart_option(*chosen[0])
    elif a.os_security:
        res = set_os_security(a.os_security)
    elif a.ensure_timer:
        res = ensure_timer()
    elif a.action == "run":
        res = run(wait=a.wait_quiet)
    elif a.action == "setup":
        res = setup(lane=a.restart_lane, installer=a.installer)
    elif a.action == "remove":
        res = remove()
    elif a.action == "history":
        res = history(n=a.n)
    elif a.action == "health":
        res = health()
    else:
        res = status()
    print(json.dumps(res, indent=2))
    return 1 if isinstance(res, dict) and (res.get("error") or res.get("state") in ("error", "refused", "needs_root_once")) else 0
