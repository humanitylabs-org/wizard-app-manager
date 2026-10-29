"""Restore defaults: app-to-app links back to exactly what the apps are designed to have.

Designed links are the ones an app declares in its wizard.md (`uses:` an app or a slot, `requires:` models/services)
or asks for at runtime; the App Manager approves them by itself (wam/connections.py, wizard-app-grants.ts). The owner's
own decisions sit on top: a link he cut (a `denied` row) and a link he added or turned back on (an `approved: owner`
grant). Restore defaults drops exactly those owner rows, then reconciles live, so:

- a designed link he cut is turned back on (or, to an app set to "Ask me first", waits for his OK again);
- a link he added is removed (one he added and later cut disappears from the list);
- nothing else changes: requests waiting for his OK, Ask me first, the box's mode, file shares and agent access.

`plan()` says what will change in plain words (the confirm dialog and `wam grants reset` show it first); `apply()` makes
the change through the App Manager's owner API (`POST /apps/wizard/connections-reset`) and, on an App Manager that
predates it, by editing its wizard-grants.json the same way under the same rule (only when this user may write it),
followed by a reconcile of the box's current mode. The owner listener (`POST api/connections/restore`) and
`wam grants reset` are the only callers: owner only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

RESET_PATH = "/apps/wizard/connections-reset"


def _app_rows(state: dict | None, key: str) -> list[dict]:
    return [g for g in ((state or {}).get(key) or []) if isinstance(g, dict) and g.get("kind", "app") == "app"
            and isinstance(g.get("from"), str) and isinstance(g.get("to"), str)]


def owner_rows(state: dict | None) -> tuple[list[dict], list[dict]]:
    """The owner's own app-to-app decisions: (grants he made, links he cut)."""
    return ([g for g in _app_rows(state, "grants") if g.get("approved") == "owner"], _app_rows(state, "denied"))


def _label(r: dict | None, frm: str, to: str, names: dict) -> str:
    if r and r.get("label"):
        return str(r["label"])
    nm = lambda x: names.get(x) or str(x).replace("slot:", "")  # noqa: E731
    return f"{nm(frm)} → {nm(to)}"


def plan(state: dict | None, report: dict | None) -> list[dict]:
    """What Restore defaults changes, one item per link: {from, to, label, change, text}. change: "on" (a designed
    link he cut comes back), "ask" (it comes back as a request waiting for his OK: its app asks first), "remove" (a
    link he added goes; `off` when he had already cut it). A designed link he merely turned back on is unchanged and
    not listed."""
    controls = {(r.get("from"), r.get("to")): r for r in (report or {}).get("controls") or [] if isinstance(r, dict)}
    names = {a.get("id"): a.get("name") or a.get("id") for a in (report or {}).get("apps") or [] if isinstance(a, dict)}
    sensitive = set((state or {}).get("sensitive") or [])
    grants, denied = owner_rows(state)

    def designed(g: dict) -> bool:
        r = controls.get((g["from"], g["to"]))
        if r is not None:
            return r.get("origin") in ("declared", "requested")
        return g.get("reason") in ("uses", "requires", "request")

    def asks(g: dict) -> bool:
        r = controls.get((g["from"], g["to"])) or {}
        provs = r.get("providers") or [g["to"].removeprefix("slot:")]
        return any(p in sensitive for p in provs)

    out: list[dict] = []
    seen: set = set()
    for g, cut in [*[(g, True) for g in denied], *[(g, False) for g in grants]]:
        k = (g["from"], g["to"])
        if k in seen:
            continue
        seen.add(k)
        label = _label(controls.get(k), g["from"], g["to"], names)
        if designed(g):
            if asks(g):
                out.append({"from": k[0], "to": k[1], "label": label, "change": "ask",
                            "text": f"{label} will wait for your OK again."})
            elif cut:
                out.append({"from": k[0], "to": k[1], "label": label, "change": "on",
                            "text": f"{label} will be turned back on."})
        else:
            out.append({"from": k[0], "to": k[1], "label": label, "change": "remove", **({"off": True} if cut else {}),
                        "text": f"{label}, which you added and turned off, will be removed." if cut
                        else f"{label}, which you added, will be removed."})
    return sorted(out, key=lambda x: ({"on": 0, "ask": 1, "remove": 2}[x["change"]], x["label"]))


def reset_state(state: dict) -> dict:
    """The rule on a wizard-grants.json document (the App Manager's own copy is resetToDefaults in
    wizard-app-grants.ts): drop the owner's app grants and every app-to-app Disconnect. Shares and the rest stay."""
    out = dict(state)
    out["grants"] = [g for g in state.get("grants") or [] if not (isinstance(g, dict) and g.get("kind", "app") == "app"
                                                                  and g.get("approved") == "owner")]
    out["denied"] = [g for g in state.get("denied") or [] if not (isinstance(g, dict) and g.get("kind", "app") == "app")]
    return out


def _file_reset(c, rt) -> dict:
    """An App Manager without the reset API: the same rule on its state file (only when this user may write it),
    then the box's current mode again, which makes the App Manager reconcile and approve the designed links."""
    from . import network
    f = Path(c.runtipi_root) / "state" / "wizard-grants.json"
    if not os.access(f, os.R_OK | os.W_OK):
        raise PermissionError("this App Manager can't restore defaults yet: update it, then try again")
    before = f.stat()
    doc = json.loads(f.read_text())
    if doc.get("schema") != 1 or not isinstance(doc.get("grants"), list) or not isinstance(doc.get("denied"), list):
        raise ValueError("the App Manager's connection file isn't one this wam knows")
    new = reset_state(doc)
    tmp = f.with_name(f".{f.name}.reset-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(new, indent=2) + "\n")
    now = f.stat()
    if (now.st_mtime_ns, now.st_size, now.st_ino) != (before.st_mtime_ns, before.st_size, before.st_ino):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the connections changed while restoring; try again")
    os.replace(tmp, f)
    mode = network.connection_mode(network.connection_state(c, rt))
    return rt.req("POST", "/apps/wizard/connection-mode", {"mode": mode})


def plan_id(changes: list[dict]) -> str:
    """A short fingerprint of a plan: the confirm dialog sends back the one it showed, so a restore never applies
    changes the owner didn't see (another tab or `wam grants` changed the links while the dialog was open)."""
    import hashlib
    rows = sorted((str(x.get("from")), str(x.get("to")), str(x.get("change"))) for x in changes)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


class PlanChanged(ValueError):
    """The links changed after the owner saw what Restore defaults would do."""


def apply(c, rt, reconcile=None, expect: str | None = None) -> dict:
    """Restore defaults now. Returns {"state": the App Manager's connection state, "changed": plan before}.
    `reconcile`: the live network step (wam serve's watcher also applies it within seconds). `expect`: the plan_id the
    owner confirmed; when the plan is no longer that one, nothing changes (PlanChanged)."""
    from . import network
    from .runtipi import RuntipiError
    before = network.connection_state(c, rt)
    changes = plan(before, network.report(c, rt))
    if expect is not None and plan_id(changes) != expect:
        raise PlanChanged("The links changed since you looked. Nothing was restored; check the list and try again.")
    try:
        st = rt.req("POST", RESET_PATH, {})
    except RuntipiError as e:
        if "-> 404" not in str(e):
            raise
        st = _file_reset(c, rt)
    if reconcile:
        reconcile()
    return {"state": st, "changed": changes}
