"""Automatic app updates: every box gets the new version of its apps soon after a publisher ships it.

A Wizard OS release only moves the system apps it pins (shell, Agent Service, Hermes, release defaults). Every other
app installed from the Wizard App Index stayed on the version it was installed at until someone ran
`wam app update <id>` by hand, so publishing a fix reached new installs only. This module closes that gap:

- check: Runtipi's own update check (the index store re-reads each installed app's listing; a new commit is always a
  newer tipi_version) lists every installed app with a newer version;
- apply: updates them one at a time through the normal `app_update` path (Runtipi backup first, the same owner gate
  for new host access, networks reconciled), then checks the app really works: it is back in the state it was
  (running or stopped), its version moved, no container is restarting or fewer containers run than before, and its
  declared health URL or MCP endpoint answers. Anything else is rolled back from the backup the update just made,
  and that version is not retried until the publisher ships a newer one;
- a nightly systemd user timer (wizard-app-updates.timer) runs apply when the app-update mode is "auto";
- the owner is told what changed through the Notifications app when it is installed.

The app-update mode is separate from the Wizard OS update mode (updates.py): system releases default to Ask me
(James, alpha), app updates default to Automatic because each one is small, backed up and rolled back on failure.
Owners can switch to "ask" (`wam app-updates --ask`) or hold single apps (`--hold <id>`).

Never touched here: the shell, the Agent Service, Hermes and anything the Wizard OS release pins (they move with the
release), apps prepared from an exact owner-local selection (.wizard-selection), and versions the owner ignored.

State lives in <wizard-os home>/app-updates.json: {"mode", "holds", "failed": {id: tipi}, "last": {...}}.
"""
from __future__ import annotations

import fcntl
import json
import os
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
SETTLE_SECONDS = 20          # an app must stay up this long after its update before it counts as working
PROBE_SECONDS = 90           # how long a declared health URL / MCP endpoint may take to answer after the update
FRESH_SLACK_MS = 120_000     # the rollback backup must be at least this recent relative to the update start
RETRY_DAYS = 7               # a version that failed is tried again after this long (a box-side fix may land meanwhile)
# Moved only by a Wizard OS release (updates.py / release_updates), never by this module.
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


# ---------------------------------------------------------------- nightly timer

