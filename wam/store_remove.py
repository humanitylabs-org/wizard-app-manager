"""Store "Remove app": uninstall one installed app from its App Store page.

Served by the Store listener (`store_install.py`) under `/wizard-os-home/store-install/_remove/`, so every request
already passed its gate: Tailscale Serve as the box owner, and for a POST also another tailnet device, the shell's
own origin, JSON and `X-Wizard-Store: 1`. App ids can't start with `_`, so these paths never shadow an app.

  GET  _remove/<app>/plan   what removing it does: exact installed identity, dependent apps, spots it empties,
                            and whether the Store may remove it at all (never Wizard's own apps or a required spot)
  POST _remove/<app>        {repo, commit, dependents, empties}: the identity and consequences the owner reviewed.
                            Anything that changed since the plan is refused; nothing is removed.
  GET  _remove/<app>        the job: removing | removed | failed | unknown

The removal itself is `wam uninstall` (cli.uninstall: the App Manager's lifecycle uninstall, which deletes the app's
app data and keeps earlier backups and any pre-migration Docker volume). Success is never taken from its return
value: the job reads the installed list back, and an app that is still listed is `failed` or `unknown`, never
`removed`. Only an exact registry record is removable: the installed recipe and apps.json must name the same
repository and full commit under the app's own id. Removals run one at a time and re-plan right before the
lifecycle call, so two removals can never empty a required spot between them. An `unknown` job keeps blocking Store
installs, updates and a second uninstall of that app until a later readback settles it as removed or failed.
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import store_install as si

ID = si.ID
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA = re.compile(r"[a-f0-9]{40}")
PREFIX = "_remove"
_jobs: dict[str, dict] = {}
# Wizard's own pieces stay out of reach of the Store even when no defaults row names them.
CORE = {"wizard-ux", "wizard-shell", "wizard-agent-service", "hermes-agent"}
BODY = {"repo", "commit", "dependents", "empties"}
# How long a finished uninstall may still read as `uninstalling` before the job reports `unknown`.
SETTLE_SECONDS = 30
# One lifecycle removal at a time: each re-plans against the apps still installed right before it runs.
_lifecycle = threading.Lock()
NO_APPS = "Wizard can't read its installed apps right now. Refresh and try again."
NO_LIST = "Wizard can't read its app list right now. Refresh and try again."
# Runtipi states that prove an uninstall is over and the app stayed. Anything else may still be uninstalling.
STAYED = {"running", "stopped"}


def removing(app_id: str, c=None) -> bool:
    """True while a removal runs or its outcome is still unknown. With `c`, an unknown outcome is read back first."""
    job = _jobs.get(app_id)
    if job and c is not None and job["state"] == "unknown":
        _settle(c, job)
    return bool(job and job["state"] in ("removing", "unknown"))


def _registry(c) -> dict | None:
    try:
        reg = json.loads((c.home / "apps.json").read_text())
    except (OSError, ValueError):
        return None
    return reg if isinstance(reg, dict) and isinstance(reg.get("apps"), list) else None


def _runtipi_status(c, app_id: str) -> str:
    from . import cli
    try:
        return str(((c.runtipi().app(f"{app_id}:{cli.STORE}") or {}).get("app") or {}).get("status") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _decide(app_id: str, name: str, installed_fn, status_fn, error: str = "") -> tuple[str, str]:
    """removed | failed | unknown, from a fresh readback. Never removed while the installed list still has it."""
    try:
        still = app_id in installed_fn()
    except Exception:  # noqa: BLE001
        return "unknown", f"Wizard couldn't confirm that {name} was removed. Refresh to check."
    if not still:
        return "removed", ""
    if status_fn(app_id) in STAYED:
        return "failed", (f"Not removed: {error}. {name} is still installed." if error
                          else f"Not removed. {name} is still installed.")
    return "unknown", f"Removing {name} hasn't finished yet. Refresh to check."


def _settle(c, job: dict) -> None:
    from . import cli
    if job["state"] != "unknown":
        return
    state, error = _decide(job["app"], job.get("name") or job["app"], lambda: cli.installed_ids(c.runtipi()),
                           lambda a: _runtipi_status(c, a))
    if state != "unknown":
        job.update(state=state, error=error) if error else (job.update(state=state), job.pop("error", None))
        job["settled"] = int(time.time())


def identity(c, app_id: str) -> dict | None:
    """Repository, commit and source from the installed recipe (never the prospective store copy)."""
    from . import cli
    d = c.runtipi_root / "apps" / cli.STORE / app_id
    try:
        src = json.loads((d / ".wizard-source").read_text())
    except (OSError, ValueError):
        return None
    repo = src.get("repo") if isinstance(src, dict) else None
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        return None
    try:
        commit = (d / ".wizard-commit").read_text().strip()
    except OSError:
        commit = ""
    if commit and not SHA.fullmatch(commit):
        return None
    recorded = src.get("source")
    source = recorded if isinstance(recorded, str) and ID.fullmatch(recorded) else "standard"
    if src.get("local") or repo.startswith("local/"):
        source = "local"
    return {"repo": repo, "commit": commit, "source": source}


def _protected(app: dict) -> bool:
    from . import slots
    return (app["id"] in CORE or app["id"] in slots.system_apps() or app.get("system") is True
            or "shell" in (app.get("provides") or []) or app.get("category") == "interface")


def plan(c, app_id: str) -> dict:
    """What removing `app_id` would do. Read-only."""
    from . import cli, slots
    if not ID.fullmatch(app_id or ""):
        raise ValueError("bad app id")
    reg = _registry(c)
    apps = {a.get("id"): a for a in (reg or {}).get("apps") or [] if isinstance(a, dict) and a.get("id")}
    rec = apps.get(app_id)
    app = rec or {"id": app_id}
    name = str(app.get("name") or app_id)
    out = {"app": app_id, "name": name, "installed": False, "removable": False, "protected": False, "reason": "",
           "identity": None, "dependents": [], "empties": [], "backups": None}
    if reg is None:   # without the registry nothing about spots, dependents or identity can be checked
        out["reason"] = NO_LIST
        return out
    try:
        installed = cli.installed_ids(c.runtipi())
    except Exception:  # noqa: BLE001 - the App Manager didn't answer: nothing is offered
        out["reason"] = NO_APPS
        return out
    if app_id not in installed:
        out["reason"] = f"{name} isn't installed on this Wizard."
        return out
    out["installed"] = True
    ident = out["identity"] = identity(c, app_id)
    defaults = slots.load_defaults()
    # Spots and dependents count only apps that are installed right now: a registry row for an app that is already
    # gone (or going) provides nothing, so it can never stand in for the last provider of a required spot.
    live = {**reg, "apps": [a for a in apps.values() if a["id"] in installed]}
    have = slots.providers(live)
    external = reg.get("external") or {}
    # Capabilities this app alone provides: removing it takes them away.
    alone = {s for s, ids in have.items() if ids == [app_id] and s not in external}
    out["empties"] = [{"slot": s, "name": str(defaults[s].get("name") or s)} for s in sorted(alone) if s in defaults]
    deps = set()
    for other in apps.values():
        if other["id"] == app_id or other["id"] not in installed:
            continue
        uses = set(other.get("uses") or [])
        if app_id in uses or uses & alone:
            deps.add(other["id"])
    for g in reg.get("grants") or []:
        if not isinstance(g, dict) or g.get("from") not in installed or g.get("from") == app_id:
            continue
        to = str(g.get("to") or "")
        if to == app_id or (to.startswith("slot:") and to[5:] in alone):
            deps.add(g["from"])
    out["dependents"] = sorted(({"id": d, "name": str(apps.get(d, {}).get("name") or d)} for d in deps),
                               key=lambda d: (d["name"].lower(), d["id"]))
    try:
        out["backups"] = len(c.runtipi().list_backups(f"{app_id}:{cli.STORE}"))
    except Exception:  # noqa: BLE001 - unknown is not zero; the Store then says nothing about backups
        out["backups"] = None
    blocked = slots.blocks_uninstall(live, app_id, defaults)
    out["protected"] = _protected(app)
    exact = (rec is not None and rec.get("app_id") in (None, app_id) and ident is not None
             and bool(SHA.fullmatch(ident["commit"])))
    if out["protected"]:
        out["reason"] = f"{name} is part of Wizard, so the Store doesn't remove it."
    elif blocked:
        needed = ", ".join(str(defaults[s].get("name") or s) for s in blocked)
        out["reason"] = f"Wizard needs {needed}. Install another app that provides it first."
    elif not exact:
        out["reason"] = f"Wizard can't confirm which source {name} came from, so it can't remove it here."
    elif str(rec.get("repo") or "").lower() != ident["repo"].lower() or rec.get("commit") != ident["commit"]:
        out["reason"] = f"Wizard's app list is out of date for {name}, so the Store can't remove it here."
    elif (si._jobs.get(app_id) or {}).get("state") == "installing":
        out["reason"] = f"{name} is being installed or updated. Try again when that finishes."
    else:
        out["removable"] = True
    return out


def _body(body) -> dict:
    if not isinstance(body, dict) or set(body) != BODY:
        raise ValueError("send exactly repo, commit, dependents and empties from the removal plan")
    repo, commit, deps, empties = (body[k] for k in ("repo", "commit", "dependents", "empties"))
    if not isinstance(repo, str) or not REPO.fullmatch(repo) or not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("invalid installed identity")
    if not isinstance(deps, list) or not all(isinstance(d, str) and ID.fullmatch(d) for d in deps) or len(deps) > 200:
        raise ValueError("invalid dependents")
    if not isinstance(empties, list) or not all(isinstance(s, str) and ID.fullmatch(s) for s in empties) or len(empties) > 64:
        raise ValueError("invalid spots")
    return {"repo": repo, "commit": commit, "dependents": sorted(set(deps)), "empties": sorted(set(empties))}


def start(c, app_id: str, body, *, uninstall_fn=None, installed_fn=None, status_fn=None) -> tuple[int, dict]:
    from . import cli
    if not ID.fullmatch(app_id or ""):
        raise ValueError("bad app id")
    want = _body(body)
    ident = {"repo": want["repo"], "commit": want["commit"]}
    busy = {"error": "another removal is already running for this app"}
    removing(app_id, c)   # an unknown outcome is read back before anything else is decided
    with si._lock:
        job = _jobs.get(app_id)
        if job and job["state"] in ("removing", "unknown"):
            return (202, job) if job["identity"] == ident else (409, busy)
    p = plan(c, app_id)
    if p["reason"] in (NO_APPS, NO_LIST):
        return 503, {"error": p["reason"]}
    if not p["installed"]:
        # A repeated request after it finished gets the same receipt; nothing runs again. Only while the app is
        # still gone: a later reinstall of the same release must never be answered with an old receipt.
        if job and job["state"] == "removed" and job["identity"] == ident:
            return 200, job
        return 404, {"error": p["reason"]}
    if not p["removable"]:
        return 409, {"error": p["reason"]}
    if {k: p["identity"][k] for k in ("repo", "commit")} != ident:
        return 409, {"error": f"{p['name']} changed since you opened it. Refresh and try again; nothing was removed."}
    if want["dependents"] != sorted(d["id"] for d in p["dependents"]) or want["empties"] != sorted(e["slot"] for e in p["empties"]):
        return 409, {"error": f"What removing {p['name']} affects has changed. Refresh and review it again; nothing was removed."}
    with si._lock:
        job = _jobs.get(app_id)
        if job and job["state"] in ("removing", "unknown"):
            return (202, job) if job["identity"] == ident else (409, busy)
        if (si._jobs.get(app_id) or {}).get("state") == "installing":
            return 409, {"error": f"{p['name']} is being installed or updated. Try again when that finishes."}
        job = _jobs[app_id] = {"app": app_id, "name": p["name"], "state": "removing", "identity": ident,
                               "started": int(time.time())}
    name = p["name"]
    uninstall_fn = uninstall_fn or (lambda c_, a: cli.uninstall(c_, a))
    installed_fn = installed_fn or (lambda: cli.installed_ids(c.runtipi()))
    status_fn = status_fn or (lambda a: _runtipi_status(c, a))

    def recheck() -> str:
        """Why this removal may no longer run, from a plan made now that every earlier removal has finished."""
        try:
            now = plan(c, app_id)
        except Exception as e:  # noqa: BLE001
            return f"Not removed: Wizard couldn't check {name} again ({type(e).__name__})."
        if not now["installed"] or not now["removable"]:
            return f"Not removed: {now['reason']}"
        if {k: now["identity"][k] for k in ("repo", "commit")} != ident:
            return f"Not removed: {name} changed since you opened it. Refresh and try again."
        if (want["dependents"] != sorted(d["id"] for d in now["dependents"])
                or want["empties"] != sorted(e["slot"] for e in now["empties"])):
            return f"Not removed: what removing {name} affects has changed. Refresh and review it again."
        return ""

    def run():
        with _lifecycle:
            refused = recheck()
            if refused:
                job.update(state="failed", error=refused, finished=int(time.time()))
                return
            error = ""
            try:
                uninstall_fn(c, app_id)
            except SystemExit as e:
                error = str(e)[:300]
            except Exception as e:  # noqa: BLE001 - report, never crash the API server
                error = f"{type(e).__name__}: {str(e)[:240]}"
            deadline = time.monotonic() + SETTLE_SECONDS
            while True:
                state, message = _decide(app_id, name, installed_fn, status_fn, error)
                if state != "unknown" or time.monotonic() >= deadline:
                    break
                time.sleep(2)
            job.update(state=state, **({"error": message} if message else {}))
            job["finished"] = int(time.time())

    threading.Thread(target=run, name=f"store-remove-{app_id}", daemon=True).start()
    return 202, job


def status(app_id: str, c=None) -> tuple[int, dict]:
    job = _jobs.get(app_id)
    if job and c is not None:
        _settle(c, job)
    return (200, job) if job else (404, {"error": "no removal for that app"})
