"""Store "Start": the owner starts a stopped app from their own device (wizard-app-manager#711).

An update that outlasts the queue timeout (or any failed one) can leave an app recorded `stopped`; a stopped app
opens as a blank page and nothing the owner could tap started it again: only `wam app start` on the box did.
Served by the Store listener (`store_install.py`) under `/wizard-os-home/store-install/_start/<app>`, behind the
same owner gate as Store Get and Update (`StoreHandler._go`: the owner's tailnet login, another device, the shell's
own Origin, JSON with `X-Wizard-Store: 1`).

  POST _start/<app>  {}   start it in the background (202 + the job); a second tap joins the running job
  GET  _start/<app>       the job: {app, state: starting|running|failed, status, error?, started, finished?}

Only an installed app that is not running, and not while a Store install/update of it runs. It starts the app
through the App Manager API (`cli.app_lifecycle`, the path `wam app start` uses); nothing else changes.
"""
from __future__ import annotations

import threading
import time

from .store_install import ID

PATH = "_start"
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
STARTABLE = {"stopped", "start_error", "stop_error", "update_error", "restart_error"}


def _status(c, app_id: str) -> str | None:
    """Runtipi's status for the app, or None when it is not installed."""
    from .cli import STORE
    row = ((c.runtipi().app(f"{app_id}:{STORE}") or {}).get("app") or {})
    return row.get("status") if row else None


def start(c, app_id: str, body, lifecycle=None) -> tuple[int, dict]:
    if not ID.fullmatch(app_id or ""):
        return 400, {"error": "bad app id"}
    if body != {}:
        return 400, {"error": "send {}"}
    from . import store_install as si
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "starting":
            return 202, job
        other = si._jobs.get(app_id)
        if other and other.get("state") == "installing":
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
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == "starting":
            return 202, job
        job = _jobs[app_id] = {"app": app_id, "state": "starting", "status": status, "started": int(time.time())}

    def run():
        try:
            res = lifecycle(c, "start", app_id)
            ok = res.get("status") == "running" and not res.get("error")
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
