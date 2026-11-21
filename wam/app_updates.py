"""Automatic app updates: every box gets the new version of its apps soon after a publisher ships it.

A WizardOS release only moves the system apps it pins (shell, Agent Service, Hermes, release defaults). Every other
app installed from the Wizard App Index stayed on the version it was installed at until someone ran
`wam app update <id>` by hand, so publishing a fix reached new installs only. This module closes that gap:

- check: Runtipi's own update check (the index store re-reads each installed app's listing; a new commit is always a
  newer tipi_version) lists every installed app with a newer version;
- apply: updates them one at a time through the normal `app_update` path (Runtipi backup first, the same owner gate
  for new host access, networks reconciled), then checks the app really works: it is back in the state it was
  (running or stopped), its version moved, no container is restarting or fewer containers run than before, and its
  declared health URL or MCP endpoint answers. Anything else is rolled back from the backup the update just made,
  and that version is not retried until the publisher ships a newer one;
- a systemd user timer (wizard-app-updates.timer) runs apply when the app-update mode is "auto": nightly at the
  04:30 tick, or every hour with feature switch `wizard/app-updates-hourly` on (wizard-os#613, off by default);
- the owner is told what changed through the Notifications app when it is installed.

The app-update mode is separate from the WizardOS update mode (updates.py): system releases default to Ask me
(James, alpha), app updates default to Automatic because each one is small, backed up and rolled back on failure.
Owners can switch to "ask" (`wam app-updates --ask`) or hold single apps (`--hold <id>`).

Never touched here: the shell, the Agent Service, Hermes and anything the WizardOS release pins (they move with the
release), apps prepared from an exact owner-local selection (.wizard-selection), and versions the owner ignored.

State lives in <wizard-os home>/app-updates.json: {"mode", "holds", "failed": {id: tipi}, "last": {...},
"nightly_at": <epoch of the last nightly tick that ran>}.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MODES = ("auto", "ask")
DEFAULT_MODE = "auto"
UNIT = "wizard-app-updates"
STATE = "app-updates.json"
LOCK = "app-updates.lock"
STATE_LOCK = "app-updates-state.lock"   # #857: guards read-modify-writes of STATE (not the run lock above)
RUNNING = "app-updates-running.json"   # #733: the app a run is on now, so pages can say "Updating…" after a reload
SETTLE_SECONDS = 20          # an app must stay up this long after its update before it counts as working
PROBE_SECONDS = 90           # how long a declared health URL / MCP endpoint may take to answer after the update
FRESH_SLACK_MS = 120_000     # the rollback backup must be at least this recent relative to the update start
NEEDS_REMIND_SECONDS = 20 * 3600   # hourly runs repeat an unchanged "Waiting for you" about once a day
RETRY_DAYS = 7               # a version that failed is tried again after this long (a box-side fix may land meanwhile)
# Moved only by a WizardOS release (updates.py / release_updates), never by this module.
SYSTEM_APPS = frozenset({"hermes-agent", "wizard-ux", "wizard-agent-service", "wizard-shell"})


# ---------------------------------------------------------------- settings

def _home(home: Path | None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def _state(home: Path | None) -> dict:
    try:
        s = json.loads((_home(home) / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


class _StateLock:
    """Serializes read-modify-writes of app-updates.json (#857: a Store update and an app-update run can both record
    a failed version). An flock on its own file, held per call; never held while an update runs."""
    def __init__(self, home: Path | None):
        self.h = _home(home)

    def __enter__(self):
        self.h.mkdir(parents=True, exist_ok=True)
        self.f = open(self.h / STATE_LOCK, "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def _write_unlocked(h: Path, changes: dict) -> dict:
    s = {**_state(h), **changes}
    tmp = h / f"{STATE}.{os.getpid()}.{threading.get_ident()}.tmp"   # concurrent writers never share a temp file
    tmp.write_text(json.dumps(s, indent=1) + "\n")
    tmp.replace(h / STATE)
    return s


def _write(home: Path | None, **changes) -> dict:
    with _StateLock(home) as lk:
        return _write_unlocked(lk.h, changes)


def _write_failed(home: Path | None, base: dict, new: dict, **changes) -> dict:
    """Write the `failed` entries this caller changed (base -> new) onto the CURRENT state, under the state lock,
    so a run that read `failed` earlier never erases a record another writer (a Store update, #857) added since."""
    with _StateLock(home) as lk:
        cur = _failed(lk.h)
        for k in set(base) | set(new):
            if base.get(k) != new.get(k):
                if k in new:
                    cur[k] = new[k]
                else:
                    cur.pop(k, None)
        _write_unlocked(lk.h, {**changes, "failed": cur})
        return cur


def mode(home: Path | None = None) -> str:
    m = _state(home).get("mode")
    return m if m in MODES else DEFAULT_MODE


def holds(home: Path | None = None) -> list[str]:
    return sorted({str(x) for x in (_state(home).get("holds") or []) if isinstance(x, str)})


def _failed(home: Path | None) -> dict:
    """{app id: {"to": tipi version that failed, "at": epoch}}."""
    f = _state(home).get("failed")
    return {k: v for k, v in f.items() if isinstance(v, dict) and isinstance(v.get("to"), int)} \
        if isinstance(f, dict) else {}


def set_mode(new: str, home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    if new not in MODES:
        raise SystemExit(f"app-updates mode must be one of {', '.join(MODES)}")
    _write(home, mode=new)
    return {**ensure_timer(home, unit_dir, systemctl), "chosen": True}


def hold(app_id: str, on: bool = True, home: Path | None = None) -> dict:
    hs = set(holds(home))
    (hs.add if on else hs.discard)(app_id)
    _write(home, holds=sorted(hs))
    return {"holds": sorted(hs)}


# ---------------------------------------------------------------- the timer (nightly, or hourly behind a switch)

HOURLY_SWITCH = "wizard/app-updates-hourly"   # wizard-os#613: Automatic runs every hour, not only at 04:30
NIGHTLY_AT = (4, 30)                           # local time of the one run a day while the switch is off


def hourly_on(home: Path | None = None) -> bool:
    from . import feature_switches
    return feature_switches.is_on(HOURLY_SWITCH, home)


def _nightly_due(home: Path | None, now: float) -> bool:
    """Switch off: the first tick at or after today's 04:30 (local) runs, once a day, exactly like the old 04:30
    timer (Persistent: a box that was off at 04:30 runs at its next tick that day)."""
    t = time.localtime(now)
    window = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, *NIGHTLY_AT, 0, 0, 0, -1))
    if now < window:
        return False
    return float(_state(home).get("nightly_at") or 0) < window


