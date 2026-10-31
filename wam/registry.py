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
import threading
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
    """Installed wizard.md + config.json; the store copy is only a legacy fallback."""
    for d in (runtipi_root / "apps" / store / app_id, runtipi_root / "repos" / store / "apps" / app_id):
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


# Launcher icon: the app's own one-colour silhouette, served by the app itself at <view>/icon.svg (the template's
# web/icon.svg). The shell draws it as a mask (wizard-ux core/sigils.js), so it shows on the next launcher load after
# an install or update, with no shell release. No wizard.md field: the registry asks the running app for the file and
# offers it only when it really is a silhouette. A favicon with a full-bleed backdrop would mask to a solid slab, so
# it is skipped and the shell keeps its own glyph or letter.
ICON_FILE = "icon.svg"
ICON_MAX_BYTES = 64 * 1024
_SVG = "{http://www.w3.org/2000/svg}"
# Children of these never paint on their own (masks, gradients, reusable defs).
_NON_PAINTING = {"defs", "mask", "clipPath", "pattern", "symbol", "marker", "linearGradient", "radialGradient",
                 "filter", "title", "desc", "metadata", "style"}


def launcher_icon_url(view, name: str | None) -> str | None:
    """Same-origin URL of the app's icon file under its view path, or None when the view is not same-origin."""
    url = view.get("url") if isinstance(view, dict) else None
    if name != ICON_FILE or not isinstance(url, str) or not re.fullmatch(r"/(?!/)[A-Za-z0-9._~/-]*", url) \
            or ".." in url.split("/"):
        return None
    return url.split("#")[0].rstrip("/") + "/" + ICON_FILE


def icon_file(icon, view) -> str | None:
    """An icon URL already projected by an earlier registry write (still under this view) keeps its file."""
    return ICON_FILE if isinstance(icon, str) and icon == launcher_icon_url(view, ICON_FILE) else None


def _num(v, whole: float) -> float | None:
    try:
        s = str(v).strip()
        return float(s[:-1]) * whole / 100 if s.endswith("%") else float(re.sub(r"px$", "", s))
    except (TypeError, ValueError):
        return None


def svg_silhouette(body: bytes) -> bool:
    """True for a small SVG that reads as a silhouette when used as a mask: no embedded raster or script and no
    unmasked shape covering (nearly) the whole canvas. App favicons usually paint a coloured rounded square first;
    those return False."""
    import xml.etree.ElementTree as ET
    if len(body) > ICON_MAX_BYTES or re.search(rb"<!(?:DOCTYPE|ENTITY)", body, re.I):
        return False
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return False
    if root.tag not in (f"{_SVG}svg", "svg"):
        return False
    vb = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    try:
        w, h = (float(vb[2]), float(vb[3])) if len(vb) == 4 else (_num(root.get("width"), 0), _num(root.get("height"), 0))
    except ValueError:
        return False
    if not w or not h:
        return False
    vx, vy = (float(vb[0]), float(vb[1])) if len(vb) == 4 else (0.0, 0.0)

    def walk(el):
        for child in el:
            tag = child.tag.split("}")[-1]
            if tag in ("image", "script", "foreignObject"):
                return True
            if tag in _NON_PAINTING:
                continue
            if child.get("fill") != "none" and not child.get("mask") and not child.get("clip-path"):
                if tag == "rect":
                    x, y = _num(child.get("x", 0), w) or 0, _num(child.get("y", 0), h) or 0
                    cw, ch = _num(child.get("width"), w) or 0, _num(child.get("height"), h) or 0
                    if x <= vx + w * .05 and y <= vy + h * .05 and cw >= w * .9 and ch >= h * .9:
                        return True
                if tag in ("circle", "ellipse"):
                    rx = _num(child.get("r") or child.get("rx"), w) or 0
                    ry = _num(child.get("r") or child.get("ry"), h) or 0
                    if rx >= w * .48 and ry >= h * .48:
                        return True
            if walk(child):
                return True
        return False

    return not walk(root)


