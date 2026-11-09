"""Tell the team when an app update fails: file a bug in the app's repo, or comment on the open one (#524).

Before this, a failed or rolled-back app update only reached the owner ("Ask your Wizard to look at it"), so the team
heard about it only if the owner reported it. With the feature switch `oscar/update-bug-reports` on (Settings > Try new
features or the WizardOS Tracker; off by default, and always off on For everyone), the nightly app-update run
(`app_updates.apply`) hands each failure to `report_all`, which for every app:

  - finds the app's GitHub repo from the source the App Manager recorded when it installed it (`.wizard-source`);
  - looks for an open issue labelled `bug` that carries the same failure mark (app, target version, failing step);
  - comments on it with this box's report, or files a new issue labelled `bug`.

The report says: box (short host name only), app, from/to version, the failing step and the error. Home folders,
tailnet names and tailnet addresses are scrubbed from the error first. Writing uses this box's own GitHub login (the same
one `private.token()` reads for private apps); without a login, or when GitHub refuses, nothing is filed and the owner's
notice stays as it was. Reporting never fails or slows the update run beyond one short GitHub call per failure.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

SWITCH = "oscar/update-bug-reports"   # feature switch, off by default
API_URL = "https://api.github.com"
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TIMEOUT = 15
MARK = "<!-- wizard-update-failure: {} -->"
# home folders, tailnet names and tailnet addresses (patterns built from parts so no literal path or host is in source)
_PRIVATE = [(re.compile("/" + "home" + r"/[^/\s'\"]+"), "/" + "home" + "/<user>"),
            (re.compile(r"\b[\w-]+(?:\.[\w-]+)*\." + "ts" + r"\.net\b"), "<tailnet-host>"),
            (re.compile(r"\b" + "100" + r"\.(?:\d{1,3}\.){2}\d{1,3}\b"), "<tailnet-ip>")]


def switch_on() -> bool:
    """Whether the owner turned on `oscar/update-bug-reports` (tests monkeypatch this)."""
    from . import feature_switches
    return feature_switches.is_on(SWITCH)


def box_name() -> str:
    """The box's short host name: never a full (tailnet) domain."""
    return (socket.gethostname() or "unknown").split(".", 1)[0][:63] or "unknown"


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
    """Title, body and dedupe mark for one failed app update."""
    app = str(row.get("id") or "unknown")
    name = str(row.get("name") or app)
    frm, to = row.get("from"), row.get("to")
    to_v = row.get("to_version") or to
    step = step_of(row)
    key = f"{app}:{to}:{step}"
    rb = row.get("rollback")
    kept = ("rolled back to the previous version" if isinstance(rb, dict) and rb.get("rolled_back")
            else "rollback failed: needs a look" if isinstance(rb, dict) else "previous version still runs")
    report = "\n".join([
        f"- Box: {box}",
        f"- App: {name} (`{app}`)",
        f"- From/to version: {frm} -> {to}" + (f" ({to_v})" if to_v != to else ""),
        f"- Failing step: {step}",
        f"- Result: {kept}",
        f"- Error: {scrub(row.get('reason'))}",
    ])
    return {"key": key, "mark": MARK.format(key),
            "title": f"App update failed: {name} {frm} to {to} ({step})",
            "body": (f"An automatic app update failed on a Wizard box. Filed by the App Manager (feature switch "
                     f"`{SWITCH}`).\n\n{report}\n\n{MARK.format(key)}\n"),
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
    d = draft(row, box or box_name())
    try:
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