def _timer_files(unit_dir: Path, wam: str) -> dict[Path, str]:
    return {
        unit_dir / f"{UNIT}.service": (
            "[Unit]\nDescription=Wizard automatic app updates (owner setting: app updates Automatic)\n\n"
            "[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n"
            f"TimeoutStartSec=6h\nExecStart={wam} app-updates --apply-if-auto\n"),
        unit_dir / f"{UNIT}.timer": (
            # Ticks hourly at :30 for everyone; apply_if_auto runs only the 04:30 tick unless the hourly switch is
            # on, so the switch takes effect at the next tick without rewriting this unit (wizard-os#613).
            "[Unit]\nDescription=Wizard app update check\n\n"
            "[Timer]\nOnCalendar=*-*-* *:30\nRandomizedDelaySec=10m\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def _systemctl(enabled: bool):
    if not enabled:
        return lambda *a: None
    return lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)


def ensure_timer(home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    """Automatic: the update timer is installed and enabled. Ask me: removed. Called by the installer on every run
    (through `wam updates --ensure-timer`) and whenever the mode changes."""
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
    return {"app_mode": m, "app_timer": m == "auto"}


# ---------------------------------------------------------------- check

def _release_managed() -> set[str]:
    from . import release_pins
    try:
        doc = release_pins.load() or {}
    except (SystemExit, ValueError, OSError):
        return set()
    return {str(row["app"]) for row in (doc.get("default_apps") or {}).values() if isinstance(row, dict) and row.get("app")}


def _selection_pinned(runtipi_root: Path, store: str, app_id: str) -> bool:
    return (runtipi_root / "repos" / store / "apps" / app_id / ".wizard-selection").is_file()


def _release_managed_strict() -> set[str] | None:
    """Like _release_managed, but None when a configured release file can't be read (then no pin is dropped)."""
    from . import release_pins
    try:
        doc = release_pins.load() or {}
    except (SystemExit, ValueError, OSError):
        return None
    return {str(row["app"]) for row in (doc.get("default_apps") or {}).values() if isinstance(row, dict) and row.get("app")}


def unpin_standard(runtipi_root: Path, store: str, app_id: str, managed: set[str] | None = None) -> bool:
    """Drop the pin a Standard Store Get/Update leaves behind (wizard-os#614).

    A Standard selection is always the commit the Wizard App Index lists right now; the Store writes it to
    `.wizard-selection` only so the install takes exactly that commit. Kept afterwards, the pin makes the index
    store stop asking the index for that app, so its version never rises and automatic updates never reach it.
    Once the installed copy is that same Standard commit, the pin has done its job and is removed.

    Kept: the shell, Agent Service and Hermes, apps the WizardOS release pins (release defaults), owner-private,
    link and channel selections (their installed source is not `standard`), and a pin whose commit is not the
    installed one yet (an install or update still running)."""
    if managed is None:
        managed = _release_managed_strict()
    if managed is None or app_id in SYSTEM_APPS or app_id in managed:
        return False
    pin = Path(runtipi_root) / "repos" / store / "apps" / app_id / ".wizard-selection"
    live = Path(runtipi_root) / "apps" / store / app_id
    try:
        sel = json.loads(pin.read_text())
        src = json.loads((live / ".wizard-source").read_text())
        have = (live / ".wizard-commit").read_text().strip()
    except (OSError, ValueError):
        return False
    if not isinstance(sel, dict) or not isinstance(src, dict) or src.get("source") != "standard":
        return False
    if not have or sel.get("commit") != have or sel.get("repo") != src.get("repo"):
        return False
    pin.unlink(missing_ok=True)
    print(f"{app_id}: the Store pin to {have[:12]} is no longer needed; it follows the App Index again",
          file=sys.stderr)
    return True


def unpin_standard_all(runtipi_root: Path, store: str, installed: list[dict]) -> list[str]:
    """unpin_standard for every installed app at rest: boxes pinned by an earlier Store Get/Update get their
    updates back. Only apps Runtipi reports running or stopped: during an install or update Runtipi copies the
    store folder more than once, and the pin must hold until the whole transaction is done."""
    managed = _release_managed_strict()
    if managed is None:
        return []
    at_rest = {(r.get("app") or {}).get("appName") for r in installed or []
               if (r.get("app") or {}).get("appStoreSlug") == store and (r.get("app") or {}).get("status") in ("running", "stopped")}
    out = []
    for pin in sorted((Path(runtipi_root) / "repos" / store / "apps").glob("*/.wizard-selection")):
        if pin.parent.name in at_rest and unpin_standard(runtipi_root, store, pin.parent.name, managed):
            out.append(pin.parent.name)
    return out


def _installed_unpinned(c, rt) -> list[dict]:
    """Runtipi's installed list after freeing apps a Store Get/Update pinned (wizard-os#614). When a pin went,
    ask again: that read makes the index store look at the index for the app, so its newer version shows."""
    from . import cli
    installed = rt.installed()
    try:
        if unpin_standard_all(c.runtipi_root, cli.STORE, installed):
            installed = rt.installed()
    except Exception as e:  # noqa: BLE001 - never stops the check or the updates
        print(f"WARNING: Store pins not checked: {e}", file=sys.stderr)
    return installed


def display_name(runtipi_root: Path, store: str, app_id: str) -> str:
    """The name the owner knows the app by (its store recipe's `name`, e.g. Movies), never the internal id."""
    try:
        name = json.loads((Path(runtipi_root) / "repos" / store / "apps" / app_id / "config.json").read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    return name.strip() if isinstance(name, str) and name.strip() else app_id


def candidates(installed: list[dict], *, store: str, runtipi_root: Path, home: Path | None = None,
               release_managed: set[str] | None = None) -> list[dict]:
    """Every installed app of the Wizard store with a newer version, and why it would be skipped (None = due)."""
    managed = _release_managed() if release_managed is None else release_managed
    held, failed = set(holds(home)), _failed(home)
    out = []
    for row in installed:
        app, meta = row.get("app") or {}, row.get("metadata") or {}
        app_id, slug = app.get("appName"), app.get("appStoreSlug")
        try:
            have, latest = int(app.get("version") or 0), int(meta.get("latestVersion") or 0)
        except (TypeError, ValueError):
            continue
        if not app_id or slug != store:
            continue
        if latest <= have:
            # A real pin keeps the index store from ever showing a newer version; list it anyway so
            # `wam app-updates` says why the app does not move (wizard-os#614).
            if app_id in SYSTEM_APPS or app_id in managed or not _selection_pinned(runtipi_root, store, app_id):
                continue
            latest = have
        skip = None
        if app_id in SYSTEM_APPS or app_id in managed:
            skip = "system"            # moves with the WizardOS release
        elif _selection_pinned(runtipi_root, store, app_id):
            skip = "pinned"            # an exact owner-local selection
        elif app.get("ignoredVersion") is not None and str(app.get("ignoredVersion")) == str(latest):
            skip = "ignored"           # the owner skipped this version in the App Manager
        elif app_id in held:
            skip = "held"
        elif (failed.get(app_id) or {}).get("to") == latest and \
                time.time() - float(failed[app_id].get("at") or 0) < RETRY_DAYS * 86400:
            skip = "failed"            # failed lately; waits for a newer version (or RETRY_DAYS)
        elif app.get("status") not in ("running", "stopped"):
            skip = f"busy:{app.get('status')}"
        out.append({"id": app_id, "name": display_name(runtipi_root, store, app_id), "from": have, "to": latest, "to_version": meta.get("latestDockerVersion"),
                    "status": app.get("status"), "skip": skip})
    return sorted(out, key=lambda r: r["id"])


def status(c=None, installed: list[dict] | None = None) -> dict:
    from . import cli
    c = c or cli.Config()
    if installed is None:
        installed = _installed_unpinned(c, c.runtipi())
    rows = candidates(installed, store=cli.STORE, runtipi_root=c.runtipi_root, home=c.home)
    return {"app_mode": mode(c.home), "holds": holds(c.home), "due": [r for r in rows if not r["skip"]],
            "skipped": [r for r in rows if r["skip"]], "last": _state(c.home).get("last")}


# ---------------------------------------------------------------- Settings > About & updates (wizard-ux#1191)

WHATS_NEW_MAX = 140                  # one line, like wizard.md `summary`
_APP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def whats_new(installed: list[dict], *, store: str, runtipi_root: Path) -> dict:
    """{id: {"version": tipi_version, "text": one line}} for each installed app of the store whose App Store copy
    says what changed: the optional wizard.md front matter key `whats_new` at the commit the Store holds now.

    That copy is the version an update would install (metadata.latestVersion is its config.json tipi_version), and,
    once the app has updated, the version it runs. Apps without the key are left out; the reader never fails."""
    from . import wizardmd
    out = {}
    for row in installed:
        app = row.get("app") or {}
        app_id = app.get("appName")
        if app.get("appStoreSlug") != store or not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id):
            continue
        folder = Path(runtipi_root) / "repos" / store / "apps" / app_id
        try:
            with open(folder / "wizard.md", encoding="utf-8", errors="replace") as f:
                text = wizardmd.front_matter(f.read(65536)).get("whats_new")
            version = int(json.loads((folder / "config.json").read_text()).get("tipi_version"))
        except Exception:  # noqa: BLE001 - no notes, unreadable or odd files: just no line for this app
            continue
        if not isinstance(text, str):
            continue
        line = " ".join(text.split())   # one line, even from a copy published before the checker rule (spec 8d)
        if line:
            out[app_id] = {"version": version, "text": line[:WHATS_NEW_MAX]}
    return out


_BUG_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]{0,9}")


def bug_url(url) -> str | None:
    """A GitHub issue URL as update_reports files or finds it, else None (nothing else is ever linked)."""
    return url if isinstance(url, str) and _BUG_URL.fullmatch(url) else None


def _reports_on() -> bool:
    """Whether wizard/update-bug-reports (#524) is on; tests monkeypatch this."""
    from . import update_reports
    return update_reports.switch_on()


def owner_status(c=None) -> dict:
    """status() plus what the owner needs to read a failure: the version that failed, when, and why (plain text),
    the bug the team was told in (`bug`, only with wizard/update-bug-reports on and a bug filed or found), and each
    app's one-line What's new (whats_new()). Read only; the same state file the nightly run writes and the
    Store's own copy of each app, never a second copy."""
    from . import cli
    c = c or cli.Config()
    installed = _installed_unpinned(c, c.runtipi())
    linked = _reports_on()   # the bug link only while the owner lets the box report failures (#524)
    failed = {k: {"to": v["to"], "at": int(v.get("at") or 0), "reason": str(v.get("reason") or "")[:300],
                  **({"bug": bug_url(v.get("bug"))} if linked and bug_url(v.get("bug")) else {})}
              for k, v in _failed(c.home).items()}
    last = _state(c.home).get("last")
    return {**status(c, installed), "failed": failed, "last": last if isinstance(last, dict) else None,
            "notes": whats_new(installed, store=cli.STORE, runtipi_root=c.runtipi_root),
            "running": running_all(c.home), "queued": queued(c.home, c.runtipi_root, cli.STORE)}


# ---------------------------------------------------------------- what is updating now (#733)

def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _set_running(h: Path, r: dict, step: str, started: int) -> None:
    """Say which app this run is on and its step (preparing / updating / checking / rolling back). Never fails."""
    doc = {"pid": os.getpid(), "app": r["id"], "from": _int(r.get("from")), "to": _int(r.get("to")),
           "to_version": r.get("to_version") if isinstance(r.get("to_version"), str) else None,
           "step": step, "started": started}
    try:
        tmp = h / (RUNNING + ".tmp")
        tmp.write_text(json.dumps(doc) + "\n")
        tmp.replace(h / RUNNING)
    except OSError:
        pass


def _clear_running(h: Path) -> None:
    try:
        (h / RUNNING).unlink()
    except OSError:
        pass


def _alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def running(home: Path | None = None) -> list[dict]:
    """The app `wam app-updates --apply` is updating now, as [{app, from, to, to_version, step, started, by}], or [].
    Trusted only while a run holds the lock: a file a killed run left behind is stale (and removed). Never blocks."""
    h = _home(home)
    path = h / RUNNING
    if not path.exists():
        return []
    try:
        fd = os.open(h / LOCK, os.O_RDONLY)
    except OSError:
        return []   # no lock file: no run ever took it
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held = True
        else:   # nothing holds the lock, so no run is active: the file is a leftover
            held = False
            _clear_running(h)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    if not held:
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []   # the run just finished (or is between writes)
    if not isinstance(doc, dict) or not _alive(doc.get("pid")) or not isinstance(doc.get("app"), str) \
            or not _APP_ID.fullmatch(doc["app"]) or doc.get("step") not in ("preparing", "updating", "checking",
                                                                            "rolling back"):
        return []   # the lock is held by something else (maintenance, an OS update) over a dead run's file
    return [{"app": doc["app"], "from": _int(doc.get("from")), "to": _int(doc.get("to")),
             "to_version": doc.get("to_version") if isinstance(doc.get("to_version"), str) else None,
             "step": doc["step"], "started": _int(doc.get("started")) or 0, "by": "app-updates"}]


def store_running() -> list[dict]:
    """Store Get/Update jobs still installing in this API process (store_install._jobs), same shape as running()."""
    try:
        from . import store_install
        with store_install._lock:
            jobs = [dict(j) for j in store_install._jobs.values()]
    except Exception:  # noqa: BLE001 - never fail the status for it
        return []
    return [{"app": j["app"], "from": None, "to": None, "to_version": None, "step": "installing",
             "started": _int(j.get("started")) or 0, "by": "store"}
            for j in jobs if isinstance(j, dict) and j.get("state") == "installing" and isinstance(j.get("app"), str)]


def running_all(home: Path | None = None) -> list[dict]:
    """running() plus the Store's jobs, one entry per app (the app-updates run wins)."""
    try:
        out = running(home)
    except Exception:  # noqa: BLE001 - never fail the status for it
        out = []
    seen = {e["app"] for e in out}
    return out + sorted((e for e in store_running() if e["app"] not in seen), key=lambda e: e["app"])


# ---------------------------------------------------------------- verify / roll back

def _containers(urn: str) -> list[str]:
    from . import cli
    try:
        return cli._app_container_states(urn)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return []


def _probe(url: str, mcp: bool, timeout: float) -> bool:
    if mcp:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "wam-app-updates", "version": "1"}}}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "content-type": "application/json", "accept": "application/json, text/event-stream"})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _endpoint(c, rt, app_id: str) -> tuple[str, bool] | None:
    """The app's declared health URL, else its MCP endpoint (loopback), from a freshly written registry."""
    from . import cli
    try:
        reg = cli.write_registry(c, rt)
    except Exception:  # noqa: BLE001 - the registry is a convenience here, never a reason to roll back
        return None
    for a in reg.get("apps") or []:
        if a.get("id") == app_id:
            if a.get("health"):
                return str(a["health"]), False
            if (a.get("mcp") or {}).get("url"):
                return str(a["mcp"]["url"]), True
    return None


