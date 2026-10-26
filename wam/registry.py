"""apps.json: the one list of installed Wizard apps. The App Manager writes it; the Agent Service and Wizard UX read it.

Source of truth is the Wizard App Manager itself: for each installed app from the Wizard store, the files it fetched
from the index live in <runtipi>/apps/<store>/<id>/ (installed copy) and <runtipi>/repos/<store>/apps/<id>/ (store copy,
with wizard.md and SKILL.md).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

import yaml

SCHEMA = 2
# Written into the same skills/ folder by the Agent Service and the wizard-os Hermes plugin (kernel.py
# JOB_SKILL_PREFIX / JOB_STAMP). registry.write must never prune them.
ROUTING_SKILL_PREFIX = "wizard-job-"
ROUTING_STAMP = ".wizard-jobs"


def home() -> Path:
    return Path(os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def _front(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (yaml.safe_load(m.group(1)) or {}) if m else {}


def app_meta(runtipi_root: Path, store: str, app_id: str) -> dict | None:
    """wizard.md + config.json of an installed Wizard app, from the store copy (falls back to installed copy)."""
    for d in (runtipi_root / "repos" / store / "apps" / app_id, runtipi_root / "apps" / store / app_id):
        wm = d / "wizard.md"
        if wm.is_file():
            w = _front(wm.read_text())
            cfg = json.loads((d / "config.json").read_text()) if (d / "config.json").is_file() else {}
            installed = runtipi_root / 'apps' / store / app_id
            identity = installed if (installed / '.wizard-source').is_file() else d
            src = json.loads((identity / '.wizard-source').read_text()) if (identity / '.wizard-source').is_file() else {}
            commit = (identity / '.wizard-commit').read_text().strip() if (identity / '.wizard-commit').is_file() else None
            repo, source = src.get('repo'), src.get('source')
            if src.get('local') or (isinstance(repo, str) and repo.startswith('local/')):
                source = 'local'
            elif not isinstance(source, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', source):
                source = 'standard'  # legacy installs without a recorded source stay Public
            return {"w": w, "cfg": cfg, "dir": d, "repo": repo, "commit": commit,
                    "source": source, "private": src.get("private") is True}
    return None


def routes(root: Path | None = None) -> dict:
    """Owner routing for app views, from <wizard-os home>/routes.json:
        {"wisdom": {"view": "/wisdom-next/", "staged_as": "wisdom-next"}}
    `view` is the same-origin path the box gateway serves the app at (phones never reach a raw host port).
    `staged_as` lists a side-by-side install under its own launcher id until the owner cuts it over, so it never
    shadows the owner's existing tile of the same id."""
    f = (root or home()) / "routes.json"
    try:
        data = json.loads(f.read_text()) if f.is_file() else {}
    except Exception:  # noqa: BLE001
        return {}
    ok = {}
    for app_id, r in (data.items() if isinstance(data, dict) else []):
        if not isinstance(r, dict):
            continue
        view = r.get("view")
        staged = r.get("staged_as")
        ok[app_id] = {
            "view": view if isinstance(view, str) and not view.startswith("//") and
            ".." not in view.split("/") and re.fullmatch(r"/[A-Za-z0-9._~/#-]*", view) else None,
            "staged_as": staged if isinstance(staged, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", staged) else None,
        }
    return ok


def build(installed: list[dict], store: str, host: str, runtipi_root: Path) -> dict:
    apps = []
    for row in installed:
        app = row.get("app") or {}
        info = row.get("info") or {}
        urn = info.get("urn") or ""
        name, _, st = urn.partition(":")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", st) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            continue
        meta = app_meta(runtipi_root, st, name)
        if not meta or (st != store and not meta.get("private")):
            continue
        # A fork can retain its canonical app id in a Git-backed store without the
        # public index replacing its recipe. Project its actual installed identity.
        if any(a["id"] == name for a in apps):
            raise ValueError(f"Duplicate installed Wizard app id: {name}")
        w, cfg = meta["w"], meta["cfg"]
        from .wizardmd import voice_names
        try:
            spoken = voice_names(w)
        except ValueError:
            spoken = []  # optional field: a bad list drops the names, never the app or the registry (wam check reports it)
        from . import chats_views
        chats = chats_views.installed(meta['dir'], w, meta['commit'])
        port = app.get("port") or cfg.get("port")
        mcp = w.get("mcp") or None
        view = w.get("view") or None
        apps.append({
            "id": name, "name": w.get("name", name), "type": w.get("type"), "category": w.get("category"),
            "version": str(w.get("version", "")), "status": app.get("status"), "urn": urn,
            "repo": meta["repo"], "commit": meta["commit"], "source": meta["source"],
            "provides": w.get("provides") or [], "uses": w.get("uses") or [], "needs": w.get("needs") or [],
            "summary": w.get("summary", ""),
            **({"voice_names": spoken} if spoken else {}),
            "shares": w.get("shares") or [], "mounts": w.get("mounts") or [],
            **({'chats_view': {**chats, 'module': f"/wizard/v1/chats-views/{name}/{chats['sha256']}.js"},
                '_chats_src': str(meta['dir'] / chats['module'])} if chats else {}),
            **({"private_inference": w["private_inference"]} if "private_inference" in w else {}),
            # Optional tag (James, 23 Oct 2026): `privacy: private` keeps the app off every non-private agent until
            # the owner ticks one. Anything else, or no line at all, is a standard app.
            **({"privacy": "private"} if str(w.get("privacy", "")).strip().lower() == "private" else {}),
            "mcp": None if not mcp else {
                "url": f"http://{host}:{port}{mcp.get('path', '/mcp')}",                 # from the host / Agent Service
                "internal": f"http://{name}:{mcp.get('port')}{mcp.get('path', '/mcp')}",   # from another app
                "transport": mcp.get("transport", "streamable-http"),
            },
            "skill": f"skills/{name}/SKILL.md" if (meta["dir"] / "SKILL.md").is_file() else None,
            "view": None if not view else {"url": f"http://{host}:{port}{view.get('path', '/')}"},
            "health": f"http://{host}:{port}{w['health']}" if w.get("health") else None,
            "_skill_src": str(meta["dir"] / "SKILL.md") if (meta["dir"] / "SKILL.md").is_file() else None,
        })
    apps.sort(key=lambda a: a["id"])
    slots: dict[str, list[str]] = {}
    for a in apps:
        for s in a["provides"]:
            slots.setdefault(s, []).append(a["id"])
    return {"schema": SCHEMA, "updated_at": int(time.time()), "store": store, "apps": apps,
            "slots": {k: {"default": v[0], "candidates": v} for k, v in sorted(slots.items())}}


def public_projection(reg: dict) -> dict:
    """Exclude copy-only app metadata from every JSON serialization boundary."""
    return {**reg, "apps": [{k: v for k, v in a.items() if not k.startswith("_")} for a in reg["apps"]]}


def read_grants_file(path: Path) -> list | None:
    """Read public grant metadata when accessible; never require root-owned backend state."""
    try:
        doc = json.loads(path.read_text())
        grants = doc.get('grants') if isinstance(doc, dict) else None
        return grants if isinstance(grants, list) else None
    except (OSError, ValueError):
        return None


def write(reg: dict, root: Path | None = None) -> Path:
    root = root or home()
    root.mkdir(parents=True, exist_ok=True)
    sk = root / "skills"
    from . import chats_views
    chats_views.publish(reg, root)
    reg.setdefault('slots', {}).setdefault('chats', {'default': chats_views.BUILTIN, 'candidates': []})
    if chats_views.BUILTIN not in reg['slots']['chats']['candidates']:
        reg['slots']['chats']['candidates'].insert(0, chats_views.BUILTIN)
        reg['slots']['chats']['default'] = chats_views.BUILTIN
    sk.mkdir(exist_ok=True)
    for entry in sk.iterdir():
        # skills/ is shared: the Agent Service / wizard-os Hermes plugin keep the wizard-job-* routing
        # skills (and their stamp) here. Only per-app skill folders are ours to prune.
        if entry.name.startswith(ROUTING_SKILL_PREFIX) or entry.name == ROUTING_STAMP:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    for a in reg["apps"]:
        src = a.get("_skill_src")   # get, not pop: write_registry writes twice and the second pass must copy skills too
        if src and not a["id"].startswith(ROUTING_SKILL_PREFIX):
            (sk / a["id"]).mkdir()
            shutil.copy2(src, sk / a["id"] / "SKILL.md")
    prev = root / "apps.json"
    old = {}
    if prev.is_file():
        try:
            old = json.loads(prev.read_text())
        except Exception:  # noqa: BLE001
            old = {}
    for slot, val in (old.get("slots") or {}).items():   # keep owner slot choices across rewrites
        if slot in reg["slots"] and val.get("default") in reg["slots"][slot]["candidates"]:
            reg["slots"][slot]["default"] = val["default"]
    tmp = root / "apps.json.tmp"
    owner = routes(root)
    owner_routes_enabled = (root / "routes.json").exists()

    def project(a: dict) -> dict:
        out = {k: v for k, v in a.items() if not k.startswith("_")}
        r = owner.get(a["id"]) or {}
        if owner_routes_enabled and out.get("view") is not None:
            # Never publish a raw host URL as the view: the UX opens views same-origin through the gateway.
            out["view"] = {"url": r["view"]} if r.get("view") else None
        if r.get("staged_as") and not a.get('chats_view'):
            out.update({"id": r["staged_as"], "app_id": a["id"], "name": f"{out.get('name') or a['id']} (test)",
                        "staged": True})
        return out

    # write_registry already resolved these through the API. Do not eagerly reopen
    # the backend's 0600 file before considering that value (including an empty list).
    grants = reg.get('grants')
    if not isinstance(grants, list):
        grants_path = Path(os.environ.get('WIZARD_GRANTS_FILE', root.parent / 'wizard-grants.json'))
        grants = read_grants_file(grants_path)
        if grants is None:
            grants = old.get('grants', [])
    public = {**reg, "grants": grants, "apps": [project(a) for a in reg["apps"]]}
    tmp.write_text(json.dumps(public, indent=2) + "\n")
    tmp.replace(prev)
    return prev


def collisions(reg: dict, multi: set[str] | None = None) -> list[str]:
    """Slots filled by more than one app. A `multi` slot (defaults.json) holds one app per provider by design,
    e.g. `ai-connection` (Claude, ChatGPT, ...), so several apps there are not a collision."""
    if multi is None:
        from . import slots as _slots
        multi = {k for k, v in _slots.load_defaults().items() if v.get("multi")}
    return [f"slot '{s}' has {len(v['candidates'])} apps ({', '.join(v['candidates'])}); default {v['default']}"
            for s, v in reg.get("slots", {}).items() if len(v["candidates"]) > 1 and s not in multi]
