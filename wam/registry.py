"""apps.json: the one list of installed Wizard apps. The App Manager writes it; the Agent Service and Wizard UX read it."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

SCHEMA = 1


def home() -> Path:
    return Path(os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def build(catalog: dict, installed: list[dict], store_slug: str, host: str, skills_src: Path) -> dict:
    """Join Runtipi's installed list with the compiled catalog. Only apps from our store count."""
    by_id = {a["id"]: a for a in catalog.get("apps", [])}
    apps = []
    for row in installed:
        app = row.get("app") or {}
        info = row.get("info") or {}
        urn = info.get("urn") or app.get("appUrn") or ""
        name, _, store = urn.partition(":")
        if store != store_slug or name not in by_id:
            continue
        m = by_id[name]
        port = app.get("port") or info.get("port") or m.get("port")
        mcp = m.get("mcp") or None
        entry = {
            "id": m["id"], "name": m["name"], "type": m["type"], "version": m["version"],
            "status": app.get("status"), "system": m["system"], "provides": m["provides"], "uses": m["uses"],
            "needs": m["needs"], "summary": m["summary"], "urn": urn,
            "mcp": None if not mcp else {
                "url": f"http://{host}:{port}{mcp['path']}",              # from the host / Agent Service
                "internal": f"http://{m['id']}:{mcp['port']}{mcp['path']}",  # from another Runtipi app
                "transport": mcp.get("transport", "streamable-http"),
            },
            "skill": f"skills/{m['id']}/SKILL.md" if (skills_src / m["id"] / "SKILL.md").is_file() else None,
            "view": None if not m.get("view") else {"route": f"/apps/{m['id']}/", "upstream": f"http://{host}:{port}{m['view'].get('path', '/')}"},
            "health": f"http://{host}:{port}{m['health']}" if m.get("health") else None,
            "recommends": m.get("recommends"),
        }
        apps.append(entry)
    apps.sort(key=lambda a: a["id"])
    slots: dict[str, list[str]] = {}
    for a in apps:
        for s in a["provides"]:
            slots.setdefault(s, []).append(a["id"])
    return {"schema": SCHEMA, "updated_at": int(time.time()), "store": store_slug, "apps": apps,
            "slots": {k: {"default": v[0], "candidates": v} for k, v in sorted(slots.items())}}


def write(reg: dict, skills_src: Path, root: Path | None = None) -> Path:
    root = root or home()
    root.mkdir(parents=True, exist_ok=True)
    sk = root / "skills"
    shutil.rmtree(sk, ignore_errors=True)
    sk.mkdir()
    for a in reg["apps"]:
        if a["skill"]:
            (sk / a["id"]).mkdir()
            shutil.copy2(skills_src / a["id"] / "SKILL.md", sk / a["id"] / "SKILL.md")
    prev = root / "apps.json"
    old = {}
    if prev.is_file():
        try:
            old = json.loads(prev.read_text())
        except Exception:  # noqa: BLE001
            old = {}
    # Keep owner slot choices across rewrites.
    for slot, val in (old.get("slots") or {}).items():
        if slot in reg["slots"] and val.get("default") in reg["slots"][slot]["candidates"]:
            reg["slots"][slot]["default"] = val["default"]
    tmp = root / "apps.json.tmp"
    tmp.write_text(json.dumps(reg, indent=2) + "\n")
    tmp.replace(prev)
    return prev


def collisions(reg: dict) -> list[str]:
    return [f"slot '{s}' has {len(v['candidates'])} apps ({', '.join(v['candidates'])}); default {v['default']}"
            for s, v in reg.get("slots", {}).items() if len(v["candidates"]) > 1]
