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
            src = json.loads((d / ".wizard-source").read_text()) if (d / ".wizard-source").is_file() else {}
            commit = (d / ".wizard-commit").read_text().strip() if (d / ".wizard-commit").is_file() else None
            return {"w": w, "cfg": cfg, "dir": d, "repo": src.get("repo"), "commit": commit}
    return None


def build(installed: list[dict], store: str, host: str, runtipi_root: Path) -> dict:
    apps = []
    for row in installed:
        app = row.get("app") or {}
        info = row.get("info") or {}
        urn = info.get("urn") or ""
        name, _, st = urn.partition(":")
        if st != store:
            continue
        meta = app_meta(runtipi_root, store, name)
        if not meta:
            continue
        w, cfg = meta["w"], meta["cfg"]
        port = app.get("port") or cfg.get("port")
        mcp = w.get("mcp") or None
        view = w.get("view") or None
        apps.append({
            "id": name, "name": w.get("name", name), "type": w.get("type"), "category": w.get("category"),
            "version": str(w.get("version", "")), "status": app.get("status"), "urn": urn,
            "repo": meta["repo"], "commit": meta["commit"],
            "provides": w.get("provides") or [], "uses": w.get("uses") or [], "needs": w.get("needs") or [],
            "summary": w.get("summary", ""),
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
    reg = {"schema": SCHEMA, "updated_at": int(time.time()), "store": store, "apps": apps,
           "slots": {k: {"default": v[0], "candidates": v} for k, v in sorted(slots.items())}}
    grants = read_grants(runtipi_root)
    if grants is not None:
        reg["grants"] = grants
    return reg


def read_grants(runtipi_root: Path) -> list[dict] | None:
    """App-to-app grants from the App Manager's state/wizard-grants.json (spec/PERMISSIONS.md). Never tokens."""
    p = runtipi_root / "state" / "wizard-grants.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None
    keep = ("from", "to", "reason", "approved", "created_at")
    return [{k: g[k] for k in keep if k in g} for g in data.get("grants") or [] if isinstance(g, dict)]


def public_projection(reg: dict) -> dict:
    """Exclude copy-only app metadata from every JSON serialization boundary."""
    return {**reg, "apps": [{k: v for k, v in a.items() if not k.startswith("_")} for a in reg["apps"]]}


def write(reg: dict, root: Path | None = None) -> Path:
    root = root or home()
    root.mkdir(parents=True, exist_ok=True)
    sk = root / "skills"
    shutil.rmtree(sk, ignore_errors=True)
    sk.mkdir()
    for a in reg["apps"]:
        src = a.get("_skill_src")   # get, not pop: write_registry writes twice and the second pass must copy skills too
        if src:
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
    tmp.write_text(json.dumps(public_projection(reg), indent=2) + "\n")
    tmp.replace(prev)
    return prev


def collisions(reg: dict) -> list[str]:
    return [f"slot '{s}' has {len(v['candidates'])} apps ({', '.join(v['candidates'])}); default {v['default']}"
            for s, v in reg.get("slots", {}).items() if len(v["candidates"]) > 1]
