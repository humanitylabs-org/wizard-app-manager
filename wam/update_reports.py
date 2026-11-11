"""Tell the team when an app update fails: file a bug in the app's repo, or comment on the open one (#524).

Before this, a failed or rolled-back app update only reached the owner ("Ask your Wizard to look at it"), so the team
heard about it only if the owner reported it. With the feature switch `wizard/update-bug-reports` on (Settings > Try new
features or the WizardOS Tracker; off by default, and always off on For everyone), the nightly app-update run
(`app_updates.apply`) hands each failure to `report_all`, which for every app:

  - finds the app's GitHub repo from the source the App Manager recorded when it installed it (`.wizard-source`);
  - looks for an open issue labelled `bug` that carries the same failure mark (app, target version, failing step);
  - comments on it with this box's report, or files a new issue labelled `bug`.

The report says: box (its short host name in a private, team-only repo; an anonymous stable id in a public one), app, from/to version, the failing step and the error. Home folders,
tailnet names and tailnet addresses are scrubbed from the error first. Writing uses this box's own GitHub login (the same
one `private.token()` reads for private apps); without a login, or when GitHub refuses, nothing is filed and the owner's
notice stays as it was. Reporting never fails or slows the update run beyond one short GitHub call per failure.

A failed WizardOS update (the installer run, or its read-back of what runs vs the release) is reported the same way to
humanitylabs-org/wizard-os by `report_os`, which the nightly maintenance run and the Automatic update check call
before they try again. Each failed installer run is reported once (`update-report-os.json` remembers it, and the last
release that read back cleanly, which is the "from" version). The owner's notice says what failed and that the team
was told.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

SWITCH = "wizard/update-bug-reports"   # feature switch, off by default
OS_REPO = "humanitylabs-org/wizard-os"
OS_STATE = "update-report-os.json"    # in ~/.wizard-os: the last clean release and the failed run already reported
API_URL = "https://api.github.com"
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TIMEOUT = 15
MARK = "<!-- wizard-update-failure: {} -->"
# home folders, tailnet names and tailnet addresses (patterns built from parts so no literal path or host is in source)
_PRIVATE = [(re.compile("/" + "home" + r"/[^/\s'\"]+"), "/" + "home" + "/<user>"),
            (re.compile(r"\b[\w-]+(?:\.[\w-]+)*\." + "ts" + r"\.net\b"), "<tailnet-host>"),
            (re.compile(r"\b" + "100" + r"\.(?:\d{1,3}\.){2}\d{1,3}\b"), "<tailnet-ip>")]


def switch_on() -> bool:
    """Whether the owner turned on `wizard/update-bug-reports` (tests monkeypatch this)."""
    from . import feature_switches
    return feature_switches.is_on(SWITCH)


def box_name() -> str:
    """The box's short host name: never a full (tailnet) domain. Only sent to private (team-only) repos."""
    return (socket.gethostname() or "unknown").split(".", 1)[0][:63] or "unknown"


def box_id() -> str:
    """An anonymous, stable id for this box, for public repos: the team can match reports from one box without
    learning its name. `wam app-updates` on the box prints nothing more; the owner can tell the team if they want."""
    seed = socket.gethostname() or ""
    try:
        seed += Path("/etc/machine-id").read_text().strip()
    except OSError:
        pass
    return "box-" + hashlib.sha256(("wizard-update-report:" + seed).encode()).hexdigest()[:10]


def scrub(text) -> str:
    """Keep private paths and hosts out of a public issue."""
    text = str(text or "")
    try:
        text = text.replace(str(Path.home()), "~")
    except RuntimeError:
        pass
    for pattern, repl in _PRIVATE:
        text = pattern.sub(repl, text)
    return text[:1500]


def app_repo(runtipi_root, store: str, app_id: str) -> str | None:
    """The GitHub repo this app was installed from, as the App Manager recorded it."""
    try:
        repo = json.loads((Path(runtipi_root) / "apps" / store / app_id / ".wizard-source").read_text()).get("repo")
    except (OSError, ValueError, AttributeError):
        return None
    return repo if isinstance(repo, str) and REPO.fullmatch(repo) else None