def baseline(c, rt, app_id: str, status: str | None, *, endpoint=_endpoint, probe=_probe) -> bool | None:
    """Whether the app's health URL / MCP endpoint answers BEFORE its update (None: not running or none declared)."""
    if status != "running":
        return None
    ep = endpoint(c, rt, app_id)
    return probe(ep[0], ep[1], 10) if ep else None


def settle_record(c, rt, urn: str, app_id: str, res: dict, before: dict, *, retry=None) -> dict:
    """#895: an update can finish on the box while Runtipi's record still says the old version. A Runtipi whose
    update job gives up after its 5-minute queue limit (a big pre-update backup) records the app `stopped` on its
    old version, while the job goes on and the new version's files and containers end up in place. Without this
    check the app is reported \"rolled back\" though the new version runs, and it is marked failed.

    When Runtipi records an older version than this update's target, its status has settled (running or stopped),
    and the installed recipe (apps/<store>/<id>/config.json, which Runtipi replaces with the new one during the
    update and puts back on its own rollback) already is the target, the record is stale: the update is run once
    more WITHOUT a backup (the first pass made one; a second full backup would only pile up), which writes the right
    version. The result carries `stale_record: True` either way, so a record that still does not move is reported
    as what it is, never as a rollback. Anything else: `res` unchanged."""
    from . import cli
    try:
        row = ((rt.app(urn) or {}).get("app") or {}) if rt is not None else {}
        recorded = int(row.get("version") if row.get("version") is not None else res.get("version") or 0)
    except Exception:  # noqa: BLE001 - unreadable: verify reports what it sees
        return res
    if recorded >= int(before.get("to") or 0) or row.get("status") not in ("running", "stopped"):
        return res
    installed, _ = _recipe_version(Path(c.runtipi_root) / "apps" / cli.STORE / app_id)
    if installed is None or installed < before["to"]:
        return res   # the old recipe is installed: the update really did not apply (or was rolled back)
    print(f"{app_id}: version {installed} is installed but Runtipi still records {recorded}; "
          "correcting the record (update again, no second backup)", file=sys.stderr, flush=True)
    try:
        again = (retry or (lambda: cli.app_update(c, app_id, backup=False)))()
    except (Exception, SystemExit) as e:  # noqa: BLE001 - reported below as a stale record, never a rollback
        return {**res, "stale_record": True, "installed_version": installed,
                "record_error": f"{type(e).__name__}: {e}"[:200]}
    return {**again, "stale_record": True, "installed_version": installed}


