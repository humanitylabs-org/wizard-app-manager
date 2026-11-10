"""Apps whose GitHub repo moved keep getting updates (wizard-app-manager#578).

A publisher can rename an app's repo or transfer it to another owner (oscarhenrycollins/wizard-mail became
humanitylabs-org/wizard-mail). The index then lists the app under the new name and delists the old key, but a box
keeps the old name in `.wizard-source` (and the app's config.json `source`). Everything that checks "is this the repo
the app came from" then refuses the new one: the Store's "installed repository conflict", the release defaults'
source check, and the rename check, so the app silently stops updating.

`follow()` fixes the record when, and only when, all of this holds:
  - the recorded repo is a public Standard source (never owner-private, local or link installs);
  - the index no longer lists the recorded repo;
  - GitHub's redirect for the recorded repo (GET /repos/<old> answers 301) names the new repo;
  - both owners are first-party (the same publisher rule as installs, `index.allowed`);
  - the index lists the new repo and it publishes the SAME app id.
It then rewrites `repo`/`git` in `.wizard-source` (and `.wizard-selection`'s `repo`, which must match it) and the
config.json `source` URL in both apps/<store>/<id> and repos/<store>/apps/<id>. The commit and every other field are
left as they are, so the next update runs normally. Nothing else is touched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
GITHUB_API = "https://api.github.com"
FOLLOWED_SOURCES = (None, "standard")   # index installs record no source or "standard"


def _github_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "wizard-app-manager",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:   # urllib follows GitHub's 301 for a moved repo
        return json.load(r)


def current_name(repo: str, get=None) -> str | None:
    """The name GitHub serves `repo` under now (follows a rename or transfer), or None when it can't tell."""
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        return None
    api = os.environ.get("WAM_GITHUB_API", GITHUB_API).rstrip("/")
    try:
        doc = (get or _github_json)(f"{api}/repos/{urllib.parse.quote(repo)}")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    name = doc.get("full_name") if isinstance(doc, dict) else None
    return name if isinstance(name, str) and REPO.fullmatch(name) else None


def _same(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def target(c, app_id: str, repo: str, *, want: str | None = None, index_get=None, resolve=None,
           github=None) -> dict | None:
    """The index record of the repo `repo` moved to, when that repo publishes the same app id `app_id`; else None.
    `want`: only follow to that repo (a Store selection or a release pin names the repo it expects)."""
    from . import index
    if not (isinstance(app_id, str) and ID.fullmatch(app_id) and isinstance(repo, str) and REPO.fullmatch(repo)):
        return None
    if want is not None and _same(want, repo):
        return None
    get = index_get or (lambda r: index._get(f"{c.index}/v1/apps/{r}"))
    try:
        old = get(repo)
    except Exception:  # noqa: BLE001 - the index being down never moves anything
        return None
    if not isinstance(old, dict) or (old.get("listed", True) and old.get("install")):
        return None   # still listed under the recorded name: nothing moved
    new = current_name(repo, github)
    if not new or _same(new, repo) or (want is not None and not _same(new, want)):
        return None
    if not index.allowed({"repo": repo}) or not index.allowed({"repo": new}):
        return None   # never follow to (or from) a publisher this box would not install from
    try:
        rec = (resolve or (lambda r: index.resolve(c.index, r)))(new)
    except Exception:  # noqa: BLE001 - not listed under the new name either: leave it
        return None
    if not isinstance(rec, dict) or rec.get("id") != app_id or not _same(rec.get("repo"), new):
        return None   # never follow to a different app
    return rec


# ---------------------------------------------------------------- the files

def _read(root: Path, rel: str) -> bytes | None:
    try:
        return (Path(root) / rel).read_bytes()
    except FileNotFoundError:
        return None


def _write(root: Path, rel: str, data: bytes) -> None:
    """Atomic replace, keeping the file's mode; root-owned Runtipi files go through the runtipi container."""
    p = Path(root) / rel
    try:
        t = p.with_name(p.name + ".wam-repo-move")
        t.write_bytes(data)
        try:
            t.chmod(p.stat().st_mode & 0o777)
        except OSError:
            pass
        t.replace(p)
    except PermissionError:
        from . import app_rename
        app_rename.rt_write("/data/" + rel, data)


def _github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _points_at(url, repo: str) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip().rstrip("/").removesuffix(".git")
    return u.lower() in (_github_url(repo).lower(), f"http://github.com/{repo}".lower())


def rewrite(root: Path, store: str, app_id: str, old: str, rec: dict, *, read=_read, write=_write) -> list[str]:
    """Point the recorded source of `app_id` at `rec`'s repo in both app folders. Returns the files changed."""
    new = rec["repo"]
    git = (rec.get("install") or {}).get("git")
    git = git if isinstance(git, str) and _points_at(git, new) else _github_url(new)
    changed = []
    for d in (f"apps/{store}/{app_id}", f"repos/{store}/apps/{app_id}"):
        for name in (".wizard-source", ".wizard-selection", "config.json"):
            rel = f"{d}/{name}"
            raw = read(root, rel)
            try:
                doc = json.loads(raw) if raw else None
            except ValueError:
                continue
            if not isinstance(doc, dict):
                continue
            if name == "config.json":
                if not _points_at(doc.get("source"), old):
                    continue
                doc["source"] = _github_url(new)
                out = json.dumps(doc, indent=2) + "\n"
            else:
                if not _same(doc.get("repo"), old):
                    continue
                doc["repo"] = new
                if name == ".wizard-source" and "git" in doc:
                    doc["git"] = git
                out = json.dumps(doc) + "\n"
            write(root, rel, out.encode())
            changed.append(rel)
    return changed


def follow(c, app_id: str, *, want: str | None = None, read=_read, write=_write, **lookups) -> dict | None:
    """If `app_id`'s recorded repo moved on GitHub to a repo that publishes the same app, record the new name.
    Returns {"id", "from", "to", "changed"} when it moved, else None. Never raises."""
    from . import cli
    try:
        raw = read(c.runtipi_root, f"apps/{cli.STORE}/{app_id}/.wizard-source")
        src = json.loads(raw) if raw else None
        if not isinstance(src, dict) or src.get("local") or src.get("source") not in FOLLOWED_SOURCES:
            return None
        if src.get("private") and src.get("source") != "standard":
            return None   # owner-private test builds keep their repo (they move via a Store release change)
        old = src.get("repo")
        if not isinstance(old, str):
            return None
        rec = target(c, app_id, old, want=want, **lookups)
        if not rec:
            return None
        changed = rewrite(c.runtipi_root, cli.STORE, app_id, old, rec, read=read, write=write)
        print(f"{app_id}: its repo moved from {old} to {rec['repo']}; updates now follow the new name",
              file=sys.stderr)
        return {"id": app_id, "from": old, "to": rec["repo"], "changed": changed}
    except Exception as e:  # noqa: BLE001 - a move check never stops an update
        print(f"WARNING: {app_id}: repo move not checked: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
        return None


def follow_all(c, installed: list[dict], only=None, **kw) -> list[dict]:
    """`follow()` for every installed app of the Wizard store (or only those ids)."""
    from . import cli
    out = []
    for row in installed or []:
        app = (row or {}).get("app") or {}
        app_id = app.get("appName")
        if app.get("appStoreSlug") != cli.STORE or not isinstance(app_id, str) or (only and app_id not in only):
            continue
        res = follow(c, app_id, **kw)
        if res:
            out.append(res)
    return out
