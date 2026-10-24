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

from .vendor.wizard_os_plugin import kernel

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
        for namespace in ("slots", "external"):
            if "schema" in value and "apps" in value and isinstance(value.get(namespace), dict):
                # Slot identifiers are a namespace, not credential-bearing metadata field names.
                out[namespace] = {k: sanitize(v) for k, v in value[namespace].items() if ID.fullmatch(k)}
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
            return {"w": w, "cfg": cfg, "dir": d, "repo": src.get("repo"), "commit": commit,
                    "private": src.get("private") is True}
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


def local_path(value, default="/") -> str:
    """Metadata supplies paths, never an origin, traversal or credential-bearing query."""
    value = value if isinstance(value, str) else default
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or "?" in value or "#" in value or "%" in value or any(p in {".", ".."} for p in value.split("/"))):
        raise ValueError("invalid app endpoint path")
    return value


def service_app_host(value: str) -> str:
    """A declared service-reachable hostname/IP, not a URL or client-supplied upstream."""
    if (not isinstance(value, str) or len(value) > 253 or not value or
            any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in value.split("."))):
        raise ValueError("service_app_host must be an IPv4 address or DNS hostname without scheme, port or path")
    if value.replace(".", "").isdigit():
        import ipaddress
        ipaddress.IPv4Address(value)
    return value


def service_app_network(value: str) -> str:
    """How the Agent Service reaches app views: "host" (service_app_host:<published port>) or "internal"
    (the app's Runtipi network DNS name, for a Service container on that network)."""
    if value not in ("host", "internal"):
        raise ValueError("service_app_network must be 'host' or 'internal'")
    return value


def _port(value) -> int | None:
    return int(value) if str(value).isdigit() and 1 <= int(value) <= 65535 else None


def _main_internal_port(directory: Path) -> int | None:
    """Runtipi's published host port is NOT the container's listening port.

    Recipes in the installed catalog historically used either in view.port.
    Prefer the main service's authoritative Runtipi compose metadata.
    """
    path = directory / "docker-compose.yml"
    if not path.is_file():
        return None
    compose = yaml.safe_load(path.read_text()) or {}
    ports = [_port((service.get('x-runtipi') or {}).get('internal_port'))
             for service in (compose.get('services') or {}).values()
             if isinstance(service, dict) and (service.get('x-runtipi') or {}).get('is_main') is True]
    return ports[0] if len(ports) == 1 else None


def _entry(app: dict, info: dict, urn: str, name: str, store: str, host: str, runtipi_root: Path,
           network: str = "host") -> dict | None:
    meta = app_meta(runtipi_root, store, name)
    if not meta:
        return None
    w, cfg = meta["w"], meta["cfg"]
    port = _port(app.get("port") or cfg.get("port"))
    mcp, view = w.get("mcp"), w.get("view")
    mcp = mcp if isinstance(mcp, dict) else None
    view = view if isinstance(view, dict) else None
    skill = meta["dir"] / "SKILL.md"
    if skill.is_symlink() or not meta["dir"].resolve().is_relative_to(runtipi_root.resolve()):
        raise ValueError("skill source symlink")
    for key in ("provides", "uses", "needs"):
        vals = w.get(key) or []
        if not isinstance(vals, list) or any(not isinstance(v, str) or not ID.fullmatch(v) for v in vals):
            raise ValueError("invalid capability metadata")
    from .wizardmd import voice_names
    try:
        spoken = voice_names(w)
    except ValueError:
        spoken = []
    entry = {
        **({"voice_names": spoken} if spoken else {}),
        **({"privacy": "private"} if str(w.get("privacy", "")).strip().lower() == "private" else {}),
        "id": name, "name": w.get("name", name), "type": w.get("type"), "category": w.get("category"),
        "version": str(w.get("version", "")), "status": app.get("status"), "urn": urn,
        "repo": meta["repo"], "commit": meta["commit"],
        "provides": w.get("provides") or [], "uses": w.get("uses") or [], "needs": w.get("needs") or [],
        "summary": w.get("summary", ""),
        "mcp": None if not mcp or not port else {
            "url": f"http://{host}:{port}{local_path(mcp.get('path'), '/mcp')}",
            "internal": f"http://{name}:{mcp.get('port')}{local_path(mcp.get('path'), '/mcp')}",
            "transport": mcp.get("transport", "streamable-http"),
        },
        "skill": f"skills/{name}/SKILL.md" if skill.is_file() else None,
        "view": None if not view or not port else {**sanitize(view), "url": f"/apps/{name}/"},
        "health": f"http://{host}:{port}{local_path(w['health'])}" if w.get("health") and port else None,
    }
    # Preserve existing metadata as data; no proposed API/backend/replacement interpretation.
    for key in ("api", "source", "backend", "replaces_view", "system"):
        if key in w or key in info or key in cfg:
            entry[key] = sanitize(w.get(key, info.get(key, cfg.get(key))))
    from . import chats_views
    chats = chats_views.installed(meta['dir'], w, meta['commit'])
    entry.update({"shares": w.get("shares") or [], "mounts": w.get("mounts") or []})
    if "private_inference" in w:
        entry["private_inference"] = w["private_inference"]
    if chats:
        entry['chats_view'] = {**chats, 'module': f"/wizard/v1/chats-views/{name}/{chats['sha256']}.js"}
    entry = sanitize(entry)
    if chats:
        entry['_chats_src'] = str(meta['dir'] / chats['module'])
    entry["_skill_src"] = str(skill) if skill.is_file() else None
    if view and port:
        internal_port = _main_internal_port(meta['dir']) or _port(view.get('port')) or port
        origin = f"{name}:{internal_port}" if network == "internal" else f"{host}:{port}"
        entry["_view_upstream"] = f"http://{origin}{local_path(view.get('path'))}"
    json.dumps(entry)  # Reject non-JSON YAML scalars before they can poison the whole projection.
    return entry