def verify(c, rt, urn: str, app_id: str, res: dict, before: dict, *, settle: float = SETTLE_SECONDS,
           probe_seconds: float = PROBE_SECONDS, sleep=time.sleep, containers=_containers, probe=_probe,
           endpoint=_endpoint) -> str | None:
    """None when the updated app works; otherwise the reason it does not."""
    if res.get("error"):
        return str(res["error"])
    if res.get("status") != before["status"]:
        return f"came back {res.get('status')} (was {before['status']})"
    try:
        if int(res.get("version") or 0) < before["to"]:
            if res.get("stale_record"):   # #895: the new version is in place; only Runtipi's record lags
                return (f"version {res.get('installed_version') or before['to']} is installed, but Runtipi still "
                        f"records version {res.get('version')} and correcting it did not work"
                        + (f" ({res['record_error']})" if res.get("record_error") else ""))
            return f"still on version {res.get('version')} (expected {before['to']}); the update was rolled back"
    except (TypeError, ValueError):
        return "unknown version after the update"
    if before["status"] != "running":
        return None
    sleep(settle)
    states = containers(urn)
    if "restarting" in states:
        return "a container keeps restarting"
    if states.count("running") < before["running"]:
        return f"{states.count('running')} of {before['running']} containers running"
    ep = endpoint(c, rt, app_id)
    # An endpoint that did not answer before the update either (e.g. an app whose host check refuses loopback
    # with 403 "Unknown host") says nothing about the update: containers decide, as for apps without one.
    if ep and before.get("endpoint_ok") is not False:
        deadline = time.monotonic() + probe_seconds
        while not probe(ep[0], ep[1], 10):
            if time.monotonic() >= deadline:
                return ("its MCP endpoint does not answer" if ep[1] else "its health check fails") + f" ({ep[0]})"
            sleep(5)
    return None


def rollback(c, rt, urn: str, started_ms: int, before: dict) -> dict:
    """Restore the backup this update made (app data, recipe and settings) and bring the app back as it was."""
    from . import cli
    backups = [b for b in (rt.list_backups(urn) or []) if int(b.get("date") or 0) >= started_ms - FRESH_SLACK_MS]
    if not backups:
        return {"rolled_back": False, "error": "no backup from this update to roll back to"}
    rt.restore_backup(urn, backups[0]["id"])
    time.sleep(3)
    status = rt.wait_status(urn, {"running", "stopped", "restore_error", "missing"}, 900)
    if before["status"] == "running" and status != "running":
        rt.start(urn)
        status = rt.wait_status(urn, {"running"}, 300)
    try:
        cli.network_step(c, rt)   # the restored recipe's networks
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: networks after rolling back {urn}: {e}", file=sys.stderr)
    after = ((rt.app(urn) or {}).get("app") or {})
    ok = status == before["status"] and int(after.get("version") or 0) == before["from"]
    return {"rolled_back": ok, "status": status, "version": after.get("version"),
            **({} if ok else {"error": f"rollback left the app {status} on version {after.get('version')}"})}


# ---------------------------------------------------------------- Store Update / Start (#857)
# The Store's (and the Tracker's) Update and Start used to trust Runtipi's status alone. Runtipi says `running` while
# a container sits in Docker's Restarting state, so a version the nightly run had caught and rolled back was installed
# again by a Store tap and reported `installed`, and a Start of a crashing app said Started. Both now get the same
# check as the nightly updater; a Store update that fails it is rolled back from the backup the update just made.

def _recipe_version(folder: Path) -> tuple[int | None, str | None]:
    """(tipi_version, version name) of a recipe folder's config.json; (None, None) when unreadable."""
    try:
        cfg = json.loads((Path(folder) / "config.json").read_text())
    except (OSError, ValueError):
        return None, None
    if not isinstance(cfg, dict):
        return None, None
    try:
        tipi = int(cfg.get("tipi_version"))
    except (TypeError, ValueError):
        tipi = None
    name = cfg.get("version")
    return tipi, (str(name) if name not in (None, "") else None)


def failed_recently(home: Path | None, app_id: str, to: int | None) -> dict | None:
    """The app-updates.json `failed` record when version `to` of the app failed its check lately (same rule as the
    nightly run's `failed` skip), else None."""
    rec = _failed(home).get(app_id)
    if to is None or not rec or rec.get("to") != to:
        return None
    return rec if time.time() - float(rec.get("at") or 0) < RETRY_DAYS * 86400 else None


def store_refusal(c, app_id: str, force: bool = False) -> str | None:
    """Why a Store update of the app to its prepared store recipe must not run: that exact version failed the check
    after an update lately and was rolled back (nightly or Store). None when it may run, or the owner forces it."""
    from . import cli
    if force:
        return None
    to, to_name = _recipe_version(Path(c.runtipi_root) / "repos" / cli.STORE / "apps" / app_id)
    rec = failed_recently(c.home, app_id, to)
    if not rec:
        return None
    name = display_name(c.runtipi_root, cli.STORE, app_id)
    when = time.strftime("%d %b %H:%M UTC", time.gmtime(float(rec.get("at") or 0)))
    reason = f" ({rec['reason']})" if rec.get("reason") else ""
    return (f"{name} {to_name or 'this version'} failed its check after an update on {when}{reason} and was rolled "
            f"back, so it waits for a newer version. Update anyway only if you know it was fixed.")


def _record_failed(home: Path | None, app_id: str, to: int | None, problem: str) -> None:
    """Never raises: a state file that can't be written must not stop the rollback or the report."""
    if to is None:
        return
    try:
        _write_failed(home, {}, {app_id: {"to": to, "at": int(time.time()), "reason": problem[:300]}})
    except OSError as e:
        print(f"WARNING: {app_id}: failed version not recorded: {e}", file=sys.stderr)