def _timer_files(unit_dir: Path, wam: str) -> dict[Path, str]:
    return {
        unit_dir / f"{UNIT}.service": (
            "[Unit]\nDescription=Wizard automatic app updates (owner setting: app updates Automatic)\n\n"
            "[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n"
            f"TimeoutStartSec=6h\nExecStart={wam} app-updates --apply-if-auto\n"),
        unit_dir / f"{UNIT}.timer": (
            "[Unit]\nDescription=Nightly Wizard app update check\n\n"
            "[Timer]\nOnCalendar=*-*-* 04:30\nRandomizedDelaySec=1h\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def _systemctl(enabled: bool):
    if not enabled:
        return lambda *a: None
    return lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)


def ensure_timer(home: Path | None = None, unit_dir: Path | None = None, systemctl: bool = True) -> dict:
    """Automatic: the nightly timer is installed and enabled. Ask me: removed. Called by the installer on every run
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
        if not app_id or slug != store or latest <= have:
            continue
        skip = None
        if app_id in SYSTEM_APPS or app_id in managed:
            skip = "system"            # moves with the Wizard OS release
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


def status(c=None) -> dict:
    from . import cli
    c = c or cli.Config()
    rows = candidates(c.runtipi().installed(), store=cli.STORE, runtipi_root=c.runtipi_root, home=c.home)
    return {"app_mode": mode(c.home), "holds": holds(c.home), "due": [r for r in rows if not r["skip"]],
            "skipped": [r for r in rows if r["skip"]], "last": _state(c.home).get("last")}


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
    required variable is unset ("Variable ... is required"), so this runs first. Returns what only the owner can
    give (then nothing is changed)."""
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


def _summary(done: list, rolled: list, failed: list, needs: list) -> tuple[str, str, str] | None:
    """The owner's notice. Apps are named as the owner sees them (Movies), never by internal id (cinequeue)."""
    if not (done or rolled or failed or needs):
        return None

    def nm(r: dict) -> str:
        return r.get("name") or r["id"]

    parts = []
    if done:
        parts.append("Updated " + ", ".join(f"{nm(d)} ({d['to_version'] or d['to']})" for d in done) + ".")
    if rolled:
        parts.append("Kept the previous version of " + ", ".join(nm(r) for r in rolled)
                     + " because the new one did not work; it is tried again when a newer version ships.")
    if needs:
        parts.append("Waiting for you: " + "; ".join(f"{nm(n)}: {n['reason']}" for n in needs) + ".")
    if failed:
        parts.append("Could not update " + "; ".join(f"{nm(f)}: {f['reason']}" for f in failed)
                     + ". Ask your Wizard to look at it.")
    bad = rolled or failed or needs
    title = "App updates need a look" if bad else (f"{len(done)} app{'s' if len(done) != 1 else ''} updated")
    return title, " ".join(parts), "high" if failed else "default"


def apply(c=None, *, only: list[str] | None = None, dry_run: bool = False, notify: bool = True) -> dict:
    """Update every due app (or `only` those), one at a time, verifying each and rolling back failures."""
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
        if updates.installing(c.home):
            return {"started": False, "skipped": "installing", "note": "a Wizard OS update is running; next window"}
        rt = c.runtipi()
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
            return {"dry_run": True, "due": due, "skipped": [r for r in rows if r["skip"]], "unknown": unknown}
        done, rolled, failed, needs = [], [], [], []
        fails = _failed(c.home)
        for r in due:
            urn = f"{r['id']}:{cli.STORE}"
            states = _containers(urn)
            before = {**r, "running": states.count("running"), "endpoint_ok": baseline(c, rt, r["id"], r["status"])}
            started_ms = int(time.time() * 1000)
            print(f"updating {r['id']} {r['from']} -> {r['to']}", file=sys.stderr, flush=True)
            try:
                missing = reconcile_settings(c, rt, r["id"])
                if missing:   # the new version needs a setting only the owner can give: nothing changed
                    needs.append({"id": r["id"], "reason": "the new version needs " + "; ".join(missing)[:200]})
                    continue
                res = cli.app_update(c, r["id"])
            except SystemExit as e:   # owner gate (new host access) or not installed: nothing changed
                needs.append({"id": r["id"], "reason": str(e).splitlines()[0][:200]})
                continue
            except Exception as e:  # noqa: BLE001 - one app never stops the others
                res = {"error": f"{type(e).__name__}: {e}"[:300], "status": None}
            problem = verify(c, rt, urn, r["id"], res, before)
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
                failed.append({**r, "reason": problem + " (the previous version still runs)", "rollback": None})
                continue
            print(f"{r['id']}: {problem}; rolling back", file=sys.stderr, flush=True)
            try:
                rb = rollback(c, rt, urn, started_ms, before)
            except Exception as e:  # noqa: BLE001
                rb = {"rolled_back": False, "error": f"{type(e).__name__}: {e}"[:300]}
            (rolled if rb.get("rolled_back") else failed).append(
                {**r, "reason": problem if rb.get("rolled_back") else f"{problem}; {rb.get('error')}", "rollback": rb})
        try:
            cli.write_registry(c, rt)
        except Exception:  # noqa: BLE001
            pass
        last = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "updated": [d["id"] for d in done], "rolled_back": [x["id"] for x in rolled],
                "failed": [x["id"] for x in failed], "needs_owner": [x["id"] for x in needs]}
        _write(c.home, failed=fails, last=last)
        msg = _summary(done, rolled, failed, needs)
        if notify and msg:
            _notify(c, *msg)
        return {"started": True, "updated": done, "rolled_back": rolled, "failed": failed, "needs_owner": needs,
                "skipped": [r for r in rows if r["skip"]], "unknown": unknown, "message": msg[1] if msg else None}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def apply_if_auto(c=None, is_busy=None) -> dict:
    """The nightly timer: only in Automatic, and not while the agent is mid-run (next window tries again)."""
    from . import cli, updates
    c = c or cli.Config()
    if mode(c.home) != "auto":
        return {"app_mode": mode(c.home), "started": False}
    if (is_busy or (lambda: updates.busy(updates.service_origin(c.home))))():
        return {"started": False, "skipped": "busy", "note": "a chat run is active; trying again next window"}
    return apply(c)


def start_detached(runner=subprocess.run) -> dict:
    """The App Manager's Update apps button: the same run as the nightly timer, in the background, in any mode."""
    wam = str(Path.home() / ".local/bin/wam")
    p = runner(["systemd-run", "--user", "--collect", f"--unit={UNIT}-now-{int(time.time())}", wam, "app-updates",
                "--apply"], capture_output=True, text=True, timeout=30)
    return {"started": p.returncode == 0, **({} if p.returncode == 0 else {"error": (p.stderr or p.stdout)[-300:]})}