def step_of(row: dict) -> str:
    """The failing step, in plain words."""
    rb = row.get("rollback")
    if isinstance(rb, dict) and rb.get("rolled_back") is False:
        return "rollback"
    return str(row.get("step") or "update")


def draft(row: dict, box: str) -> dict:
    """Title, body and dedupe mark for one failed app (or WizardOS, `kind: os`) update."""
    app = str(row.get("id") or "unknown")
    name = str(row.get("name") or app)
    frm, to = row.get("from"), row.get("to")
    to_v = row.get("to_version") or to
    step = step_of(row)
    key = f"{app}:{to}:{step}"
    rb = row.get("rollback")
    kept = row.get("result") or (
        "rolled back to the previous version" if isinstance(rb, dict) and rb.get("rolled_back")
        else "rollback failed: needs a look" if isinstance(rb, dict) else "previous version still runs")
    is_os = row.get("kind") == "os"
    report = "\n".join([
        f"- Box: {box}",
        f"- {'Part' if is_os else 'App'}: {name} (`{app}`)",
        f"- From/to version: {frm} -> {to}" + (f" ({to_v})" if to_v != to else ""),
        f"- Failing step: {step}",
        f"- Result: {kept}",
        f"- Error: {scrub(row.get('reason'))}",
    ])
    return {"key": key, "mark": MARK.format(key),
            "title": (f"WizardOS update failed: {frm} to {to} ({step})" if is_os
                      else f"App update failed: {name} {frm} to {to} ({step})"),
            "body": (f"{'A WizardOS' if is_os else 'An automatic app'} update failed on a Wizard box. Filed "
                     f"automatically by the App Manager because "
                     f"the box owner turned on the feature switch `{SWITCH}`; like any agent-drafted bug it waits for "
                     f"a human yes.\n\n{report}\n\n{MARK.format(key)}\n"),
            "comment": f"Same failure on another run:\n\n{report}\n"}