def _clear_failed(home: Path | None, app_id: str) -> None:
    try:
        if app_id in _failed(home):
            _write_failed(home, {app_id: _failed(home)[app_id]}, {})
    except OSError as e:
        print(f"WARNING: {app_id}: old failure not cleared: {e}", file=sys.stderr)


def store_update(c, app_id: str, update, *, settle: float = SETTLE_SECONDS, probe_seconds: float = PROBE_SECONDS,
                 sleep=time.sleep, containers=None, probe=None, endpoint=None, roll=None, retry=None) -> dict:
    """Run a Store update (`update()` -> app_update's result) and check the app the way the nightly run does
    (`verify`): it comes back as it was, its version moved, no container restarts, no fewer containers run, its
    health URL / MCP endpoint answers. On a failed check it is rolled back from the backup the update made, the
    version is recorded in app-updates.json `failed` (the nightly run then skips it too), and the result carries an
    `error` naming what happened, e.g. "Photos 0.4.0 keeps crashing (a container keeps restarting); kept 0.3.1".
    A SystemExit from update() (owner gate, a setting only the owner can give: nothing changed) passes through."""
    from . import cli
    containers = containers or _containers
    probe = probe or _probe
    endpoint = endpoint or _endpoint
    roll = roll or rollback
    urn = f"{app_id}:{cli.STORE}"
    try:
        rt = c.runtipi()
        row = ((rt.app(urn) or {}).get("app") or {})
    except Exception:  # noqa: BLE001 - Runtipi unreadable: the update itself will say so
        rt, row = None, {}
    if rt is None or not row:
        return update()   # nothing to compare against; app_update refuses an app that is not installed
    try:
        have = int(row.get("version") or 0)
    except (TypeError, ValueError):
        have = 0
    to, to_name = _recipe_version(Path(c.runtipi_root) / "repos" / cli.STORE / "apps" / app_id)
    _, from_name = _recipe_version(Path(c.runtipi_root) / "apps" / cli.STORE / app_id)
    name = display_name(c.runtipi_root, cli.STORE, app_id)
    status = row.get("status")
    before = {"id": app_id, "name": name, "from": have, "to": to if to is not None else have, "status": status,
              "running": containers(urn).count("running"),
              "endpoint_ok": baseline(c, rt, app_id, status, endpoint=endpoint, probe=probe)}
    started_ms = int(time.time() * 1000)
    try:
        res = update()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - it may have half-applied: check and roll back like the nightly run
        res = {"error": f"{type(e).__name__}: {e}"[:300], "status": None}
    res = settle_record(c, rt, urn, app_id, res, before, retry=retry)   # #895
    problem = verify(c, rt, urn, app_id, res, before, settle=settle, probe_seconds=probe_seconds, sleep=sleep,
                     containers=containers, probe=probe, endpoint=endpoint)
    if problem is None:
        _clear_failed(c.home, app_id)
        return res
    what = f"{name} {to_name or 'update'} " + ("keeps crashing" if "restarting" in problem else "did not work")
    if res.get("stale_record"):   # #895: the new version runs; restoring the backup would throw away its data
        _record_failed(c.home, app_id, to, problem)
        return {**res, "error": f"{what} ({problem})"[:400], "rolled_back": False}
    try:
        unchanged = int(res.get("version") or 0) == have and res.get("status") == status
    except (TypeError, ValueError):
        unchanged = False
    if unchanged:   # the update never applied: the previous version still runs, nothing to restore
        _record_failed(c.home, app_id, to, problem)
        return {**res, "error": f"{what} ({problem}); {from_name or 'the previous version'} still runs",
                "rolled_back": False}
    print(f"{app_id}: {problem}; rolling back", file=sys.stderr, flush=True)
    try:
        rb = roll(c, rt, urn, started_ms, before)
    except Exception as e:  # noqa: BLE001
        rb = {"rolled_back": False, "error": f"{type(e).__name__}: {e}"[:300]}
    _record_failed(c.home, app_id, to, problem)   # after the rollback: recording never delays or blocks it
    if rb.get("rolled_back"):
        err = f"{what} ({problem}); kept {from_name or 'the previous version'}"
    else:
        err = f"{what} ({problem}), and rolling it back did not work: {rb.get('error')}"
    return {**res, "status": rb.get("status", res.get("status")), "error": err[:400],
            "rolled_back": bool(rb.get("rolled_back")), "rollback": rb}


def check_started(c, app_id: str, *, settle: float = SETTLE_SECONDS, sleep=time.sleep, containers=None) -> str | None:
    """After a Store Start that Runtipi reports `running`: None when the app stays up, else why it does not (the
    container half of `verify`; there is no before-start endpoint baseline, so its health URL is not judged)."""
    from . import cli
    sleep(settle)
    name = display_name(c.runtipi_root, cli.STORE, app_id)
    try:
        states = (containers or cli._app_container_states)(f"{app_id}:{cli.STORE}")
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        # Docker itself unreadable from here: Runtipi's `running` is all there is (never fail every Start for it)
        print(f"WARNING: {app_id}: containers not checked after start: {e}", file=sys.stderr)
        return None
    if "restarting" in states:
        return f"{name} keeps crashing after it starts (a container keeps restarting)"
    if "running" not in states:   # gone, exited or dead: it did not stay up (Codex P2: an empty set never passes)
        return f"{name} stopped again right after it started"
    return None


# ---------------------------------------------------------------- apply

def reconcile_settings(c, rt, app_id: str) -> list[str]:
    """Fill settings the new version newly requires, with the same rules as install (shared secrets from the apps
    it uses, generated passwords, owner origins), keeping every saved value. Runtipi refuses an update whose
    required variable is unset ("Variable ... is required"), so app_update runs this once the update is admitted
    (after the owner gate), right before queueing it. Returns what only the owner can give (then nothing is changed)."""
    from . import cli
    urn = f"{app_id}:{cli.STORE}"
    recipe = Path(c.runtipi_root) / "repos" / cli.STORE / "apps" / app_id
    try:
        info = json.loads((recipe / "config.json").read_text())
        uses = (cli.registry._front((recipe / "wizard.md").read_text()) or {}).get("uses") or []
    except (OSError, ValueError):
        return []   # no prepared recipe to read: let the update itself decide
    current = (((rt.app(urn) or {}).get("app") or {}).get("config")) or {}
    form, missing = cli.build_form(c, app_id, info, current, uses=uses if isinstance(uses, list) else [])
    if missing:
        return missing
    form = {**form, **current}   # ports, exposure, credentials and unknown saved fields stay as they are
    if form != current:
        rt.req("PATCH", f"/app-lifecycle/{urn}/update-config", form, timeout=120)
    return []


def _notify(c, title: str, message: str, priority: str = "default", required: bool = False) -> bool:
    """Best effort: one line in the owner's Notifications inbox when that app is installed. True when it was taken
    (`required`: a JSON-RPC error counts as not delivered, so the caller can try again later)."""
    try:
        reg = json.loads((Path(c.home) / "apps.json").read_text())
        url = next((a["mcp"]["url"] for a in reg.get("apps") or []
                    if a.get("id") == "notifications" and (a.get("mcp") or {}).get("url")), None)
        if not url:
            return False
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "notify", "arguments": {"title": title, "message": message, "priority": priority}}}).encode()
        got = urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST", headers={
            "content-type": "application/json", "accept": "application/json, text/event-stream"}), timeout=15).read()
        if required:
            text = got.decode("utf-8", "replace")
            return '"result"' in text and '"isError":true' not in text.replace(" ", "")
        return True
    except Exception:  # noqa: BLE001 - telling the owner never fails the run
        return False