def _probe_icon(base: str, timeout: float) -> bool | None:
    """True/False = the app answered (has a silhouette or not); None = it could not be reached (keep what we had)."""
    import urllib.error
    import urllib.request
    url = base.split("#")[0].rstrip("/") + "/" + ICON_FILE
    req = urllib.request.Request(url, headers={"User-Agent": "wizard-app-manager", "Accept": "image/svg+xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - box-local app address only
            if r.status != 200 or "image/svg+xml" not in (r.headers.get("Content-Type") or "").lower():
                return False
            return svg_silhouette(r.read(ICON_MAX_BYTES + 1))
    except urllib.error.HTTPError:
        return False
    except (OSError, ValueError):
        return None


def resolve_icons(reg: dict, previous: dict | None = None, timeout: float = 1.5) -> dict:
    """Mark each app whose running copy serves a silhouette icon.svg (`_icon`, projected to `icon` by write).
    Only box-local app addresses are asked, in parallel; an app that is down keeps its previous answer."""
    from concurrent.futures import ThreadPoolExecutor
    if os.environ.get("WAM_ICON_PROBE", "1") == "0":
        return reg
    before = {a.get("app_id") or a.get("id"): a for a in ((previous or {}).get("apps") or []) if isinstance(a, dict)}
    jobs = {}
    for a in reg.get("apps") or []:
        v = a.get("view")
        base = (v.get("local") or v.get("url")) if isinstance(v, dict) else None
        if isinstance(base, str) and re.match(r"http://(?:127\.0\.0\.1|localhost|\[::1\]|[0-9.]+)(?::\d+)?/", base):
            jobs[a["id"]] = base
    if not jobs:
        return reg
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        found = dict(zip(jobs, pool.map(lambda b: _probe_icon(b, timeout), jobs.values())))
    for a in reg["apps"]:
        got = found.get(a["id"])
        old = before.get(a["id"]) or {}
        if got is None and a["id"] in found:
            got = isinstance(old.get("icon"), str) and old["icon"].endswith("/" + ICON_FILE)
        if got:
            a["_icon"] = ICON_FILE
        else:
            a.pop("_icon", None)
    return reg


def read_grants_file(path: Path) -> list | None:
    """Read public grant metadata when accessible; never require root-owned backend state."""
    try:
        doc = json.loads(path.read_text())
        grants = doc.get('grants') if isinstance(doc, dict) else None
        return grants if isinstance(grants, list) else None
    except (OSError, ValueError):
        return None


def write_skills(sk: Path, apps: list) -> None:
    """Project each installed app's SKILL.md into skills/<app>/ without a moment where a still-installed app's skill
    is missing. Readers run concurrently with this: Hermes' skill loader, `wizard link`'s collision check, and other
    registry writers (the App Manager's network watcher rewrites the registry after every app start, e.g. while the
    installer recreates connection apps). Each file is replaced atomically; only folders of apps that are gone are
    removed, and two writers at once never fail on each other's folders."""
    # get, not pop: write_registry writes twice and the second pass must copy skills too
    want = {a["id"]: a["_skill_src"] for a in apps
            if a.get("_skill_src") and not a["id"].startswith(ROUTING_SKILL_PREFIX)}
    for entry in list(sk.iterdir()):
        # skills/ is shared: the Agent Service / wizard-os Hermes plugin keep the wizard-job-* routing
        # skills (and their stamp) here. Only per-app skill folders are ours to prune.
        if entry.name.startswith(ROUTING_SKILL_PREFIX) or entry.name == ROUTING_STAMP:
            continue
        if entry.name in want and entry.is_dir() and not entry.is_symlink():
            for inner in list(entry.iterdir()):   # an app folder holds only its SKILL.md (and writers' temp files)
                if inner.name == "SKILL.md":
                    continue
                if inner.name.startswith(".SKILL.md."):   # another writer's temp file: leave it unless it is stale
                    try:
                        if time.time() - inner.lstat().st_mtime < 300:
                            continue
                    except OSError:
                        continue
                if inner.is_dir() and not inner.is_symlink():
                    shutil.rmtree(inner, ignore_errors=True)
                else:
                    inner.unlink(missing_ok=True)
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    for app_id, src in want.items():
        d = sk / app_id
        d.mkdir(exist_ok=True)
        tmp = d / f".SKILL.md.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, d / "SKILL.md")
        finally:
            tmp.unlink(missing_ok=True)


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
    write_skills(sk, reg["apps"])
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
    tmp = root / f".apps.json.{os.getpid()}.{threading.get_ident()}.tmp"   # unique: registry writers can overlap
    owner = routes(root)
    owner_routes_enabled = (root / "routes.json").exists()

    def project(a: dict) -> dict:
        out = {k: v for k, v in a.items() if not k.startswith("_")}
        r = owner.get(a["id"]) or {}
        if owner_routes_enabled and out.get("view") is not None:
            # Never publish a raw host URL as the view: the UX opens views same-origin through the gateway.
            out["view"] = {"url": r["view"]} if r.get("view") else None
        icon = launcher_icon_url(out.get("view"), a.get("_icon") or icon_file(out.get("icon"), out.get("view")))
        out.pop("icon", None)
        if icon:
            out["icon"] = icon
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
    try:
        tmp.write_text(json.dumps(public, indent=2) + "\n")
        tmp.replace(prev)
    finally:
        tmp.unlink(missing_ok=True)
    return prev


def collisions(reg: dict, multi: set[str] | None = None) -> list[str]:
    """Slots filled by more than one app. A `multi` slot (defaults.json) holds one app per provider by design,
    e.g. `ai-connection` (Claude, ChatGPT, ...), so several apps there are not a collision."""
    if multi is None:
        from . import slots as _slots
        multi = {k for k, v in _slots.load_defaults().items() if v.get("multi")}
    return [f"slot '{s}' has {len(v['candidates'])} apps ({', '.join(v['candidates'])}); default {v['default']}"
            for s, v in reg.get("slots", {}).items() if len(v["candidates"]) > 1 and s not in multi]