def build(installed: list[dict], store: str, host: str, runtipi_root: Path, network: str = "host") -> dict:
    host, network = service_app_host(host), service_app_network(network)
    apps = []
    for row in installed:
        app, info = row.get("app") or {}, row.get("info") or {}
        urn = info.get("urn") or ""
        name, _, st = urn.partition(":")
        if not ID.fullmatch(st) or not ID.fullmatch(name):
            continue
        try:
            meta = app_meta(runtipi_root, st, name)
            if not meta or (st != store and not meta.get("private")):
                continue
            if any(a["id"] == name for a in apps):
                raise ValueError(f"Duplicate installed Wizard app id: {name}")
            entry = _entry(app, info, urn, name, st, host, runtipi_root, network)
        except (ValueError, TypeError, AttributeError, OSError, yaml.YAMLError):
            # One malformed app stays visible/uninstallable, but contributes no unsafe routes/skills/slots.
            entry = {"id": name, "name": name, "urn": urn, "status": app.get("status"),
                     "metadata_error": "metadata_invalid", "provides": [], "uses": [], "needs": [],
                     "mcp": None, "view": None, "health": None, "skill": None, "_skill_src": None}
        if entry:
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
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _skills(reg: dict, root: Path, old: dict) -> None:
    sk = root / "skills"
    if sk.is_symlink():
        raise ValueError("skills root must not be a symlink")
    sk.mkdir(exist_ok=True)
    manifest = root / ".generated-app-skills.json"
    owned = _json_file(manifest)
    # Migrate pre-manifest registries: only the explicitly named app SKILL.md files belong to WAM.
    if not manifest.exists():
        old_apps = old.get("apps", [])
        old_apps = old_apps if isinstance(old_apps, list) else []
        for a in old_apps:
            if not isinstance(a, dict):
                continue
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
            a["skill"] = None
            continue  # Existing external/system links are never app-owned.
        current = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        accepted = owned.get(app_id)
        accepted = accepted if isinstance(accepted, list) else [accepted]
        if current and current not in accepted:
            # A system/user-owned collision is not ours to overwrite.
            a["skill"] = None
            continue
        if src:
            source = Path(src)
            if source.is_symlink():
                raise ValueError("app skill source symlinks are not allowed")
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if current == digest:
                owned[app_id] = new[app_id] = digest
                continue
            # Write-ahead ownership retains both old/new digests across a crash between individual files.
            owned[app_id] = [d for d in {current, digest} if d]
            atomic_write(manifest, (json.dumps(owned) + "\n").encode())
            atomic_write(p, data)
            owned[app_id] = digest
            new[app_id] = digest
    for app_id, digest in owned.items():
        if not ID.fullmatch(app_id) or app_id in new:
            continue
        p = sk / app_id / "SKILL.md"
        if (not p.parent.is_symlink() and not p.is_symlink() and p.is_file()
                and hashlib.sha256(p.read_bytes()).hexdigest() in (digest if isinstance(digest, list) else [digest])):
            p.unlink()
            if not any(p.parent.iterdir()):
                p.parent.rmdir()
    atomic_write(manifest, (json.dumps(new) + "\n").encode())


POINTER_MARK = "Written by Wizard OS from apps.json and rebuilt when apps change. Do not edit."
POINTER_TEMP = "SKILL.md.tmp"   # the Agent Service's and the plugin's in-flight name


def _generated_pointer(d: Path) -> bool:
    """A wizard-job-* folder Wizard OS wrote: a real folder holding only its generated SKILL.md."""
    if d.is_symlink() or not d.is_dir():
        return False
    names = {p.name for p in d.iterdir()}
    skill = d / "SKILL.md"
    if not names <= {"SKILL.md", POINTER_TEMP} or skill.is_symlink() or not skill.is_file():
        return False
    return POINTER_MARK in skill.read_text(encoding="utf-8", errors="replace")


