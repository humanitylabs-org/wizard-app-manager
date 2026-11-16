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
import time
import urllib.error
import urllib.request
from pathlib import Path

MODES = ("auto", "ask")
DEFAULT_MODE = "auto"
UNIT = "wizard-app-updates"
STATE = "app-updates.json"
LOCK = "app-updates.lock"
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


def _write(home: Path | None, **changes) -> dict:
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    s = {**_state(h), **changes}
    tmp = h / (STATE + ".tmp")
    tmp.write_text(json.dumps(s, indent=1) + "\n")
    tmp.replace(h / STATE)
    return s


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

SWITCH = "wizard/app-updates-list"   # the same off-by-default switch as the Apps list in Settings


def switch_on(home: Path | None = None) -> bool:
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home)


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
            "running": running_all(c.home)}


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
          reporter=None, moves=None, quiet_repeats: bool = False) -> dict:
    """Update every due app (or `only` those), one at a time, verifying each and rolling back failures.
    `quiet_repeats` (the hourly timer): a notice that would only repeat a "Waiting for you" already delivered in the
    last day is not sent again, so an app waiting for the owner's OK reminds once a day, not every hour."""
    from . import cli, updates
    c = c or cli.Config()
    h = _home(c.home)
    h.mkdir(parents=True, exist_ok=True)
    lock = open(h / LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"started": False, "skipped": "running", "note": "app updates are already running"}
    try:
        _clear_running(h)   # a run killed mid-update left its app behind (#733)
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
            if unknown and not renames:   # say what asking for it would do (a staging copy's move, wizard-os#628)
                from . import app_rename
                try:
                    moves = [d for d in app_rename.due(c, rt) if d["from"] in unknown]
                except Exception:  # noqa: BLE001 - a dry run never fails on the rename check
                    moves = []
                if moves:
                    out["moves"] = moves
                    out["unknown"] = [x for x in unknown if x not in {d["from"] for d in moves}]
            return out
        done, rolled, failed, needs = [], [], [], []
        renamed = []
        if not only or unknown:
            # An app its publisher renamed (wizard.md renamed_from) moves to its new id first, verified or rolled
            # back on its own; its update, if any, then runs under the new id on the next run. Asked for by id, an
            # app with no update of its own (a staging copy the released app claims, wizard-os#628) moves too.
            from . import app_rename
            try:
                moves = (renames(c, rt) if renames else app_rename.rename_due(c, rt, only=unknown or None))
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
            if renamed:
                moved = {r["id"] for r in renamed}
                rows = candidates(rt.installed(), store=cli.STORE, runtipi_root=c.runtipi_root, home=c.home)
                if only:   # an update asked for by id still touches only those apps (Codex P1)
                    rows = [r for r in rows if r["id"] in only]
                    for r in rows:
                        if r["skip"] in ("held", "failed", "ignored"):
                            r["skip"] = None
                due = [r for r in rows if not r["skip"] and r["id"] not in moved]
        fails = _failed(c.home)
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
        _write(c.home, failed=fails, last=last)
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
            for k, url in bugs.items():
                fails[k] = {**fails[k], "bug": url}
            _write(c.home, failed=fails)
        msg = _summary(done, rolled, failed, needs, renamed, told)
        if notify and msg and not repeat and _notify(c, *msg) and needs:
            _write(c.home, needs_told={"ids": sorted(last["needs_owner"]), "at": time.time()})
        return {"started": True, **({"repo_moved": repo_moved} if repo_moved else {}), "renamed": renamed, "updated": done, "rolled_back": rolled, "failed": failed, "needs_owner": needs,
                "skipped": [r for r in rows if r["skip"]], "unknown": unknown, "message": msg[1] if msg else None,
                **({"reported": reports} if reports else {})}
    finally:
        _clear_running(h)   # before the lock goes, so a reader never sees a finished run (#733)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


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
    return apply(c, quiet_repeats=every_hour)


def start_detached(runner=subprocess.run, only: list[str] | None = None) -> dict:
    """The App Manager's Update apps button: the same run as the nightly timer, in the background, in any mode.
    `only` (Settings' Update now / Retry on one app) is `wam app-updates --apply <id>`."""
    wam = str(Path.home() / ".local/bin/wam")
    p = runner(["systemd-run", "--user", "--collect", f"--unit={UNIT}-now-{int(time.time())}", wam, "app-updates",
                "--apply", *(only or [])], capture_output=True, text=True, timeout=30)
    return {"started": p.returncode == 0, **({} if p.returncode == 0 else {"error": (p.stderr or p.stdout)[-300:]})}