def _summary(done: list, rolled: list, failed: list, needs: list, renamed: list = (),
             told: set | frozenset = frozenset()) -> tuple[str, str, str] | None:
    """The owner's notice. Apps are named as the owner sees them (Movies), never by internal id (cinequeue).
    `told`: ids of failed/rolled-back apps already reported to the team (update_reports, switch-gated)."""
    if not (done or rolled or failed or needs or renamed):
        return None

    def nm(r: dict) -> str:
        return r.get("name") or r["id"]

    parts = []
    if renamed:
        parts.append("Moved " + ", ".join(nm(r) for r in renamed) + " to " + ("its" if len(renamed) == 1 else "their")
                     + " new release; everything you saved came along.")
    if done:
        parts.append("Updated " + ", ".join(f"{nm(d)} ({d['to_version'] or d['to']})" for d in done) + ".")
    if rolled:
        parts.append("Kept the previous version of " + ", ".join(nm(r) for r in rolled)
                     + " because the new one did not work; it is tried again when a newer version ships.")
        if all(r.get("id") in told for r in rolled):
            parts.append("The team was told about it.")
    if needs:
        parts.append("Waiting for you: " + "; ".join(f"{nm(n)}: {n['reason']}" for n in needs) + ".")
    if failed:
        parts.append("Could not update " + "; ".join(f"{nm(f)}: {f['reason']}" for f in failed)
                     + (". The team was told, so you don't need to report it." if all(f.get("id") in told for f in failed)
                        else ". Ask your Wizard to look at it."))
    bad = rolled or failed or needs
    n = len(done) + len(renamed)
    title = "App updates need a look" if bad else (f"{n} app{'s' if n != 1 else ''} updated")
    return title, " ".join(parts), "high" if failed else "default"


def apply(c=None, *, only: list[str] | None = None, dry_run: bool = False, notify: bool = True, renames=None,
          reporter=None, moves=None, quiet_repeats: bool = False, queue: bool = True) -> dict:
    """Update every due app (or `only` those), one at a time, verifying each and rolling back failures.
    `quiet_repeats` (the hourly timer): a notice that would only repeat a "Waiting for you" already delivered in the
    last day is not sent again, so an app waiting for the owner's OK reminds once a day, not every hour.
    While another app-updates run is going, the request is queued for that run (#764) unless `queue` is False (the
    timer: its next tick comes anyway); the run takes queued requests after its own apps, under the same lock."""
    from . import cli
    c = c or cli.Config()
    h = _home(c.home)
    h.mkdir(parents=True, exist_ok=True)
    lock = open(h / LOCK, "a+")   # never "w": that would wipe the pid a running run wrote (#764)
    for attempt in (1, 2):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            q = request_for(c, only) if queue and not dry_run else None
            if q is not None or attempt == 2:   # queued; or the lock is not a run's (maintenance, an OS update)
                lock.close()
                return q or {"started": False, "skipped": "running", "note": "app updates are already running"}
            # no run holds it: it may have just ended, so try once more
    released = False
    try:
        if not dry_run:   # a dry run takes no requests, so it never says it is a run (Codex P2)
            lock.seek(0)
            lock.truncate()
            lock.write(f"{os.getpid()}\n")   # says an app-updates run holds the lock (not maintenance, #764)
            lock.flush()
            if only:   # asked for again while waiting: this run takes them now
                with _queue_lock(h):
                    _queue_write(h, [e for e in _queue_read(h, expire=False) if e["app"] not in set(only)])
        _clear_running(h)   # a run killed mid-update left its app behind (#733)
        kw = {"dry_run": dry_run, "notify": notify, "renames": renames, "reporter": reporter, "moves": moves,
              "quiet_repeats": quiet_repeats}
        out = _apply_locked(c, h, only, **kw)
        if dry_run or out.get("skipped") == "installing":
            return out   # queued requests wait for the next run (a WizardOS update is going)
        while True:
            with _queue_lock(h):
                entries = _queue_take(h)
                if not entries:
                    _release(h, lock)   # under the queue lock: a request now either sees no run or was taken
                    released = True
                    break
            # The apps asked for by name, in the order asked, as one `only` batch: an explicit request (Retry, Update
            # now on a held app) overrides holds and past failures, never system/pinned apps (Codex P1). Ids that are
            # not installed or have nothing newer come back in `unknown`/nowhere and are not retried.
            ids = list(dict.fromkeys(e["app"] for e in entries))
            print(f"queued: updating {', '.join(ids)}", file=sys.stderr, flush=True)
            try:
                out = _merge(out, _apply_locked(c, h, ids, **kw), ids)
            except BaseException:
                _queue_put_back(h, entries)   # a Runtipi error or a stop loses no request: the next run takes it
                raise
        return out
    finally:
        if not released:
            _release(h, lock)
        lock.close()


def _release(h: Path, lock) -> None:
    _clear_running(h)   # before the lock goes, so a reader never sees a finished run (#733)
    try:
        lock.seek(0)
        lock.truncate()   # the pid goes first: maintenance may take this lock next (#764)
        lock.flush()
    except OSError:
        pass
    fcntl.flock(lock, fcntl.LOCK_UN)


LISTS = ("renamed", "updated", "rolled_back", "failed", "needs_owner", "skipped", "unknown")


def _merge(a: dict, b: dict, ids: list[str] | None) -> dict:
    """One answer for a run plus the queued requests it took after its own apps (#764)."""
    if not b.get("started"):
        return {**a, "queued_ran": a.get("queued_ran", []) + [ids or ["*"]], "queued_note": b.get("note")}
    out = {**a, **{k: list(a.get(k) or []) + list(b.get(k) or []) for k in LISTS}}
    out["message"] = "\n".join(m for m in (a.get("message"), b.get("message")) if m) or None
    out["queued_ran"] = a.get("queued_ran", []) + [ids or ["*"]]
    if b.get("reported"):
        out["reported"] = {**(a.get("reported") or {}), **b["reported"]}
    return out


# ---------------------------------------------------------------- queued requests (#764)

QUEUE = "app-updates-queue.json"
QUEUE_LOCK = "app-updates-queue.lock"
QUEUE_MAX_AGE = 6 * 3600    # a request no run took within this long is dropped (a killed run never drains it)
QUEUE_MAX = 100             # app ids per request, and requests kept
QUEUE_NOTE = "queued behind the update that is running"


class _queue_lock:
    def __init__(self, h: Path):
        self.h = h

    def __enter__(self):
        self.h.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.h / QUEUE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _queue_read(h: Path, now: float | None = None, expire: bool = True) -> list[dict]:
    """[{app, at}], deduped by app (first kept), order kept. `expire`: drop requests older than QUEUE_MAX_AGE (only
    while no run holds the lock; a long run still gets to them)."""
    try:
        doc = json.loads((h / QUEUE).read_text())
    except (OSError, ValueError):
        return []
    now = time.time() if now is None else now
    out, seen = [], set()
    for e in doc if isinstance(doc, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("app"), str) or not _APP_ID.fullmatch(e["app"]) \
                or e["app"] in seen:
            continue
        at = _int(e.get("at"))
        if at is None or (expire and now - at > QUEUE_MAX_AGE):
            continue
        seen.add(e["app"])
        out.append({"app": e["app"], "at": at})
    return out[:QUEUE_MAX]