def write_pointers(reg: dict, root: Path, mcp_address: str = "url") -> list[str]:
    """The wizard-job-<job> pointer skills in <root>/skills, rendered by the pinned wizard-os plugin kernel (the
    Agent Service's and the plugin's renderer), so an agent reading this skills folder sees an install or an
    uninstall at once, without waiting for a Hermes session or the Agent Service. Stale generated pointers go;
    app skills, system/owner skills, symlinks and owner-edited pointers (no generated marker) stay."""
    sk = root / "skills"
    if sk.is_symlink():
        raise ValueError("skills root must not be a symlink")
    sk.mkdir(parents=True, exist_ok=True)
    want = kernel.pointer_skills(kernel.bridge_apps(public_projection(reg), mcp_address))
    for d in sorted(sk.iterdir()):
        if d.name.startswith(kernel.JOB_SKILL_PREFIX) and d.name not in want and _generated_pointer(d):
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
    for name, text in want.items():
        d, p = sk / name, sk / name / "SKILL.md"
        if d.is_symlink() or p.is_symlink() or (d.exists() and not d.is_dir()):
            continue
        if p.is_file():
            current = p.read_text(encoding="utf-8", errors="replace")
            if current == text or POINTER_MARK not in current:
                continue   # unchanged, or a skill of the owner's that happens to use the name
        atomic_write(p, text.encode())
    stamp_pointers(root)
    return sorted(want)


def stamp_pointers(root: Path) -> None:
    """Same stamp the Agent Service writes: the plugin's session-start refresh skips work this already did. It must
    be newer than <root>/apps.json: that refresh (the pinned kernel) deletes every wizard-job-* folder it doesn't
    render, owner folders included, so whoever rewrites apps.json last re-stamps."""
    atomic_write(root / "skills" / kernel.JOB_STAMP, f"{int(time.time() * 1000)}\n".encode())


def write(reg: dict, root: Path | None = None, *, preserve_choices=True, mcp_address: str = "url") -> Path:
    root = root or home()
    if root.is_symlink():
        raise ValueError("registry root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    prev = root / "apps.json"
    old = _json_file(prev)
    from . import chats_views
    chats_views.publish(reg, root)
    reg.setdefault('slots', {}).setdefault('chats', {'default': chats_views.BUILTIN, 'candidates': []})
    if chats_views.BUILTIN not in reg['slots']['chats']['candidates']:
        reg['slots']['chats']['candidates'].insert(0, chats_views.BUILTIN)
        reg['slots']['chats']['default'] = chats_views.BUILTIN
    choices = old.get("slots") if preserve_choices else {}
    choices = choices if isinstance(choices, dict) else {}
    for slot, val in choices.items():
        if (isinstance(val, dict) and slot in reg["slots"]
                and val.get("default") in reg["slots"][slot]["candidates"]):
            reg["slots"][slot]["default"] = val["default"]
    _skills(reg, root, old)
    owner = routes(root)
    owner_routes_enabled = (root / "routes.json").exists()

    def project(a: dict) -> dict:
        out = sanitize(a)
        r = owner.get(a["id"]) or {}
        if owner_routes_enabled and out.get("view") is not None:
            out["view"] = {"url": r["view"]} if r.get("view") else None
        if r.get("staged_as") and not a.get('chats_view'):
            out.update({"id": r["staged_as"], "app_id": a["id"], "name": f"{out.get('name') or a['id']} (test)", "staged": True})
        return out

    grants_path = Path(os.environ.get('WIZARD_GRANTS_FILE', root.parent / 'wizard-grants.json'))
    grants = _json_file(grants_path).get('grants', []) if grants_path.is_file() else old.get('grants', [])
    reg.setdefault('grants', grants)
    public = {**reg, "apps": [project(a) for a in reg["apps"]]}
    atomic_write(prev, (json.dumps(public_projection(public), indent=2) + "\n").encode())
    write_pointers(reg, root, mcp_address)
    return prev


def collisions(reg: dict, multi: set[str] | None = None) -> list[str]:
    """Slots filled by more than one app. A `multi` slot (defaults.json) holds one app per provider by design,
    e.g. `ai-connection` (Claude, ChatGPT, ...), so several apps there are not a collision."""
    if multi is None:
        from . import slots as _slots
        multi = {k for k, v in _slots.load_defaults().items() if v.get("multi")}
    return [f"slot '{s}' has {len(v['candidates'])} apps ({', '.join(v['candidates'])}); default {v['default']}"
            for s, v in reg.get("slots", {}).items() if len(v["candidates"]) > 1 and s not in multi]
