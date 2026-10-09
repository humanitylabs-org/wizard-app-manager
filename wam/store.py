"""Find Wizard apps and compile them into a stock Runtipi app store.

Sources (sources.json):
  {"local": "/path/to/app-repo"}                    a checkout with wizard.md at its root
  {"local": "/path/to/monorepo", "glob": "apps/*"}   every subfolder with a wizard.md
  {"git": "https://github.com/org/repo", "ref": "main", "glob": "apps/*"}
  {"topic": "wizard-ai"}                             every public GitHub repo with that topic
  {"nostr": "wizard-app", "relays": [...], "authors": [npub...]}
                                                     NIP-34 announcements with that hashtag (off by
                                                     default; WAM_NOSTR=1 or "nostr": true adds it)

A topic or hashtag is how an app gets found. Only a passing wizard.md gets it into the store.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import nostr, wizardmd

DEFAULT_TOPICS = ["wizard-app", "wizard-ai"]  # wizard-app approved by James 9 Oct (wizard-os DECISIONS #4); wizard-ai kept during transition


@dataclass
class Candidate:
    origin: str           # human label: repo slug or path
    root: Path            # directory holding wizard.md
    results: list = field(default_factory=list)
    nostr: dict | None = None   # verified NIP-34 announcement, when found on Nostr

    @property
    def ok(self) -> bool:
        return wizardmd.passed(self.results)


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)


def topic_repos(topic: str, token: str | None = None) -> list[str]:
    """Public repos carrying a GitHub topic (unauthenticated works; 60 searches/hour)."""
    url = f"https://api.github.com/search/repositories?q=topic:{topic}&per_page=100"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "wizard-app-manager"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return [f"https://github.com/{i['full_name']}" for i in data.get("items", []) if not i.get("archived")]


def _norm(url: str) -> str:
    return url.strip().lower().removesuffix("/").removesuffix(".git")


def commit_of(root: Path) -> str | None:
    r = _run(["git", "rev-parse", "HEAD"], cwd=root)
    return r.stdout.strip() if r.returncode == 0 else None


def fetch(url: str, ref: str | None, cache: Path) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", url.removeprefix("https://").removesuffix(".git"))
    dest = cache / slug
    if (dest / ".git").is_dir():
        _run(["git", "fetch", "--depth", "1", "origin", ref or "HEAD"], cwd=dest)
        _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=dest)
    else:
        shutil.rmtree(dest, ignore_errors=True)
        cmd = ["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [url, str(dest)]
        r = _run(cmd)
        if r.returncode:
            raise RuntimeError(f"clone failed: {url}: {r.stderr.strip()[:300]}")
    return dest


def expand(root: Path, glob: str | None) -> list[Path]:
    if not glob:
        return [root]
    return sorted(p for p in root.glob(glob) if p.is_dir() and (p / "wizard.md").is_file())


def discover(sources: list[dict], cache: Path, token: str | None = None, nostr_fetch=None) -> tuple[list[Candidate], list[str]]:
    cands: list[Candidate] = []
    errors: list[str] = []
    seen: set[str] = set()
    for s in sources:
        roots: list[tuple] = []
        try:
            if "local" in s:
                roots = [(str(p), p) for p in expand(Path(s["local"]).expanduser(), s.get("glob"))]
            elif "git" in s:
                d = fetch(s["git"], s.get("ref"), cache)
                roots = [(s["git"] + ("/" + str(p.relative_to(d)) if p != d else ""), p) for p in expand(d, s.get("glob"))]
            elif "topic" in s:
                for url in topic_repos(s["topic"], token):
                    if _norm(url) in seen:
                        continue
                    seen.add(_norm(url))
                    try:
                        roots.append((url, fetch(url, None, cache)))
                    except Exception as e:  # noqa: BLE001
                        errors.append(str(e))
            elif "nostr" in s:
                kw = {"fetch": nostr_fetch} if nostr_fetch else {}
                anns, errs = nostr.announcements(s["nostr"], s.get("relays"), s.get("authors"), **kw)
                errors.extend(errs)
                for a in anns:
                    if not a["clone"]:
                        errors.append(f"nostr {a['npub']}/{a['d']}: announcement has no clone URL")
                        continue
                    if _norm(a["clone"]) in seen:   # same repo already found by topic; GitHub copy wins
                        continue
                    seen.add(_norm(a["clone"]))
                    try:
                        roots.append((f"nostr:{a['npub']}/{a['d']}", fetch(a["clone"], None, cache), a))
                    except Exception as e:  # noqa: BLE001
                        errors.append(str(e))
            else:
                errors.append(f"unknown source: {s}")
                continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s}: {e}")
            continue
        for origin, root, *ann in roots:
            res = wizardmd.check(root)
            a = ann[0] if ann else None
            if a is not None and wizardmd.passed(res):
                res.append(nostr.publisher_rule(wizardmd.front_matter((root / "wizard.md").read_text()).get("publisher"), a))
            cands.append(Candidate(origin, root, res, a))
    return cands, errors


def meta(c: Candidate) -> dict:
    text = (c.root / "wizard.md").read_text()
    d = wizardmd.front_matter(text)
    cfg = json.loads((c.root / d["runtime"]["config"]).read_text())
    return {
        "id": d["id"], "name": d.get("name", d["id"]), "type": d["type"], "version": str(d["version"]),
        "summary": d["summary"], "source": d.get("source"), "origin": c.origin,
        "system": bool(d.get("system", False)), "provides": d.get("provides") or [], "uses": d.get("uses") or [],
        "needs": d.get("needs") or [], "mcp": d.get("mcp"), "skill": d.get("skill"), "view": d.get("view"),
        "health": d.get("health"), "recommends": d.get("recommends"),
        "port": cfg.get("port"), "architectures": cfg.get("supported_architectures") or [],
        "commit": commit_of(c.root),                     # installs and updates pin to this
        "publisher": d.get("publisher"),
        "found_via": "nostr" if c.nostr else "github",
        "nostr": None if not c.nostr else {"npub": c.nostr["npub"], "d": c.nostr["d"], "event": c.nostr["id"], "euc": c.nostr["euc"]},
    }


def compile_store(cands: list[Candidate], out: Path) -> dict:
    """Write a stock Runtipi store (apps/<id>/...) plus catalog.json and skills/<id>/SKILL.md."""
    apps_dir = out / "apps"
    shutil.rmtree(apps_dir, ignore_errors=True)
    shutil.rmtree(out / "skills", ignore_errors=True)
    apps_dir.mkdir(parents=True)
    catalog: dict = {"apps": [], "skipped": []}
    used: set[str] = set()
    for c in cands:
        if not c.ok:
            catalog["skipped"].append({"origin": c.origin, "failed": wizardmd.failures(c.results)})
            continue
        m = meta(c)
        if m["id"] in used:
            catalog["skipped"].append({"origin": c.origin, "failed": [f"duplicate id {m['id']}"]})
            continue
        used.add(m["id"])
        d = wizardmd.front_matter((c.root / "wizard.md").read_text())
        dst = apps_dir / m["id"]
        (dst / "metadata").mkdir(parents=True)
        cfg = json.loads((c.root / d["runtime"]["config"]).read_text())
        cfg["id"] = m["id"]  # the store folder name and the config id must agree
        (dst / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        shutil.copy2(c.root / d["runtime"]["compose"], dst / "docker-compose.yml")
        rt_dir = (c.root / d["runtime"]["config"]).parent
        for name in ("logo.jpg",):
            for cand in (rt_dir / "metadata" / name, rt_dir / name, c.root / "metadata" / name):
                if cand.is_file():
                    shutil.copy2(cand, dst / "metadata" / name)
                    break
        (dst / "metadata" / "description.md").write_text(f"# {m['name']}\n\n{wizardmd.body((c.root / 'wizard.md').read_text())}\n")
        if m["skill"]:
            sk = out / "skills" / m["id"]
            sk.mkdir(parents=True)
            shutil.copy2(c.root / m["skill"], sk / "SKILL.md")
        catalog["apps"].append(m)
    catalog["apps"].sort(key=lambda a: a["id"])
    (out / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
    (out / "README.md").write_text(
        "# Wizard app store (generated)\n\nCompiled by wizard-app-manager from every source whose wizard.md passed the checker. "
        "Do not edit by hand.\n\n" + "\n".join(f"- `{a['id']}` {a['version']} ({a['type']}) — {a['summary']}" for a in catalog["apps"]) + "\n")
    return catalog


def commit_store(out: Path) -> str:
    """Keep the compiled store as a git repo so Runtipi can pull it. Returns HEAD."""
    if not (out / ".git").is_dir():
        _run(["git", "init", "-q", "-b", "main"], cwd=out)
    env = dict(os.environ, GIT_AUTHOR_NAME="wizard-app-manager", GIT_AUTHOR_EMAIL="wam@localhost",
               GIT_COMMITTER_NAME="wizard-app-manager", GIT_COMMITTER_EMAIL="wam@localhost")
    subprocess.run(["git", "add", "-A"], cwd=out, check=True, env=env)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=out, env=env).returncode:
        subprocess.run(["git", "commit", "-q", "-m", "compile store"], cwd=out, check=True, env=env)
    _run(["git", "update-server-info"], cwd=out)
    return _run(["git", "rev-parse", "HEAD"], cwd=out).stdout.strip()


def matches_arch(app: dict, arch: str) -> bool:
    norm = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)
    return not app.get("architectures") or norm in app["architectures"]


__all__ = ["discover", "compile_store", "commit_store", "matches_arch", "fnmatch", "DEFAULT_TOPICS"]