def _queue_write(h: Path, entries: list[dict]) -> None:
    if not entries:
        try:
            (h / QUEUE).unlink()
        except OSError:
            pass
        return
    tmp = h / (QUEUE + ".tmp")
    tmp.write_text(json.dumps(entries) + "\n")
    tmp.replace(h / QUEUE)


def _queue_take(h: Path) -> list[dict]:
    entries = _queue_read(h, expire=False)   # the run holds the lock: nothing it was asked for is stale
    _queue_write(h, [])
    return entries


def _queue_put_back(h: Path, entries: list[dict]) -> None:
    """Requests a failed batch took go back in front of anything asked for since. Never fails."""
    try:
        with _queue_lock(h):
            _queue_write(h, _dedupe(entries + _queue_read(h, expire=False)))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: queued app updates not put back: {type(e).__name__}", file=sys.stderr)


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set = set()
    return [e for e in entries if not (e["app"] in seen or seen.add(e["app"]))][:QUEUE_MAX]


def run_active(h: Path) -> bool:
    """True while an app-updates run (not maintenance or an OS update, which hold the same lock) holds the lock:
    the holder wrote its pid into the lock file. Never blocks."""
    try:
        fd = os.open(h / LOCK, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            pass   # held
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        try:
            pid = int(os.read(fd, 32).decode().strip() or 0)
        except (OSError, ValueError):
            return False
    finally:
        os.close(fd)
    return pid > 0 and _alive(pid)


def request(only: list[str], home: Path | None = None) -> dict | None:
    """Queue these app ids for the run that is going (an app already queued keeps its place). None when no
    app-updates run holds the lock (the caller starts one). Checked and written under the queue lock, which the run
    also takes before it lets its lock go, so a request is never left behind by a run that just ended.
    `queued` lists the ids the queue really holds (it keeps QUEUE_MAX)."""
    h = _home(home)
    ids = list(dict.fromkeys(i for i in only or [] if isinstance(i, str) and _APP_ID.fullmatch(i)))[:QUEUE_MAX]
    with _queue_lock(h):
        if not run_active(h):
            return None
        now = int(time.time())
        entries = _dedupe(_queue_read(h, expire=False) + [{"app": i, "at": now} for i in ids])
        if ids:
            _queue_write(h, entries)
    kept = {e["app"] for e in entries}
    return {"started": False, "queued": [i for i in ids if i in kept], "note": QUEUE_NOTE}


def request_for(c, only: list[str] | None) -> dict | None:
    """request() for a page or `wam app-updates --apply`: `only` ids, or (None) every app due now. None when no
    app-updates run is going."""
    if not run_active(_home(c.home)):
        return None
    if only is None:
        try:
            only = [r["id"] for r in status(c)["due"]]
        except Exception as e:  # noqa: BLE001 - Runtipi unreachable: nothing to queue, say why in the log
            print(f"WARNING: due apps not read: {type(e).__name__}", file=sys.stderr)
            only = []
    return request(only, c.home)


def request_apply(c, only: list[str] | None = None) -> dict:
    """Update now / Update all from a page: queue behind the app-updates run going now (200 {"started": false,
    "queued": [ids], "note"}), else start a run in the background ({"started": true} or {"started": false, "error"})."""
    return request_for(c, only) or start_detached(only=only)


def queued(home: Path | None = None, runtipi_root: Path | None = None, store: str | None = None) -> list[dict]:
    """What waits for the running update, as [{app, at, name}]. Requests older than QUEUE_MAX_AGE while no run holds
    the lock are dropped here."""
    h = _home(home)
    try:
        with _queue_lock(h):
            active = run_active(h)
            entries = _queue_read(h, expire=not active)
            if not active and (h / QUEUE).exists():
                _queue_write(h, entries)   # rewrite without the expired ones
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            name = display_name(runtipi_root, store, e["app"]) if runtipi_root and store else e["app"]
        except Exception:  # noqa: BLE001 - a name never fails the status
            name = e["app"]
        out.append({"app": e["app"], "at": e["at"], "name": name})
    return out


def _apply_locked(c, h: Path, only: list[str] | None, *, dry_run: bool, notify: bool, renames, reporter, moves,
                  quiet_repeats: bool) -> dict:
    """apply() once its lock is held: one pass over `only` (or every due app)."""
    from . import cli, updates
    if updates.installing(c.home):
        return {"started": False, "skipped": "installing", "note": "a WizardOS update is running; next window"}
    rt = c.runtipi()
    repo_moved = []
    if not dry_run:
        # An app whose repo moved to a new GitHub name or owner (#578) first records the new name, so its
        # updates (and the rename check below) follow the repo instead of stopping at the delisted old name.
        from . import repo_moves
        try:
            # (and first frees apps a Store Get/Update pinned, so their updates show; wizard-os#614)
            repo_moved = (moves or repo_moves.follow_all)(c, _installed_unpinned(c, rt), only=only)
        except Exception as e:  # noqa: BLE001 - a move check never stops the updates
            print(f"WARNING: app repo moves not checked: {e}", file=sys.stderr)
    rows = candidates(rt.installed(), store=cli.STORE, runtipi_root=c.runtipi_root, home=c.home)
    if only:
        unknown = sorted(set(only) - {r["id"] for r in rows})
        rows = [r for r in rows if r["id"] in only]
        for r in rows:   # an explicit request overrides holds and a past failure, never system/pinned apps
            if r["skip"] in ("held", "failed", "ignored"):
                r["skip"] = None
    else:
        unknown = []
    due = [r for r in rows if not r["skip"]]
    if dry_run:
        out = {"dry_run": True, "due": due, "skipped": [r for r in rows if r["skip"]], "unknown": unknown}
        if only and not renames:   # say what asking for it would do (a staging copy's move, wizard-os#628; a
            from . import app_rename   # renamed app's move, #832)
            try:
                moves = [d for d in app_rename.due(c, rt, only=list(only)) if d["from"] in only]
            except Exception:  # noqa: BLE001 - a dry run never fails on the rename check
                moves = []
            if moves:
                gone = {d["from"] for d in moves}
                out["moves"] = moves
                out["unknown"] = [x for x in unknown if x not in gone]
                out["due"] = [r for r in due if r["id"] not in gone]
        return out
    done, rolled, failed, needs = [], [], [], []
    renamed = []
    # An app its publisher renamed (wizard.md renamed_from) moves to its new id first, verified or rolled
    # back on its own; its update, if any, then runs under the new id on the next run. Asked for by id, an
    # app with no update of its own (a staging copy the released app claims, wizard-os#628) moves too, and so
    # does a renamed app that also has an update under its old id (#832: updating it in place, or installing
    # the new id beside it, left two apps).
    from . import app_rename
    try:
        moves = (renames(c, rt) if renames else app_rename.rename_due(c, rt, only=list(only) if only else None))
    except Exception as e:  # noqa: BLE001 - a rename check never stops the updates
        print(f"WARNING: app renames not checked: {e}", file=sys.stderr)
        moves = []
    for res in moves:
        if res.get("renamed"):
            renamed.append({"id": res["to"], "name": res.get("name") or res["to"]})
        else:
            failed.append({"id": res.get("from"), "name": res.get("name") or res.get("from"),
                           "reason": "moving it to its new name did not work, so it stays as it was"
                           + ("" if res.get("rolled_back", True) else " (needs a look)"),
                           "rollback": res, "step": "move to its new name"})
    # an app whose move was tried is never updated in place in the same run (#832, Codex P1): a failed move left
    # it as it was, a move that could not retire the old id finishes on the next run
    tried = {res.get("from") for res in moves}
    due = [r for r in due if r["id"] not in tried]
    if renamed:
        moved = {r["id"] for r in renamed} | tried
        rows = candidates(rt.installed(), store=cli.STORE, runtipi_root=c.runtipi_root, home=c.home)
        if only:   # an update asked for by id still touches only those apps (Codex P1)
            rows = [r for r in rows if r["id"] in only]
            for r in rows:
                if r["skip"] in ("held", "failed", "ignored"):
                    r["skip"] = None
        due = [r for r in rows if not r["skip"] and r["id"] not in moved]
    fails = _failed(c.home)
    fails_read = json.loads(json.dumps(fails))   # what this run read: only its own changes are written back (#857)
    for r in due:
        started = int(time.time())
        _set_running(h, r, "preparing", started)   # #733: pages read it via owner_status()
        urn = f"{r['id']}:{cli.STORE}"
        states = _containers(urn)
        before = {**r, "running": states.count("running"), "endpoint_ok": baseline(c, rt, r["id"], r["status"])}
        started_ms = int(time.time() * 1000)
        print(f"updating {r['id']} {r['from']} -> {r['to']}", file=sys.stderr, flush=True)

        def settings(rt_, urn_, r=r, started=started):
            missing = reconcile_settings(c, rt_, r["id"])
            if not missing:   # admitted: Runtipi's update is next (#733)
                _set_running(h, r, "updating", started)
            return missing
        try:
            # new required settings are filled inside app_update, only once the update is admitted (#582)
            res = cli.app_update(c, r["id"], settings=settings)
        except SystemExit as e:   # owner gate, a setting only the owner can give, or not installed: nothing changed
            needs.append({"id": r["id"], "reason": str(e).splitlines()[0][:200]})
            continue
        except Exception as e:  # noqa: BLE001 - one app never stops the others
            res = {"error": f"{type(e).__name__}: {e}"[:300], "status": None}
        res = settle_record(c, rt, urn, r["id"], res, before)   # #895: new version in place, record stale
        _set_running(h, r, "checking", started)
        problem = verify(c, rt, urn, r["id"], res, before)
        step = "update" if res.get("error") else "check after update"
        if problem is None:
            fails.pop(r["id"], None)
            done.append({**r, "version": res.get("version")})
            continue
        fails[r["id"]] = {"to": r["to"], "at": int(time.time()), "reason": problem[:300]}
        try:
            unchanged = int(res.get("version") or 0) == r["from"] and res.get("status") == before["status"]
        except (TypeError, ValueError):
            unchanged = False
        if res.get("stale_record"):   # #895: the new version runs; restoring the backup would throw away its data
            failed.append({**r, "reason": problem, "rollback": None, "step": "record after update"})
            continue
        if unchanged:   # the update never applied: the previous version still runs, nothing to restore
            failed.append({**r, "reason": problem + " (the previous version still runs)", "rollback": None,
                           "step": step})
            continue
        print(f"{r['id']}: {problem}; rolling back", file=sys.stderr, flush=True)
        _set_running(h, r, "rolling back", started)
        try:
            rb = rollback(c, rt, urn, started_ms, before)
        except Exception as e:  # noqa: BLE001
            rb = {"rolled_back": False, "error": f"{type(e).__name__}: {e}"[:300]}
        (rolled if rb.get("rolled_back") else failed).append(
            {**r, "reason": problem if rb.get("rolled_back") else f"{problem}; {rb.get('error')}", "rollback": rb,
             "step": step})
    _clear_running(h)   # every app is done; what follows is bookkeeping (#733)
    try:
        cli.write_registry(c, rt)
    except Exception:  # noqa: BLE001
        pass
    last = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "renamed": [x["id"] for x in renamed],
            "updated": [d["id"] for d in done], "rolled_back": [x["id"] for x in rolled],
            "failed": [x["id"] for x in failed], "needs_owner": [x["id"] for x in needs]}
    told_needs = _state(c.home).get("needs_told") or {}
    fails = _write_failed(c.home, fails_read, fails, last=last)
    repeat = bool(quiet_repeats and needs and not (done or rolled or failed or renamed)
                  and sorted(last["needs_owner"]) == told_needs.get("ids")
                  and time.time() - float(told_needs.get("at") or 0) < NEEDS_REMIND_SECONDS)
    reports = {}
    if notify and (rolled or failed):   # tell the team too (#524): only with wizard/update-bug-reports on
        try:
            from . import update_reports
            reports = (reporter or update_reports.report_all)(c, rolled + failed, store=cli.STORE)
        except Exception as e:  # noqa: BLE001 - reporting never fails the run
            print(f"WARNING: update failures not reported: {type(e).__name__}", file=sys.stderr)
    told = {k for k, v in reports.items() if isinstance(v, dict) and v.get("issue")}
    bugs = {k: reports[k]["issue"] for k in told if k in fails and bug_url(reports[k]["issue"])}
    if bugs:   # Settings links the failed row to the bug the team was told in (wizard-ux#1191)
        linked = dict(fails)
        for k, url in bugs.items():
            linked[k] = {**fails[k], "bug": url}
        fails = _write_failed(c.home, fails, linked)
    msg = _summary(done, rolled, failed, needs, renamed, told)
    if notify and msg and not repeat and _notify(c, *msg) and needs:
        _write(c.home, needs_told={"ids": sorted(last["needs_owner"]), "at": time.time()})
    return {"started": True, **({"repo_moved": repo_moved} if repo_moved else {}), "renamed": renamed, "updated": done, "rolled_back": rolled, "failed": failed, "needs_owner": needs,
            "skipped": [r for r in rows if r["skip"]], "unknown": unknown, "message": msg[1] if msg else None,
            **({"reported": reports} if reports else {})}


