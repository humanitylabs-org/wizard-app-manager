"""Store "Start": the owner starts a stopped app from their own device (wizard-app-manager#711).

An update that outlasts the queue timeout (or any failed one) can leave an app recorded `stopped`; a stopped app
opens as a blank page and nothing the owner could tap started it again: only `wam app start` on the box did.
Served by the Store listener (`store_install.py`) under `/wizard-os-home/store-install/_start/<app>`, behind the
same owner gate as Store Get and Update (`StoreHandler._go`: the owner's tailnet login, another device, the shell's
own Origin, JSON with `X-Wizard-Store: 1`).

  POST _start/<app>  {}   start it in the background (202 + the job); a second tap joins the running job
  GET  _start/<app>       the job: {app, state: starting|running|failed, status, error?, started, finished?}

Only an installed app that is not running, and not while a Store install/update of it runs. A Store update of an app
Runtipi still reports as updating (or another transitional status) is refused too (409, `busy`), so a retry never
starts a second update on top of the first. It starts the app
through the App Manager API (`cli.app_lifecycle`, the path `wam app start` uses); nothing else changes.
"""
from __future__ import annotations

import threading
import time

from . import store_install as si
from .store_install import ID

PATH = "_start"
_jobs: dict[str, dict] = {}
# One lock with Store install/update: a start and an update of the same app are claimed atomically against each other
# (store_install.start checks `starting()` under the same lock), so they never run at once.
_lock = si._lock
STARTABLE = {"stopped", "start_error", "stop_error", "update_error", "restart_error"}


def _installing(app_id: str) -> bool:
    other = si._jobs.get(app_id)
    return bool(other and other.get("state") == "installing")


def starting(app_id: str) -> bool:
    """A Store Start of this app is running (store_install.start calls this while holding the shared lock)."""
    job = _jobs.get(app_id)
    return bool(job and job["state"] == "starting")


def _status(c, app_id: str) -> str | None:
    """Runtipi's status for the app, or None when it is not installed."""
    from .cli import STORE
    row = ((c.runtipi().app(f"{app_id}:{STORE}") or {}).get("app") or {})
    return row.get("status") if row else None


TRANSITIONAL = {"installing", "updating", "backing_up", "restoring", "starting", "stopping", "resetting", "uninstalling"}


def busy(c, app_id: str) -> str | None:
    """The transitional status Runtipi reports for the app (an update still running), or None. Unknown -> None: the
    Store update path keeps its own checks."""
    try:
        status = _status(c, app_id)
    except Exception:  # noqa: BLE001 - Runtipi unreadable: don't block on a guess
        return None
    return status if status in TRANSITIONAL else None


def start(c, app_id: str, body, lifecycle=None, check=None) -> tuple[int, dict]:
    """`lifecycle(c, verb, app)` starts it (default cli.app_lifecycle); `check(c, app) -> problem|None` then judges
    whether it stays up (default app_updates.check_started, #857: Runtipi says `running` while a container
    restart-loops). Tests that inject `lifecycle` pass their own `check` (or none)."""
    if not ID.fullmatch(app_id or ""):
        return 400, {"error": "bad app id"}
    if body != {}:
        return 400, {"error": "send {}"}
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "starting":
            return 202, job
        if _installing(app_id):
            return 409, {"error": "this app is being installed or updated; wait for that to finish"}
    status = _status(c, app_id)
    if status is None:
        return 404, {"error": f"{app_id} is not installed"}
    if status == "running":
        return 200, {"app": app_id, "state": "running", "status": "running", "note": "already running"}
    if status not in STARTABLE:
        return 409, {"error": f"{app_id} is {status}; wait for that to finish, then start it"}
    if lifecycle is None:
        from .cli import app_lifecycle as lifecycle
        if check is None:
            from .app_updates import check_started as check
    with _lock:   # re-check both registries and claim in one step
        job = _jobs.get(app_id)
        if job and job["state"] == "starting":
            return 202, job
        if _installing(app_id):
            return 409, {"error": "this app is being installed or updated; wait for that to finish"}
        job = _jobs[app_id] = {"app": app_id, "state": "starting", "status": status, "started": int(time.time())}

    def run():
        try:
            res = lifecycle(c, "start", app_id)
            ok = res.get("status") == "running" and not res.get("error")
            if ok and check is not None:
                problem = check(c, app_id)
                if problem:   # #857: it started, then keeps crashing: not "Started"
                    ok, res = False, {**res, "error": problem}
            job.update(state="running" if ok else "failed", status=res.get("status"),
                       **({} if ok else {"error": res.get("error") or f"finished as {res.get('status')}"}))
        except SystemExit as e:
            job.update(state="failed", error=str(e)[:400])
        except Exception as e:  # noqa: BLE001 - report, never crash the API server
            job.update(state="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        job["finished"] = int(time.time())

    threading.Thread(target=run, name=f"store-start-{app_id}", daemon=True).start()
    return 202, job


def status(app_id: str) -> tuple[int, dict]:
    job = _jobs.get(app_id)
    return (200, job) if job else (404, {"error": "no start for that app"})