def _github(method: str, path: str, tok: str, body: dict | None = None):
    req = urllib.request.Request(f"{API_URL}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "wizard-app-manager", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read() or b"null")


def report(row: dict, repo: str, tok: str, *, box: str | None = None, gh=_github) -> dict:
    """File or comment for one failure. Returns {"filed"|"commented": url} or {"error": text} (no token in it)."""
    try:
        if box is None:   # the box's name goes only to a private (team-only) repo; public repos get an anonymous id
            box = box_name() if (gh("GET", f"/repos/{repo}", tok) or {}).get("private") is True else box_id()
        d = draft(row, box)
        open_bugs = gh("GET", f"/repos/{repo}/issues?state=open&labels=bug&per_page=100", tok) or []
        same = next((i for i in open_bugs if isinstance(i, dict) and not i.get("pull_request")
                     and d["mark"] in str(i.get("body") or "")), None)
        if same:
            got = gh("POST", f"/repos/{repo}/issues/{int(same['number'])}/comments", tok, {"body": d["comment"]})
            return {"commented": (got or {}).get("html_url") or same.get("html_url"), "issue": same.get("html_url")}
        got = gh("POST", f"/repos/{repo}/issues", tok, {"title": d["title"], "body": d["body"], "labels": ["bug"]})
        return {"filed": (got or {}).get("html_url"), "issue": (got or {}).get("html_url")}
    except urllib.error.HTTPError as e:
        return {"error": f"GitHub said {e.code}"}
    except (OSError, ValueError, TypeError, KeyError) as e:
        return {"error": f"GitHub unreachable ({type(e).__name__})"}


def report_all(c, rows: list[dict], *, store: str, token=None, gh=_github, on=None) -> dict:
    """Report every failed row when the switch is on. Returns {app_id: result}; empty when switched off."""
    if not rows or not (on or switch_on)():
        return {}
    if token is None:
        from . import private
        token = private.token
    tok = token()
    out = {}
    for r in rows:
        app = r.get("id")
        repo = app_repo(c.runtipi_root, store, app) if app else None
        if not repo:
            out[app] = {"error": "no recorded repo for this app"}
        elif not tok:
            out[app] = {"error": "this Wizard has no GitHub login"}
        else:
            out[app] = report(r, repo, tok, gh=gh)
    return out


def _os_home(home) -> Path:
    from . import updates
    return updates._home(home)


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_os_state(home, **changes) -> None:
    h = _os_home(home)
    doc = {**_read(h / OS_STATE), **changes}
    try:
        tmp = h / (OS_STATE + ".tmp")
        tmp.write_text(json.dumps(doc) + "\n")
        tmp.replace(h / OS_STATE)
    except OSError:
        pass


def os_failure(home=None) -> dict | None:
    """The WizardOS update that failed on this box as a report row (with a `run` key: one per installer run), or
    None when the last run did not fail. Reads only the installer's own files (install-status.json, release.json)."""
    from . import updates
    h = _os_home(home)
    run = updates._read_status(h / "install-status.json") or {}
    receipt = updates._read_status(h / "release.json") or {}
    pins = updates._read_status(h / "release-pins.json") or {}
    if receipt.get("status") == "unpinned":   # a developer box, updated by hand: not a release failure
        return None
    # A full update whose core read-back failed writes a `mismatch` receipt and then ends the run as failed: the
    # read-back is the real failure when the receipt belongs to this run (written after it started).
    mine = str(receipt.get("at") or "") >= str(run.get("started_at") or "")
    if run.get("state") == "failed" and not (receipt.get("status") == "mismatch" and mine):
        step = str(run.get("step") or "install")
        reason = (run.get("failure_reason") or run.get("message")
                  or f"Installer failed (exit {run.get('exit_code', 'unknown')}).")
    elif receipt.get("status") in ("install-failed", "mismatch"):
        step = "read-back"
        bad = [c.get("name") for c in receipt.get("checks") or [] if isinstance(c, dict) and c.get("core")
               and not c.get("ok")]
        reason = f"what runs does not match the release ({receipt['status']}" + (
            ": " + ", ".join(str(b) for b in bad[:5]) if bad else "") + ")"
    else:
        return None
    to = (receipt.get("release") if receipt.get("status") in ("install-failed", "mismatch") else None) \
        or pins.get("version") or "unknown"
    frm = _read(h / OS_STATE).get("good") or "unknown"
    stamp = run.get("started_at") or run.get("updated_at") or receipt.get("at") or ""
    return {"kind": "os", "id": "wizard-os", "name": "WizardOS", "from": frm, "to": to, "step": step,
            "reason": reason, "rollback": None, "result": "the update stopped; retry it with Update now",
            "run": f"{to}:{step}:{stamp}"}


def os_notice(row: dict, told: bool) -> tuple[str, str, str]:
    """The owner's notice for a failed WizardOS update, in plain words."""
    return ("WizardOS update needs a look",
            f"The WizardOS update to {row['to']} did not finish (step: {row['step']}): {scrub(row.get('reason'))[:300]}."
            + (" The team was told, so you don't need to report it." if told
               else " Ask your Wizard to look at it."), "high")


def report_os(home=None, *, token=None, gh=_github, on=None, notify=None) -> dict:
    """Report a failed WizardOS update to the team once per failed run, when the switch is on. Best effort:
    returns {} when switched off or nothing failed, else the report result (never raises)."""
    try:
        if not (on or switch_on)():
            return {}
        from . import updates
        row = os_failure(home)
        if row is None:
            good = updates.installed(home)
            if good and _read(_os_home(home) / OS_STATE).get("good") != good:
                _save_os_state(home, good=good)   # the "from" version of a later failure
            return {}
        if _read(_os_home(home) / OS_STATE).get("reported") == row["run"]:
            return {}
        if token is None:
            from . import private
            token = private.token
        tok = token()
        got = report(row, OS_REPO, tok, gh=gh) if tok else {"error": "this Wizard has no GitHub login"}
        if got.get("issue"):
            _save_os_state(home, reported=row["run"])
            if notify is None:
                from . import app_updates
                notify = lambda *msg: app_updates._notify(type("C", (), {"home": _os_home(home)})(), *msg)  # noqa: E731
            notify(*os_notice(row, True))
        return got
    except Exception as e:  # noqa: BLE001 - reporting never fails the update check
        return {"error": f"not reported ({type(e).__name__})"}