def apply_if_auto(c=None, is_busy=None, hourly=None, now: float | None = None) -> dict:
    """The timer: only in Automatic, and not while the agent is mid-run (next window tries again). It ticks every
    hour; with `wizard/app-updates-hourly` off only the first tick from 04:30 each day runs (wizard-os#613)."""
    from . import cli, updates
    c = c or cli.Config()
    if mode(c.home) != "auto":
        return {"app_mode": mode(c.home), "started": False}
    every_hour = hourly_on(c.home) if hourly is None else hourly
    if not every_hour:
        now = time.time() if now is None else now
        if not _nightly_due(c.home, now):
            return {"started": False, "skipped": "not-due", "note": "app updates run nightly (04:30)"}
        _write(c.home, nightly_at=now)
    if (is_busy or (lambda: updates.busy(updates.service_origin(c.home))))():
        return {"started": False, "skipped": "busy", "note": "a chat run is active; trying again next window"}
    return apply(c, quiet_repeats=every_hour, queue=False)


def start_detached(runner=subprocess.run, only: list[str] | None = None) -> dict:
    """The App Manager's Update apps button: the same run as the nightly timer, in the background, in any mode.
    `only` (Settings' Update now / Retry on one app) is `wam app-updates --apply <id>`."""
    wam = str(Path.home() / ".local/bin/wam")
    p = runner(["systemd-run", "--user", "--collect", f"--unit={UNIT}-now-{int(time.time())}", wam, "app-updates",
                "--apply", *(only or [])], capture_output=True, text=True, timeout=30)
    return {"started": p.returncode == 0, **({} if p.returncode == 0 else {"error": (p.stderr or p.stdout)[-300:]})}
