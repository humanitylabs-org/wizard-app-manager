"""Installed-app projection and individually atomic, ownership-aware skill refresh."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Any

import yaml

SCHEMA = 2
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|credential|api[_-]?key|^headers$|^env$", re.I)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def home() -> Path:
    return Path(os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def sanitize(value: Any) -> Any:
    """Strip private/credential-bearing keys recursively, including untrusted metadata URLs."""
    if isinstance(value, dict):
        out = {k: sanitize(v) for k, v in value.items()
               if isinstance(k, str) and not k.startswith("_") and not SENSITIVE.search(k)}
        if value.get("type") in {"password", "random"} or "env_variable" in value:
            out.pop("default", None)
            out.pop("value", None)
        return out
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        if JWT.search(value) or re.search(r"\bBearer\s+\S+", value, re.I):
            return "[redacted]"
        if value.startswith(("http://", "https://")):
            try:
                u = urlsplit(value)
                if u.username or u.password:
                    return None
                return urlunsplit((u.scheme, u.netloc, u.path, "", ""))
            except ValueError:
                return None
    return value


def _front(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    value = yaml.safe_load(m.group(1)) if m else {}
    return value if isinstance(value, dict) else {}


def app_meta(runtipi_root: Path, store: str, app_id: str) -> dict | None:
    if not ID.fullmatch(store) or not ID.fullmatch(app_id):
        return None
    for d in (runtipi_root / "repos" / store / "apps" / app_id, runtipi_root / "apps" / store / app_id):
        wm = d / "wizard.md"
        if wm.is_file():
            w = _front(wm.read_text())
            cfg = json.loads((d / "config.json").read_text()) if (d / "config.json").is_file() else {}
            src = json.loads((d / ".wizard-source").read_text()) if (d / ".wizard-source").is_file() else {}
            commit = (d / ".wizard-commit").read_text().strip() if (d / ".wizard-commit").is_file() else None
            return {"w": w, "cfg": cfg, "dir": d, "repo": src.get("repo"), "commit": commit}
    return None


def local_path(value, default="/") -> str:
    """Metadata supplies paths, never an origin, traversal or credential-bearing query."""
    value = value if isinstance(value, str) else default
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or "?" in value or "#" in value or "%" in value or any(p in {".", ".."} for p in value.split("/"))):
        raise ValueError("invalid app endpoint path")
    return value


def build(installed: list[dict], store: str, host: str, runtipi_root: Path) -> dict:
    apps = []
    for row in installed:
        app, info = row.get("app") or {}, row.get("info") or {}
        urn = info.get("urn") or ""
        name, _, st = urn.partition(":")
        if st != store or not ID.fullmatch(name):
            continue
        meta = app_meta(runtipi_root, store, name)
        if not meta:
            continue
        w, cfg = meta["w"], meta["cfg"]
        port = app.get("port") or cfg.get("port")
        port = int(port) if str(port).isdigit() and 1 <= int(port) <= 65535 else None
        mcp, view = w.get("mcp"), w.get("view")
        mcp = mcp if isinstance(mcp, dict) else None
        view = view if isinstance(view, dict) else None
        skill = meta["dir"] / "SKILL.md"
        entry = {
            "id": name, "name": w.get("name", name), "type": w.get("type"), "category": w.get("category"),
            "version": str(w.get("version", "")), "status": app.get("status"), "urn": urn,
            "repo": meta["repo"], "commit": meta["commit"],
            "provides": w.get("provides") or [], "uses": w.get("uses") or [], "needs": w.get("needs") or [],
            "summary": w.get("summary", ""),
            "mcp": None if not mcp or not port else {
                "url": f"http://127.0.0.1:{port}{local_path(mcp.get('path'), '/mcp')}",
                "internal": f"http://{name}:{mcp.get('port')}{local_path(mcp.get('path'), '/mcp')}",
                "transport": mcp.get("transport", "streamable-http"),
            },
            "skill": f"skills/{name}/SKILL.md" if skill.is_file() else None,
            "view": None if not view or not port else {**sanitize(view), "url": f"/apps/{name}/"},
            "health": f"http://127.0.0.1:{port}{local_path(w['health'])}" if w.get("health") and port else None,
        }
        # Preserve existing metadata as data; no proposed API/backend/replacement interpretation.
        for key in ("api", "source", "backend", "replaces_view", "system"):
            if key in w or key in info or key in cfg:
                entry[key] = sanitize(w.get(key, info.get(key, cfg.get(key))))
        entry = sanitize(entry)
        entry["_skill_src"] = str(skill) if skill.is_file() else None
        if view and port:
            entry["_view_upstream"] = f"http://127.0.0.1:{port}{local_path(view.get('path'))}"
        apps.append(entry)
    apps.sort(key=lambda a: a["id"])
    slots: dict[str, list[str]] = {}
    for a in apps:
        for s in a["provides"]:
            slots.setdefault(s, []).append(a["id"])
    return {"schema": SCHEMA, "updated_at": int(time.time()), "store": store, "apps": apps,
            "slots": {k: {"default": v[0], "candidates": v} for k, v in sorted(slots.items())}}


def public_projection(reg: dict) -> dict:
    return sanitize(reg)


def atomic_write(path: Path, data: bytes, mode=0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _skills(reg: dict, root: Path, old: dict) -> None:
    sk = root / "skills"
    sk.mkdir(exist_ok=True)
    manifest = root / ".generated-app-skills.json"
    owned = _json_file(manifest)
    # Migrate pre-manifest registries: only the explicitly named app SKILL.md files belong to WAM.
    if not manifest.exists():
        for a in old.get("apps", []):
            app_id = a.get("id", "")
            if ID.fullmatch(app_id) and a.get("skill") == f"skills/{app_id}/SKILL.md":
                p = sk / app_id / "SKILL.md"
                if p.is_file() and not p.is_symlink():
                    owned[app_id] = hashlib.sha256(p.read_bytes()).hexdigest()
    new = {}
    for a in reg["apps"]:
        app_id, src = a["id"], a.get("_skill_src")
        if not ID.fullmatch(app_id):
            raise ValueError("invalid app id")
        p = sk / app_id / "SKILL.md"
        if p.parent.is_symlink() or p.is_symlink():
            raise ValueError("skill symlinks are not allowed")
        current = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        if current and owned.get(app_id) != current:
            # A system/user-owned collision is not ours to overwrite.
            continue
        if src:
            data = Path(src).read_bytes()
            atomic_write(p, data)
            new[app_id] = hashlib.sha256(data).hexdigest()
    for app_id, digest in owned.items():
        if not ID.fullmatch(app_id) or app_id in new:
            continue
        p = sk / app_id / "SKILL.md"
        if (not p.parent.is_symlink() and not p.is_symlink() and p.is_file()
                and hashlib.sha256(p.read_bytes()).hexdigest() == digest):
            p.unlink()
            if not any(p.parent.iterdir()):
                p.parent.rmdir()
    atomic_write(manifest, (json.dumps(new) + "\n").encode())


def write(reg: dict, root: Path | None = None) -> Path:
    root = root or home()
    root.mkdir(parents=True, exist_ok=True)
    prev = root / "apps.json"
    old = _json_file(prev)
    for slot, val in (old.get("slots") or {}).items():
        if slot in reg["slots"] and val.get("default") in reg["slots"][slot]["candidates"]:
            reg["slots"][slot]["default"] = val["default"]
    _skills(reg, root, old)
    atomic_write(prev, (json.dumps(public_projection(reg), indent=2) + "\n").encode())
    return prev


def collisions(reg: dict) -> list[str]:
    return [f"slot '{s}' has {len(v['candidates'])} apps ({', '.join(v['candidates'])}); default {v['default']}"
            for s, v in reg.get("slots", {}).items() if len(v["candidates"]) > 1]
