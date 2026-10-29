"""Opt-in quiet restarts. The existing maintenance runner owns scheduling and its lease.

Activity probes are fail-closed but not an admission fence: enabling automation still
requires the attended acceptance gate. No new work-admission contract is invented here.
"""
from __future__ import annotations

import time
from pathlib import Path

OPTIONS = {"quiet_restarts": ("off", "auto"), "held_platform": ("off", "patch"), "quiet_reboot": ("off", "auto")}
BAD = ("error", "refused")


def safe_step(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return {"state": "error", "reason": str(exc)[-600:]}


def reconcile(home: Path, snapshot: dict | None = None) -> dict:
    from . import maintain as m
    path = home / "maintenance/reboot.json"
    rec = m._load(path)
    if rec.get("state") not in ("intent", "scheduled"):
        return rec
    # Read-only status never invokes needrestart or schedules privileged work.
    from .updates import boot_id
    boot = (snapshot or {}).get("boot_id") or boot_id()
    if boot and boot != rec.get("boot_id"):
        rec.update(state="rebooted", finished_at=m._now(), new_boot_id=boot)
    elif time.time() - rec.get("requested_epoch", time.time()) > 300:
        rec.update(state="error", reason="The requested reboot did not happen within five minutes.")
    else:
        return rec
    m._save(path, rec)
    return rec


def run(home: Path, os_result: dict, runner, wait: float, is_busy, sleep) -> dict:
    from . import maintain as m
    cfg = m.settings(home)
    probe = is_busy or (lambda: m.busy_reasons(home))

    def inspect():
        return safe_step(m.call_helper, ["restart-check"], runner)

    def act(action):
        # Reconfirm before EVERY operation, including each service, not just once per run.
        reasons = m.wait_quiet(home, wait, probe, sleep)
        if not reasons:
            reasons = probe()
        if reasons:
            return {"state": "deferred", "reason": " ".join(reasons)}
        return m.call_helper(action, runner)

    out = {"held": {"state": "report_only", "pending": os_result.get("held", [])},
           "services": {"state": "report_only", "results": []}, "reboot": {"state": "report_only"}}
    snapshot = inspect()
    out["before"] = snapshot
    previous = reconcile(home, snapshot)
    if cfg["held_platform"] == "patch" and cfg["os_security"] == "patch":
        out["held"] = safe_step(act, ["apply-held"])
    elif cfg["held_platform"] == "patch":
        out["held"]["reason"] = "Held updates also require os-security patch; nothing installed."

    # Refresh after a platform update; its changed libraries can add services.
    if out["held"].get("state") == "applied":
        snapshot = inspect()
    out["services"]["pending"] = snapshot.get("services", [])
    if cfg["quiet_restarts"] == "auto":
        if snapshot.get("state") in BAD + ("report_only",):
            out["services"] = {**snapshot, "available": False}
        else:
            results = [safe_step(act, ["restart-service", name]) for name in snapshot.get("services", [])]
            state = ("error" if any(r.get("state") in BAD for r in results) else
                     "deferred" if any(r.get("state") == "deferred" for r in results) else
                     "pending" if any(r.get("state") == "report_only" for r in results) else "current")
            out["services"].update(state=state, results=results)
    elif snapshot.get("state") in BAD + ("report_only",):
        out["services"].update(state=snapshot["state"], reason=snapshot.get("reason", ""), available=False)

    snapshot = inspect() if any(cfg[k] != "off" for k in ("quiet_restarts", "held_platform")) else snapshot
    out["after"] = snapshot
    if snapshot.get("state") in BAD:
        out["services"].update(state="error", reason=snapshot.get("reason", "Restart verification failed."))
    if "pending" in out["services"]:
        out["services"]["pending"] = snapshot.get("services", out["services"]["pending"])
    required = snapshot.get("reboot_required", os_result.get("reboot_required", False))
    out["reboot"]["required"] = required
    if previous:
        out["reboot"]["receipt"] = previous
    if cfg["quiet_reboot"] == "auto" and required:
        if snapshot.get("state") in BAD or any(out[k].get("state") in BAD for k in ("held", "services")):
            out["reboot"].update(state="deferred", reason="A restart step failed; preserving the box for diagnosis.")
        elif previous.get("state") == "rebooted" and previous.get("new_boot_id") == snapshot.get("boot_id"):
            out["reboot"].update(state="error", reason="A reboot already completed but another is still required; inspect before retrying.")
        elif previous.get("state") in ("intent", "scheduled"):
            out["reboot"].update(state="scheduled", receipt=previous)
        else:
            def reboot():
                # Probe before persisting intent AND once more at the privileged call boundary.
                reasons = m.wait_quiet(home, wait, probe, sleep)
                if reasons:
                    return {"state": "deferred", "required": True, "reason": " ".join(reasons)}
                rec = {"state": "intent", "boot_id": snapshot.get("boot_id"), "requested_at": m._now(),
                       "requested_epoch": time.time()}
                if not rec["boot_id"]:
                    return {"state": "error", "reason": "Cannot persist reboot intent without a boot ID."}
                m._save(home / "maintenance/reboot.json", rec)
                reasons = probe()
                res = {"state": "deferred", "reason": " ".join(reasons)} if reasons else m.call_helper(["reboot"], runner)
                rec.update(res)
                m._save(home / "maintenance/reboot.json", rec)
                return {**res, "required": True, "receipt": rec}
            out["reboot"] = safe_step(reboot)
    elif not required and previous.get("state") == "rebooted":
        out["reboot"].update(state="rebooted")
    return out


def items(home: Path, last: dict) -> list[dict]:
    lane = last.get("restarts") or {}
    if not lane:
        return []
    if lane.get("state") in BAD:
        return [{"key": "maintenance-services", "label": "Service restarts", "status": "error",
                 "value": "check failed", "detail": str(lane.get("reason", ""))[:600]}]
    rows = []
    for key, label in (("held", "Platform packages"), ("services", "Service restarts"), ("reboot", "System reboot")):
        doc = lane.get(key) or {}
        state = doc.get("state", "report_only")
        value = state.replace("_", " ")
        status = "error" if state in BAD else "warn" if state in ("deferred", "scheduled", "report_only", "pending") else "ok"
        detail = str(doc.get("reason") or "")
        if key == "services":
            failures = [r for r in doc.get("results", []) if r.get("state") in BAD]
            if failures:
                detail = "; ".join(str(r.get("service", "service")) + ": " + str(r.get("reason", "failed")) for r in failures)
            elif doc.get("pending"):
                detail = ", ".join(doc["pending"])
            elif state == "report_only" and doc.get("available") is not False:
                value, status = "report only", "ok"
        if key == "held" and doc.get("pending"):
            detail = ", ".join(p["name"] for p in doc["pending"])
        if key == "reboot":
            rec = reconcile(home)
            if rec.get("state") == "error":
                status, value, detail = "error", "reboot failed", rec.get("reason", "")
            elif rec.get("state") == "rebooted" and state in ("scheduled", "rebooted", "report_only"):
                status, value, detail = "ok", "rebooted", "Boot ID changed; system health is reported separately."
            elif doc.get("required"):
                detail = detail or "A reboot is required; automatic reboots are off." if state == "report_only" else detail
            elif state == "report_only":
                status, value = "ok", "not required"
        rows.append({"key": "maintenance-" + key, "label": label, "status": status, "value": value, "detail": detail[:600]})
    return rows
