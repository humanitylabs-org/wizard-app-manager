"""App-to-app grants and connections on stock Runtipi (wizard-os#822, map section D; DECISIONS #76).

On the App Manager fork, the backend owns the connection policy (packages/backend/src/modules/docker/
wizard-app-grants.ts and wizard-storage.ts) and serves the owner API that `wam grants`, `wam network mode`, the
Connections view and the watchers call. Stock Runtipi has none of it. With the stock switch on (wam/stock_runtipi.py)
wam answers those same routes itself, from the same state file, with the same rules:

  GET  /apps/wizard/grants                     approved grants
  GET  /apps/wizard/connections                mode, grants, denied, pending, sensitive, notices
  GET  /apps/<id>:<store>/proposed-grants      what an install would ask for
  POST /apps/<id>:<store>/app-grants           {to, approve}: Reconnect/Approve, Disconnect/Deny
  POST /apps/<id>:<store>/sensitive            {sensitive}
  POST /apps/<id>:<store>/share-grants         {to, share, approve}: recorded (see below)
  POST /apps/wizard/connection-mode            {mode: open|declared}
  POST /apps/wizard/connections-reset          Restore defaults
  POST /apps/wizard/connection-notices         {delivered: [ids]}

State: <runtipi root>/state/wizard-grants.json, the fork's own file and format, so a box that moves from the fork to
stock keeps every owner decision and every reader of that file (network.py, registry, connections_reset) is unchanged.
Each app's layer-2 file (keys and hashes) is <app-data>/<store>/<id>/wizard/grants.json, mounted read-only at
/wizard in its containers by the generated stock store (wizard-app-store scripts/stock-store, mountAppGrants there);
an app asks for a connection at runtime in its own <app-data>/.../wizard-requests/connections.json.

The fork reconciles before every app start (DockerService.prepareApp). Stock Runtipi starts apps by itself, so wam
reconciles instead: on every owner change here, and from `tick` (called before every network reconcile, which the
`wam serve` watchers run after every app start and every few seconds), when an app was installed, updated or removed
or an app's request file changed. A provider learns a new consumer's key hash before the consumer gets the key
(providers are written first), as on the fork.

File shares (`mounts:`/`shares:`, DECISIONS #79/#80; today the Claude sign-in the Hermes Agent app mounts from the
Claude app) are granted like on the fork and mounted through the consumer's stock user-config override: a bind of the
provider's data/<share path> at the mount path, in a block marked `x-wizard-shares` (the rest of the file is kept).
Compose merges service volumes by target, so the share replaces the recipe's placeholder there; a revoke takes it out
and recreates the app, so the placeholder comes back.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

STORE = "wizardapps"
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
TARGET = re.compile(r"^(slot:)?[a-z0-9][a-z0-9-]{1,40}$")
SLOT = re.compile(r"^slot:[a-z0-9][a-z0-9-]{1,40}$")
CORE_SLOTS = ("shell", "agent-service", "agent")
REQUESTS_DIR = "wizard-requests"
REQUESTS_FILE = "connections.json"
NOTICES_KEPT = 50
LEGACY_CLAUDE = "wizard-ai-accounts-claude"


class GrantError(ValueError):
    """The owner's request can't be applied (bad input, app not installed): a 400 on the fork."""


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- policy (wizard-app-grants.ts, pure)

def app_grant(g: dict) -> bool:
    return not g.get("kind") or g.get("kind") == "app"


def same_app_grant(a: dict, b: dict) -> bool:
    return app_grant(a) and app_grant(b) and a.get("from") == b.get("from") and a.get("to") == b.get("to")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _id(a: dict) -> str:
    return a["manifest"]["id"]


def targets(app: dict, apps: list[dict]) -> list[dict]:
    m = app["manifest"]
    uses = m.get("uses") or []
    if not isinstance(uses, list) or any(not isinstance(x, str) or not ID.match(x.removeprefix("slot:")) for x in uses):
        raise ValueError("Invalid uses")
    ids = {_id(a) for a in apps}
    edges = [{"kind": "app", "from": m["id"], "to": to if to.startswith("slot:") or to in ids else f"slot:{to}",
              "reason": "uses"} for to in uses]
    req = m.get("requires") if isinstance(m.get("requires"), dict) else {}
    if req.get("models") or req.get("services"):
        edges.append({"kind": "app", "from": m["id"], "to": "slot:connection-router", "reason": "requires"})
    out = []
    for i, g in enumerate(edges):
        if g["to"] != g["from"] and next(j for j, x in enumerate(edges) if x["to"] == g["to"]) == i:
            out.append(g)
    return out


def providers(to: str, apps: list[dict]) -> list[dict]:
    if to.startswith("slot:"):
        return [a for a in apps if to[5:] in (a["manifest"].get("provides") or [])]
    return [a for a in apps if _id(a) == to]


def pending_of(f: dict) -> list[dict]:
    return f["pending"] if isinstance(f.get("pending"), list) else []


def sensitive_of(f: dict) -> list[str]:
    return [x for x in f["sensitive"] if isinstance(x, str) and ID.match(x)] if isinstance(f.get("sensitive"), list) else []


def notices_of(f: dict) -> list[dict]:
    return f["notices"] if isinstance(f.get("notices"), list) else []


def mode_of(f: dict) -> str:
    return "open" if f.get("mode") == "open" else "declared"


def app_proposals(apps: list[dict], f: dict) -> list[dict]:
    rows = [*f["grants"], *f["denied"], *pending_of(f)]
    return [g for a in apps for g in targets(a, apps)
            if providers(g["to"], apps) and not any(same_app_grant(old, g) for old in rows)]


def keep_open_connections(f: dict, apps: list[dict], sensitive: str, now: int | None = None) -> None:
    if mode_of(f) != "open":
        return
    now = _now() if now is None else now
    rows = [*f["grants"], *f["denied"], *pending_of(f)]
    for a in apps:
        frm = _id(a)
        if frm == sensitive or not a["urn"].endswith(f":{STORE}"):
            continue
        if any(app_grant(g) and g.get("from") == frm and any(_id(p) == sensitive for p in providers(g["to"], apps))
               for g in rows):
            continue
        f["grants"].append({"kind": "app", "from": frm, "to": sensitive, "reason": "open", "approved": "open",
                            "created_at": now})


def _name_of(i: str, apps: list[dict]) -> str:
    a = next((x for x in apps if _id(x) == i), None)
    return (a["manifest"].get("name") if a else None) or i


def _target_name(to: str, apps: list[dict]) -> str:
    ps = providers(to, apps)
    return ", ".join(_name_of(_id(p), apps) for p in ps) if ps else to.removeprefix("slot:")


def add_notice(f: dict, apps: list[dict], kind: str, frm: str, to: str, now: int | None = None) -> dict:
    now = _now() if now is None else now
    a, b = _name_of(frm, apps), _target_name(to, apps)
    sens = sensitive_of(f)
    outbound = kind == "pending" and frm in sens and not any(_id(p) in sens for p in providers(to, apps))
    if kind != "pending":
        text = f"{a} now connects to {b}."
    elif outbound:
        text = f"{a}, which you marked sensitive, wants to connect to {b}. Approve or deny it in App Manager → Connections."
    else:
        text = f"{a} wants to connect to {b}, which you marked sensitive. Approve or deny it in App Manager → Connections."
    n = {"id": secrets.token_hex(6), "at": now, "kind": kind, "from": frm, "to": to, "text": text, "delivered": False}
    f["notices"] = [*notices_of(f), n][-NOTICES_KEPT:]
    return n


def _request_target(to: str, ids: set[str]) -> str:
    return to if to.startswith("slot:") or to in ids else f"slot:{to}"


def apply_connection_policy(apps: list[dict], f: dict, requests: dict[str, list[str]], seeded_before: set[str],
                            now: int | None = None) -> None:
    """applyConnectionPolicy: approve everything new the owner's policy allows, queue what needs the owner."""
    now = _now() if now is None else now
    sensitive = sensitive_of(f)
    f["sensitive"] = sensitive
    f["pending"] = pending_of(f)
    declared = {(g["from"], g["to"]) for a in apps for g in targets(a, apps)}
    ids = {_id(a) for a in apps}
    f["grants"] = [g for g in f["grants"] if not (app_grant(g) and g.get("approved") == "install" and g.get("from") in ids
                                                  and (g.get("from"), g.get("to")) not in declared)]
    asked = {(frm, _request_target(to, ids)) for frm, tos in requests.items() for to in tos}
    keep: list[dict] = []
    is_open = mode_of(f) == "open"

    def sensitive_target(to: str) -> bool:
        return any(_id(p) in sensitive for p in providers(to, apps))

    f["grants"] = [g for g in f["grants"] if not (app_grant(g) and g.get("approved") == "open" and not sensitive_target(g["to"]))]

    def reaches(frm: str, to: str):
        return lambda x: app_grant(x) and x.get("from") == frm and any(_id(q) == to for q in providers(x["to"], apps))

    def already_open(g: dict) -> bool:
        return is_open and any(
            any((x.get("approved") == "open" and reaches(g["from"], _id(p))(x))
                or (x.get("approved") == "owner" and reaches(_id(p), g["from"])(x)) for x in f["grants"])
            for p in providers(g["to"], apps))

    def outbound_from_sensitive(g: dict) -> bool:
        return is_open and g["from"] in sensitive and any(
            _id(p) not in sensitive and not any(reaches(_id(p), g["from"])(x)
                                                 or (x.get("approved") == "owner" and reaches(g["from"], _id(p))(x))
                                                 for x in f["grants"])
            for p in providers(g["to"], apps))

    def needs_owner(g: dict) -> bool:
        return (sensitive_target(g["to"]) and not already_open(g)) or outbound_from_sensitive(g)

    for g in list(f["pending"]):
        k = (g.get("from"), g.get("to"))
        if (g.get("from") not in ids or not providers(g.get("to", ""), apps)
                or not (k in declared or k in asked or (is_open and g.get("reason") == "open"))
                or any(same_app_grant(d, g) for d in f["denied"]) or any(same_app_grant(x, g) for x in f["grants"])):
            continue
        if needs_owner(g):
            keep.append(g)
        elif g.get("reason") == "open":
            continue
        else:
            f["grants"].append({"kind": "app", "from": g["from"], "to": g["to"], "reason": g.get("reason"),
                                "approved": "request" if g.get("reason") == "request" else "install", "created_at": now})
            add_notice(f, apps, "requested" if g.get("reason") == "request" else "added", g["from"], g["to"], now)
    f["pending"] = keep

    def denied_to(frm: str) -> set[str]:
        return {_id(p) for d in f["denied"] if app_grant(d) and d.get("from") == frm for p in providers(d["to"], apps)}

    def blocked(g: dict) -> bool:
        return any(_id(p) in denied_to(g["from"]) for p in providers(g["to"], apps))

    def consider(g: dict, approved: str, quiet: bool) -> None:
        if not any(_id(p) != g["from"] for p in providers(g["to"], apps)):
            return
        if any(same_app_grant(old, g) for old in [*(x for x in f["grants"] if x.get("approved") != "open"),
                                                  *f["denied"], *f["pending"]]):
            return
        if blocked(g):
            return
        if needs_owner(g):
            f["pending"].append({"kind": "app", "from": g["from"], "to": g["to"], "reason": g["reason"], "created_at": now})
            add_notice(f, apps, "pending", g["from"], g["to"], now)
            return
        f["grants"].append({"kind": "app", "from": g["from"], "to": g["to"], "reason": g["reason"], "approved": approved,
                            "created_at": now})
        if not quiet:
            add_notice(f, apps, "requested" if approved == "request" else "added", g["from"], g["to"], now)

    for a in apps:
        for g in targets(a, apps):
            consider(g, "install", _id(a) not in seeded_before)
    for frm, tos in requests.items():
        if frm not in ids:
            continue
        for to in tos:
            consider({"kind": "app", "from": frm, "to": _request_target(to, ids), "reason": "request"}, "request", False)
    if is_open:
        for a in apps:
            frm = _id(a)
            if frm in seeded_before:
                continue
            for s in sensitive:
                if s == frm or s not in ids:
                    continue
                rows = [*f["grants"], *f["denied"], *f["pending"]]
                if any(app_grant(g) and g.get("from") == frm and any(_id(p) == s for p in providers(g["to"], apps))
                       for g in rows):
                    continue
                f["pending"].append({"kind": "app", "from": frm, "to": s, "reason": "open", "created_at": now})
                add_notice(f, apps, "pending", frm, s, now)


def reset_to_defaults(f: dict) -> list[dict]:
    owned = lambda g: app_grant(g) and g.get("approved") == "owner"  # noqa: E731
    dropped = [*(g for g in f["grants"] if owned(g)), *(g for g in f["denied"] if app_grant(g))]
    f["grants"] = [g for g in f["grants"] if not owned(g)]
    f["denied"] = [g for g in f["denied"] if not app_grant(g)]
    return dropped


def provider_endpoint(app: dict) -> str | None:
    """providerEndpoint: an explicit api.endpoint, else the main service's alias (or name) and internal port."""
    api = app["manifest"].get("api") if isinstance(app["manifest"].get("api"), dict) else {}
    explicit = api.get("endpoint")
    if explicit is not None:
        from urllib.parse import urlsplit
        u = urlsplit(str(explicit))
        if u.scheme not in ("http", "https") or u.username or u.password or u.query or u.fragment \
                or re.search(r"[\s$]", str(explicit)):
            raise ValueError("Invalid api.endpoint")
        return str(explicit).rstrip("/")
    services = (app["compose"] or {}).get("services") or {}
    mains = [(n, s) for n, s in services.items() if isinstance(s, dict) and (s.get("x-runtipi") or {}).get("is_main")]
    if len(mains) != 1:
        return None
    name, svc = mains[0]
    try:
        port = int(svc["x-runtipi"].get("internal_port"))
    except (TypeError, ValueError):
        return None
    if port < 1 or port > 65535 or svc.get("network_mode"):
        return None
    nets = svc.get("networks") or {}
    vals = list(nets.values()) if isinstance(nets, dict) else []
    alias = next((s for n in vals if isinstance(n, dict) for s in (n.get("aliases") or [])
                  if isinstance(s, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", s)), None)
    duplicate_main = isinstance(nets, dict) and any(
        k != "tipi_main_network" and isinstance(n, dict) and "runtipi_tipi_main_network" in str(n.get("name") or "")
        and k in nets for k, n in ((app["compose"] or {}).get("networks") or {}).items())
    container = svc.get("container_name") or f"{app['urn'].replace(':', '_')}-{name}-1"
    return f"http://{container if duplicate_main else (alias or name)}:{port}"


# ---------------------------------------------------------------- files on the box

class Files:
    """Read and atomically replace files under the Runtipi root. On owner boxes parts of it are root-owned and wam
    runs as the owner: anything this user may not touch goes through the runtipi container as root (#931)."""

    def __init__(self, root: Path, rtfiles=None):
        self.root = Path(root)
        self._rt = rtfiles

    def rt(self):
        if self._rt is None:
            from .gateway_keys import RuntipiFiles
            self._rt = RuntipiFiles(self.root)
        return self._rt

    def _inside(self, p: Path) -> Path:
        """Every file this module touches is under the Runtipi root; anything else (an id that slipped through as
        `..`) is refused before it is opened."""
        base = os.path.normpath(str(self.root))
        full = os.path.normpath(os.path.join(base, str(p)))
        if not full.startswith(base + os.sep):
            raise ValueError(f"{p} is outside the Runtipi root")
        return Path(full)

    def read(self, p: Path) -> str | None:
        p = self._inside(p)
        try:
            return p.read_text()
        except FileNotFoundError:
            return None
        except PermissionError:
            got = self.rt().read(Path(p))
            return got.decode() if got is not None else None

    def write(self, p: Path, text: str, mode: int = 0o644) -> bool:
        """Replace p with text (nothing when it already holds exactly that). True when it changed."""
        p = self._inside(p)
        if self.read(p) == text:
            return False
        try:
            if p.is_symlink():
                raise ValueError(f"{p} is a link; refusing to replace it")
            tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                with os.fdopen(fd, "w") as out:
                    out.write(text)
                os.chmod(tmp, mode)
                os.replace(tmp, p)
            finally:
                if tmp.exists():
                    tmp.unlink()
        except PermissionError:
            try:
                st = p.parent.stat()
                uid, gid = st.st_uid, st.st_gid
            except OSError:
                uid = gid = 0
            self.rt().write(p, text.encode(), mode, uid, gid)
        return True

    def folder(self, base: Path, relative: str, mode: int, private: bool = False) -> None:
        """confined(base, relative, true) + the fork's mode rule: no link or file anywhere on the way, a missing
        folder is made; a private app keeps the mode of a folder it already has."""
        base = Path(base)
        if not relative or relative.startswith("/") or any(x in ("", ".", "..") for x in relative.split("/")):
            raise ValueError("Unsafe storage path")
        full = base / relative
        walk = Path(full.anchor)
        existed = True
        for part in full.parts[1:]:
            walk = walk / part
            try:
                st = os.lstat(walk)
            except FileNotFoundError:
                st = None
            if st is not None and (not os.path.isdir(walk) or os.path.islink(walk)):
                raise ValueError(f"Storage path is not a real directory: {walk}")
            if st is None:
                existed = False
                if not Path(walk).is_relative_to(base):
                    raise ValueError(f"App data folder missing: {base}")
                try:
                    os.mkdir(walk, 0o700)
                except FileExistsError:
                    pass
                except PermissionError:
                    self._root_mkdir(full, mode)
                    return
        if (not private or not existed) and (os.stat(full).st_mode & 0o777) != mode:
            try:
                os.chmod(full, mode)
            except PermissionError:
                self._root_mkdir(full, mode)

    def _root_mkdir(self, full: Path, mode: int) -> None:
        from .app_rename import _ok, _sh
        inner = self.rt()._inner(full)
        _ok(_sh('[ ! -L "$1" ] || exit 1; mkdir -p "$1" && chmod "$2" "$1"', inner, format(mode, "o")), f"mkdir {full}")


def _front(text: str) -> dict | None:
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text or "", re.S)
    if not m:
        return None
    doc = yaml.safe_load(m.group(1))
    return doc if isinstance(doc, dict) else None


def _compose(text: str | None) -> dict:
    if not text:
        return {"services": {}}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        from .network import load_yaml
        doc = load_yaml(text)
    return doc if isinstance(doc, dict) else {"services": {}}


# ---------------------------------------------------------------- file shares on stock (DECISIONS #79/#80)

CLAUDE_SHARE = {"id": "claude-login", "path": "claude"}
CLAUDE_MOUNT = {"app": "wizard-claude", "share": "claude-login", "path": "/claude"}
SHARES_MARK = "x-wizard-shares"          # top-level key in the app's user-config override: the share mounts wam wrote
SHARES_HEADER = "# File shares: the mounts listed under x-wizard-shares are written by wam (owner share grants).\n"


def _vol_target(v) -> str | None:
    if isinstance(v, str):
        parts = v.split(":")
        return parts[1] if len(parts) > 1 else parts[0]
    if isinstance(v, dict):
        t = v.get("target")
        return t if isinstance(t, str) else None
    return None


def _vol_source(v) -> str | None:
    if isinstance(v, str):
        return v.split(":")[0]
    if isinstance(v, dict):
        s = v.get("source")
        return s if isinstance(s, str) else None
    return None


def _service_volumes(compose: dict):
    for name, s in ((compose.get("services") or {}) if isinstance(compose.get("services"), dict) else {}).items():
        if isinstance(s, dict):
            vols = s.get("volumes")
            vols = getattr(vols, "value", vols)
            yield name, s, [v for v in vols or [] if isinstance(v, (str, dict))] if isinstance(vols, list) else []


def legacy_claude_share(app_id: str, manifest: dict, compose: dict) -> None:
    """The fork's narrow shim for the Claude sign-in pair (wizard-storage.ts apps()): the Claude app shares its
    `claude-login` folder and the Hermes Agent app mounts it at /claude, although neither wizard.md declares it.
    On the fork it keys on the legacy fixed volume `wizard-ai-accounts-claude`; the generated stock store already
    turned that volume into a folder (the Claude app's own data/claude, a placeholder folder in the Hermes Agent app),
    so on stock it also keys on those generated mounts."""
    legacy = any(isinstance(v, dict) and v.get("name") == LEGACY_CLAUDE for v in (compose.get("volumes") or {}).values())
    if app_id == "wizard-claude":
        own = any(_vol_target(v) == "/claude" and (_vol_source(v) or "").startswith("${APP_DATA_DIR}/data/claude")
                  for _, _, vols in _service_volumes(compose) for v in vols)
        if legacy or own:
            manifest.setdefault("shares", [dict(CLAUDE_SHARE)])
    elif app_id == "hermes-agent":
        mounts = any(_vol_target(v) == "/claude" for _, _, vols in _service_volumes(compose) for v in vols)
        if legacy or mounts:
            manifest.setdefault("mounts", [dict(CLAUDE_MOUNT)])


def share_mounts(app: dict, apps: list[dict], f: dict) -> dict[str, list[dict]]:
    """The fork's prepare step for one consumer, as an override: for every granted `mounts:` entry, a bind of the
    provider's data/<share path> at the mount path, in the services whose recipe mounts that path (Claude Code runs in
    the Hermes Agent's engine service), otherwise the main service. Service name -> volume entries."""
    out: dict[str, list[dict]] = {}
    me = _id(app)
    for m in app["manifest"].get("mounts") or []:
        if not isinstance(m, dict) or not isinstance(m.get("path"), str) or not m["path"].startswith("/"):
            continue
        if not any(g.get("kind") == "share" and g.get("from") == me and g.get("to") == m.get("app")
                   and g.get("share") == m.get("share") for g in f.get("grants") or []):
            continue
        p = next((x for x in apps if _id(x) == m.get("app")), None)
        share = next((s for s in (p["manifest"].get("shares") or []) if isinstance(s, dict) and s.get("id") == m.get("share")),
                     None) if p else None
        rel = str((share or {}).get("path") or "")
        if not p or not share or not rel or rel.startswith("/") or any(x in ("", ".", "..") for x in rel.split("/")):
            continue                                    # the fork refuses to start; here the share just isn't mounted
        carriers = [n for n, _, vols in _service_volumes(app["compose"]) if any(_vol_target(v) == m["path"] for v in vols)]
        if not carriers:
            carriers = [n for n, s, _ in _service_volumes(app["compose"]) if (s.get("x-runtipi") or {}).get("is_main")]
            if len(carriers) != 1:
                continue
        for n in carriers:
            out.setdefault(n, []).append({"type": "bind", "source": f"{p['hostData']}/data/{rel}", "target": m["path"],
                                          "bind": {"create_host_path": False}})
    return out


def _provider_folders(app: dict, apps: list[dict], f: dict, files: Files) -> None:
    """A granted share's provider folder exists before the consumer starts, owned by the provider service's uid
    (never the consumer's), as on the fork. Docker would otherwise make it root-owned."""
    me = _id(app)
    for m in app["manifest"].get("mounts") or []:
        if not isinstance(m, dict) or not any(g.get("kind") == "share" and g.get("from") == me and g.get("to") == m.get("app")
                                              and g.get("share") == m.get("share") for g in f.get("grants") or []):
            continue
        p = next((x for x in apps if _id(x) == m.get("app")), None)
        share = next((s for s in (p["manifest"].get("shares") or []) if isinstance(s, dict) and s.get("id") == m.get("share")),
                     None) if p else None
        if not p or not share or not p.get("data"):
            continue
        rel = f"data/{share.get('path')}"
        full = Path(p["data"]) / rel
        if full.is_dir():
            continue
        try:
            files.folder(Path(p["data"]), rel, 0o700)
        except (OSError, ValueError):
            continue
        svc = next((s for _, s, vols in _service_volumes(p["compose"])
                    if any((_vol_source(v) or "").startswith(f"${{APP_DATA_DIR}}/{rel}") for v in vols)), None) \
            or next((s for _, s, _ in _service_volumes(p["compose"]) if (s.get("x-runtipi") or {}).get("is_main")), None) or {}
        user = str(svc.get("user") or "0:0")
        if re.fullmatch(r"\d+(?::\d+)?", user):
            uid, _, gid = user.partition(":")
            try:
                os.chown(full, int(uid), int(gid or uid))
            except OSError:
                try:
                    from .app_rename import _ok, _sh
                    inner = files.rt()._inner(full)
                    _ok(_sh('[ ! -L "$1" ] || exit 1; chown "$2" "$1"', inner, f"{uid}:{gid or uid}"), f"chown {full}")
                except Exception:  # noqa: BLE001 - the consumer still starts; the provider owns its folder on first write
                    pass


def write_share_overlay(files: Files, path: Path, mounts: dict[str, list[dict]]) -> bool:
    """Put (or with {}, remove) the share mounts in one app's user-config override; keep everything else in the file
    (wam's network block, the owner's own lines). Compose merges service volumes by target, so a share replaces the
    recipe's own placeholder at that path, and removing it brings the placeholder back. True when the file changed."""
    from .network import dump_yaml, load_yaml
    before = files.read(path)
    doc = (load_yaml(before) or {}) if before else {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a compose mapping")
    if not mounts and SHARES_MARK not in doc:
        return False
    mark = doc.pop(SHARES_MARK, None)
    svcs = doc.get("services") if isinstance(doc.get("services"), dict) else {}
    for name, targets in ((mark or {}).get("services") or {}).items() if isinstance(mark, dict) else []:
        s = svcs.get(name)
        if not isinstance(s, dict):
            continue
        vols = s.get("volumes")
        tagged = vols if hasattr(vols, "tag") else None
        lst = tagged.value if tagged is not None else vols
        if isinstance(lst, list):
            lst[:] = [v for v in lst if not (isinstance(v, dict) and v.get("target") in (targets or []))]
            if not lst:
                s.pop("volumes", None)
        if not s:
            svcs.pop(name)
    if "services" in doc and not doc["services"]:
        doc.pop("services")
    if mounts:
        svcs = doc.setdefault("services", {})
        for name, entries in mounts.items():
            s = svcs.setdefault(name, {})
            vols = s.get("volumes")
            lst = vols.value if hasattr(vols, "tag") else vols
            if not isinstance(lst, list):
                lst = []
                s["volumes"] = lst
            lst[:] = [v for v in lst if _vol_target(v) not in {e["target"] for e in entries}] + entries
        doc[SHARES_MARK] = {"services": {n: sorted(e["target"] for e in es) for n, es in sorted(mounts.items())}}
    head = []
    for line in (before or "").splitlines():
        if not line.startswith("#"):
            break
        if line + "\n" != SHARES_HEADER:
            head.append(line + "\n")
    if not doc:
        # Only our block was in it: an override with nothing in it (an empty file is not valid compose).
        text = "".join(head) + "services: {}\n"
    else:
        text = "".join(head) + (SHARES_HEADER if SHARES_MARK in doc else "") + dump_yaml(doc)
    if text == (before or ""):
        return False
    return files.write(path, text, 0o644)


def apply_shares(apps: list[dict], f: dict, root: Path, files: Files) -> list[str]:
    """Every installed consumer's override carries exactly its granted shares. Returns the urns whose override changed
    (a running one is recreated by the caller: a plain restart keeps the old mounts)."""
    changed = []
    for a in apps:
        store = a["urn"].partition(":")[2] or STORE
        path = Path(root) / "user-config" / store / _id(a) / "docker-compose.yml"
        mounts = share_mounts(a, apps, f)
        if mounts:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                pass                    # a root-owned user-config: the write goes through the runtipi container
            _provider_folders(a, apps, f, files)
        if not mounts and files.read(path) is None:
            continue
        if write_share_overlay(files, path, mounts):
            changed.append(a["urn"])
    return changed


def installed_urns(rt) -> list[tuple[str, str]]:
    out = []
    for r in rt.installed():
        name, _, store = str(((r.get("info") or {}).get("urn")) or "").partition(":")
        if not name:
            app = r.get("app") if isinstance(r.get("app"), dict) else {}
            name, store = str(app.get("appName") or ""), str(app.get("appStoreSlug") or "")
        if ID.match(name) and ID.match(store):
            out.append((name, store))
    return out


def read_apps(root: Path, installed: list[tuple[str, str]], files: Files, warn=None) -> list[dict]:
    """WizardStorage.apps(): every installed app with a wizard.md, from Runtipi's installed copy of its recipe."""
    root = Path(root)
    out = []
    for app_id, store in installed:
        d = root / "apps" / store / app_id
        text = files.read(d / "wizard.md")
        if not text:
            continue
        manifest = _front(text)
        if manifest is None:
            raise ValueError(f"Invalid manifest {app_id}")
        if manifest.get("id") != app_id:
            raise ValueError("Installed manifest identity mismatch")
        compose = _compose(files.read(d / "docker-compose.yml"))
        legacy_claude_share(app_id, manifest, compose)
        data = root / "app-data" / store / app_id
        env = files.read(data / "app.env") or ""
        m = re.search(r"^APP_DATA_DIR=['\"]?([^\n'\"]+)", env, re.M)
        cfg = json.loads(files.read(d / "config.json") or "{}")
        if not isinstance(cfg, dict) or ("private_data" in cfg and not isinstance(cfg["private_data"], bool)):
            raise ValueError(f"Invalid data permission policy for {app_id}")
        out.append({"urn": f"{app_id}:{store}", "manifest": manifest, "compose": compose, "data": data,
                    "hostData": m.group(1) if m else str(data), "privateData": cfg.get("private_data") is True})
    provs = {_id(a) for a in out if a["manifest"].get("shares")}
    for a in out:
        for mt in a["manifest"].get("mounts") or []:
            if isinstance(mt, dict) and mt.get("app"):
                provs.add(mt["app"])
    if any(sum(1 for i, _ in installed if i == p) > 1 for p in provs):
        raise ValueError("Ambiguous installed provider ids")
    return out


def read_requests(app: dict, files: Files) -> list[str]:
    """readRequests: the app's own requests file, no links followed, size-capped, only well-formed targets."""
    d = Path(app["data"]) / REQUESTS_DIR
    try:
        if not d.is_dir() or d.is_symlink():
            return []
        fd = os.open(d / REQUESTS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        import stat as _st
        if not _st.S_ISREG(st.st_mode) or st.st_size > 16384:
            return []
        doc = json.loads(os.read(fd, 16385).decode())
    except (OSError, ValueError):
        return []
    finally:
        os.close(fd)
    lst = doc if isinstance(doc, list) else doc.get("connections") if isinstance(doc, dict) and isinstance(doc.get("connections"), list) else []
    out = []
    for x in lst[:32]:
        to = x if isinstance(x, str) else x.get("to") if isinstance(x, dict) and isinstance(x.get("to"), str) else ""
        if TARGET.match(to) and to.removeprefix("slot:") != _id(app) and to not in out:
            out.append(to)
    return out


def _valid_app_file(d) -> bool:
    return (isinstance(d, dict) and isinstance(d.get("outgoing"), dict)
            and all(isinstance(v, str) and v for v in d["outgoing"].values())
            and isinstance(d.get("incoming"), list)
            and all(isinstance(v, dict) and isinstance(v.get("from"), str) and isinstance(v.get("sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", v["sha256"]) for v in d["incoming"])
            and isinstance(d.get("enforce"), bool))


def _read_app_file(files: Files, p: Path) -> dict | None:
    text = files.read(p)
    if text is None:
        return None
    d = json.loads(text)
    if not _valid_app_file(d):
        raise ValueError("Invalid app grants file; refusing to replace")
    return d


def _legacy_paths(app: dict, root: Path, files: Files) -> list[Path]:
    """legacyPaths: an older owner mount of the grants file declared in the app's user-config override."""
    store = app["urn"].split(":")[1]
    text = files.read(Path(root) / "user-config" / store / _id(app) / "docker-compose.yml")
    if not text:
        return []
    doc = _compose(text)
    out: list[Path] = []
    suffix = f"/app-data/{store}/{_id(app)}"
    host_root = app["hostData"][:-len(suffix)] if app["hostData"].endswith(suffix) else str(root)
    for svc in (doc.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        if isinstance(env, list):
            env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env if isinstance(e, str))
        env = env if isinstance(env, dict) else {}
        file = str(env.get("WIZARD_GRANTS_FILE") or "/wizard/grants.json")
        for v in svc.get("volumes") or []:
            src, dst = (v.split(":")[0], (v.split(":") + [""])[1]) if isinstance(v, str) else \
                ((v.get("source"), v.get("target")) if isinstance(v, dict) else (None, None))
            if not src or not dst or not file.startswith(f"{dst}/"):
                continue
            source = str(src).replace("${ROOT_FOLDER_HOST}", host_root).replace("${APP_DATA_DIR}", app["hostData"])
            if not source.startswith(f"{host_root}/"):
                raise ValueError("Legacy grants mount outside runtipi root; explicit migration required")
            local = Path(root) / source[len(host_root) + 1:] / file[len(dst) + 1:]
            if any(x == ".." for x in local.relative_to(root).parts):
                raise ValueError("Unsafe storage path")
            if local not in out:
                out.append(local)
    return out


def reconcile_app_grants(installed_apps: list[dict], f: dict, root: Path, save, files: Files,
                         exclude: str | None = None) -> None:
    """reconcileAppGrants: apply the policy, mint keys for new grants, publish provider hashes first, then keys."""
    f["layer2_removing"] = [i for i in (f.get("layer2_removing") or []) if any(_id(a) == i for a in installed_apps)]
    removing = set(f["layer2_removing"])
    apps = [a for a in installed_apps if _id(a) != exclude and _id(a) not in removing and a["urn"].endswith(f":{STORE}")]
    previous = f.get("layer2") if isinstance(f.get("layer2"), dict) else {}
    adopted = set(previous.get("adopted") or [])
    foreign_adopted = set(previous.get("foreign_adopted") or [])

    def foreign_providers(frm: str, to: str) -> list[dict]:
        if not TARGET.match(to):
            return []
        if frm in foreign_adopted and not any(same_app_grant(g, {"kind": "app", "from": frm, "to": to}) for g in f["grants"]):
            return []
        dests = [p for p in providers(to, installed_apps) if _id(p) != frm]
        if any(app_grant(g) and g.get("from") == frm and (g.get("to") == to or any(
                _id(p) in {_id(d) for d in dests} for p in providers(g["to"], installed_apps))) for g in f["denied"]):
            return []
        return [p for p in dests if not p["urn"].endswith(f":{STORE}") and _id(p) != exclude and _id(p) not in removing]

    def primary_endpoint(frm: str, to: str, old: dict) -> str | None:
        primary = (old.get("provider_endpoints") or {}).get(to)
        if not primary:
            return None
        qualified = list(((old.get("provider_endpoints_all") or {}).get(to) or {}).items())
        owners = [i for i, e in qualified if e == primary]
        if not owners:
            return primary
        active = {_id(p) for p in providers(to, installed_apps) if _id(p) not in (frm, exclude) and _id(p) not in removing}
        if any(i in active for i in owners):
            return primary
        rest = sorted((i, e) for i, e in qualified if i in active)
        return rest[0][1] if rest else None

    docs: dict[str, dict] = {}
    for app in apps:
        files.folder(app["data"], "wizard", 0o755, app.get("privateData", False))
        canonical = Path(app["data"]) / "wizard" / "grants.json"
        legacy = [p for p in _legacy_paths(app, root, files) if p != canonical]
        old = _read_app_file(files, canonical)
        if old is None:
            old = next((d for d in (_read_app_file(files, p) for p in legacy) if d), None) or \
                {"outgoing": {}, "incoming": [], "provider_endpoints": {}, "enforce": False}
        foreign = {to: ps for to in old["outgoing"] if (ps := foreign_providers(_id(app), to))}
        pe = {}
        for to, endpoint in (old.get("provider_endpoints") or {}).items():
            if to not in old["outgoing"]:
                pe[to] = endpoint
                continue
            primary = primary_endpoint(_id(app), to, old) if to in foreign else None
            if primary:
                pe[to] = primary
        pea = {}
        for to, ps in foreign.items():
            ent = {i: e for i, e in ((old.get("provider_endpoints_all") or {}).get(to) or {}).items()
                   if any(_id(p) == i for p in ps)}
            if ent:
                pea[to] = ent
        docs[_id(app)] = {"app": app, "paths": [canonical, *legacy], "old": old,
                          "doc": {**old, "outgoing": {k: v for k, v in old["outgoing"].items() if k in foreign},
                                  "incoming": list(old["incoming"]), "provider_endpoints": pe, "provider_endpoints_all": pea}}
        if _id(app) not in adopted:
            for to in old["outgoing"]:
                g = {"kind": "app", "from": _id(app), "to": to, "reason": "owner", "approved": "owner", "created_at": _now()}
                if (ID.match(to) or SLOT.match(to)) and not any(same_app_grant(x, g) for x in [*f["grants"], *f["denied"]]):
                    f["grants"].append(g)
            adopted.add(_id(app))
        if _id(app) not in foreign_adopted:
            for to in foreign:
                g = {"kind": "app", "from": _id(app), "to": to, "reason": "owner", "approved": "owner", "created_at": _now()}
                if not any(same_app_grant(x, g) for x in f["grants"]):
                    f["grants"].append(g)
            foreign_adopted.add(_id(app))
    requests: dict[str, list[str]] = {}
    for app in apps:
        files.folder(app["data"], REQUESTS_DIR, 0o777, app.get("privateData", False))
        asked = read_requests(app, files)
        if asked:
            requests[_id(app)] = asked
    apply_connection_policy(apps, f, requests, set(f.get("seeded") or []))
    managed: dict[str, list[str]] = {}
    endpoints: dict[str, dict[str, str]] = {}
    adopted_hashes: dict[str, list[str]] = {}
    for d in docs.values():
        for to, key in d["old"]["outgoing"].items():
            for p in providers(to, apps):
                adopted_hashes.setdefault(_id(p), []).append(_hash(key))
    prev_managed = previous.get("managed") or {}
    for i, d in docs.items():
        old_hashes = [*(prev_managed.get(i) or []), *(adopted_hashes.get(i) or [])]
        d["doc"]["incoming"] = [r for r in d["doc"]["incoming"] if r["sha256"] not in old_hashes]
    for g in [x for x in f["grants"] if app_grant(x) and x.get("approved") != "open"]:
        consumer = docs.get(g.get("from"))
        dests = [p for p in providers(g["to"], apps) if _id(p) != g["from"]]
        if not consumer or not dests:
            continue
        key = consumer["old"]["outgoing"].get(g["to"]) or secrets.token_hex(32)
        consumer["doc"]["outgoing"][g["to"]] = key
        digest = _hash(key)
        for provider in dests:
            p = docs[_id(provider)]
            for row in [*(r for r in p["old"]["incoming"] if r["sha256"] == digest), {"from": g["from"], "sha256": digest}]:
                if not any(r["sha256"] == digest and r["from"] == row["from"] for r in p["doc"]["incoming"]):
                    p["doc"]["incoming"].append(row)
            managed.setdefault(_id(provider), []).append(digest)
            own = provider_endpoint(provider)
            if own:
                consumer["doc"]["provider_endpoints_all"].setdefault(g["to"], {})[_id(provider)] = own
            old_endpoint = primary_endpoint(g["from"], g["to"], consumer["old"])
            override = bool(old_endpoint) and old_endpoint != ((previous.get("endpoints") or {}).get(g["from"]) or {}).get(g["to"])
            endpoint = old_endpoint if override else own
            if endpoint:
                consumer["doc"]["provider_endpoints"][g["to"]] = endpoint
                if not override:
                    endpoints.setdefault(g["from"], {})[g["to"]] = endpoint
    f["seeded"] = list(dict.fromkeys([*(f.get("seeded") or []), *(_id(a) for a in apps)]))
    pending = {i: list(dict.fromkeys([*(prev_managed.get(i) or []), *(managed.get(i) or [])]))
               for i in dict.fromkeys([*prev_managed, *managed])}
    f["layer2"] = {"managed": pending, "adopted": sorted(adopted), "foreign_adopted": sorted(foreign_adopted),
                   "endpoints": previous.get("endpoints") or {}}
    save(f)
    dump = lambda v: json.dumps(v, indent=2, ensure_ascii=False) + "\n"  # noqa: E731
    for d in docs.values():        # providers first: no consumer gets a new key before its hash is published
        for p in d["paths"]:
            files.write(p, dump({**d["doc"], "outgoing": d["old"]["outgoing"]}))
    for d in docs.values():
        for p in d["paths"]:
            files.write(p, dump(d["doc"]))
    f["layer2"] = {"managed": managed, "adopted": sorted(adopted), "foreign_adopted": sorted(foreign_adopted),
                   "endpoints": endpoints}
    save(f)


# ---------------------------------------------------------------- the owner API (WizardStorage + the controller)

class Grants:
    def __init__(self, root: Path, rt, home: Path | None = None, files: Files | None = None):
        self.root = Path(root)
        self.rt = rt
        self.home = Path(home) if home else Path(os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()
        self.files = files or Files(self.root)

    @property
    def path(self) -> Path:
        return self.root / "state" / "wizard-grants.json"

    @contextmanager
    def serial(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.home / "stock-grants.lock", "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def read(self) -> dict:
        raw = self.files.read(self.path)
        if not raw:
            return {"schema": 1, "updated_at": 0, "seeded": [], "grants": [], "denied": []}
        f = json.loads(raw)
        if not isinstance(f, dict) or f.get("schema") != 1 or not isinstance(f.get("grants"), list) \
                or not isinstance(f.get("denied"), list):
            raise ValueError("Invalid wizard-grants.json; refusing to start")
        return f

    def write(self, f: dict) -> None:
        raw = self.files.read(self.path)
        prev = json.loads(raw) if raw else None
        changed = prev is None or {**prev, "updated_at": 0} != {**f, "updated_at": 0}
        if changed:
            f["updated_at"] = _now()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                pass                    # a root-owned Runtipi root: the write below goes through the runtipi container
            self.files.write(self.path, json.dumps(f, indent=2, ensure_ascii=False) + "\n", 0o600)
        else:
            f["updated_at"] = prev.get("updated_at", 0)
        for mirror in dict.fromkeys([self.home / "apps.json", self.root / "app-data" / "wizard-os" / "apps.json"]):
            try:
                reg = mirror.read_text()
            except OSError:
                continue
            try:
                doc = json.loads(reg)
            except ValueError:
                continue
            if not isinstance(doc, dict):
                continue
            out = json.dumps({**doc, "grants": f["grants"]}, indent=2) + "\n"
            if out != reg:
                try:
                    tmp = mirror.with_name(f"{mirror.name}.tmp-grants")
                    tmp.write_text(out)
                    tmp.replace(mirror)
                except OSError:
                    pass

    def apps(self) -> list[dict]:
        return read_apps(self.root, installed_urns(self.rt), self.files)

    def _reconcile(self, apps: list[dict], f: dict, exclude: str | None = None) -> None:
        reconcile_app_grants(apps, f, self.root, self.write, self.files, exclude)
        self.shares(apps, f)

    def shares(self, apps: list[dict], f: dict) -> list[str]:
        """Write every consumer's share mounts; recreate the running ones whose mounts changed (Runtipi's restart
        runs compose up again with the override, which recreates the containers). The urns that changed."""
        changed = apply_shares(apps, f, self.root, self.files)
        for urn in changed:
            try:
                status = ((self.rt.app(urn) or {}).get("app") or {}).get("status") if hasattr(self.rt, "app") else None
                if status == "running" and hasattr(self.rt, "restart"):
                    self.rt.restart(urn)
            except Exception:  # noqa: BLE001 - the mounts apply on the app's next start; never fails the reconcile
                pass
        return changed

    @staticmethod
    def state(f: dict) -> dict:
        return {"mode": mode_of(f), "grants": [g for g in f["grants"] if app_grant(g)],
                "denied": [g for g in f["denied"] if app_grant(g)], "pending": pending_of(f),
                "sensitive": sensitive_of(f), "notices": notices_of(f), "updated_at": f.get("updated_at", 0)}

    # -- reads
    def grants(self) -> list[dict]:
        return self.read()["grants"]

    def connections(self) -> dict:
        return self.state(self.read())

    def proposed(self, app_id: str | None = None) -> list[dict]:
        if app_id and not ID.match(app_id):
            raise GrantError("Invalid app id")
        apps = self.apps()
        if app_id and not any(_id(a) == app_id for a in apps):
            for slug in sorted(os.listdir(self.root / "repos")) if (self.root / "repos").is_dir() else []:
                if not ID.match(slug):
                    continue
                d = self.root / "repos" / slug / "apps" / app_id
                text = self.files.read(d / "wizard.md")
                if not text:
                    continue
                m = _front(text)
                if not m or m.get("id") != app_id:
                    raise GrantError("Invalid candidate manifest")
                cand = _compose(self.files.read(d / "docker-compose.yml"))
                legacy_claude_share(app_id, m, cand)
                apps.append({"urn": f"{app_id}:{slug}", "manifest": m, "compose": cand, "data": "", "hostData": ""})
                break
        if app_id:
            consumer = next((a for a in apps if _id(a) == app_id), None)
            for dep in (consumer["manifest"].get("uses") or []) if consumer else []:
                if not isinstance(dep, str) or not ID.match(dep) or any(_id(a) == dep for a in apps):
                    continue
                d = self.root / "repos" / STORE / "apps" / dep
                text = self.files.read(d / "wizard.md")
                if not text:
                    continue
                m = _front(text)
                if not m or m.get("id") != dep:
                    raise GrantError("Invalid dependency manifest")
                cand = _compose(self.files.read(d / "docker-compose.yml"))
                legacy_claude_share(dep, m, cand)
                apps.append({"urn": f"{dep}:{STORE}", "manifest": m, "compose": cand, "data": "", "hostData": ""})
        f = self.read()
        key = lambda g: (g.get("kind"), g.get("from"), g.get("to"), g.get("share") or "")  # noqa: E731
        known = {key(g) for g in [*f["grants"], *f["denied"]]}
        out = []
        for a in apps:
            for mt in a["manifest"].get("mounts") or []:
                if not isinstance(mt, dict):
                    continue
                p = next((x for x in apps if _id(x) == mt.get("app")), None)
                g = {"kind": "share", "from": _id(a), "to": mt.get("app"), "share": mt.get("share"), "reason": "mounts"}
                if p and any(isinstance(s, dict) and s.get("id") == mt.get("share") for s in p["manifest"].get("shares") or []) \
                        and key(g) not in known:
                    out.append(g)
                    known.add(key(g))
        out += app_proposals(apps, f)
        return [g for g in out if not app_id or g["from"] == app_id]

    # -- owner changes
    def change(self, grant: dict, approve: bool) -> str:
        with self.serial():
            apps = self.apps()
            f = self.read()
            now = _now()
            if grant["kind"] == "app":
                to = str(grant.get("to") or "")
                if not ID.match(str(grant.get("from") or "")) or not ID.match(to.removeprefix("slot:")) or grant["from"] == to:
                    raise GrantError("Invalid app grant")
                consumer = next((a for a in apps if _id(a) == grant["from"]), None)
                if not consumer:
                    raise GrantError("Consumer not installed")
                prior = next((g for g in [*pending_of(f), *f["grants"], *f["denied"]] if same_app_grant(g, grant)), None)
                reason = prior["reason"] if prior and prior.get("reason") in ("uses", "requires", "request") else "owner"
                f["grants"] = [g for g in f["grants"] if not same_app_grant(g, grant)]
                f["denied"] = [g for g in f["denied"] if not same_app_grant(g, grant)]
                f["pending"] = [g for g in pending_of(f) if not same_app_grant(g, grant)]
                (f["grants"] if approve else f["denied"]).append(
                    {"kind": "app", "from": grant["from"], "to": to, "reason": reason, "approved": "owner", "created_at": now})
                if not approve:
                    cut = {_id(p) for p in providers(to, apps)}
                    alias = lambda g: (app_grant(g) and g.get("from") == grant["from"] and not same_app_grant(g, grant)  # noqa: E731
                                       and any(_id(p) in cut for p in providers(g["to"], apps)))
                    also = [*(g for g in f["grants"] if alias(g)), *(g for g in f["pending"] if alias(g))]
                    f["grants"] = [g for g in f["grants"] if not alias(g)]
                    f["pending"] = [g for g in f["pending"] if not alias(g)]
                    for g in also:
                        if not any(same_app_grant(d, g) for d in f["denied"]):
                            f["denied"].append({"kind": "app", "from": g["from"], "to": g["to"], "reason": g.get("reason") or "owner",
                                                "approved": "owner", "created_at": now})
                f["layer2_removing"] = [i for i in (f.get("layer2_removing") or []) if i != grant["from"]]
                self.write(f)
                self._reconcile(apps, f)
                return consumer["urn"]
            frm, to, share = (str(grant.get(k) or "") for k in ("from", "to", "share"))
            if grant["kind"] != "share" or not ID.match(frm) or not ID.match(to) or not ID.match(share):
                raise GrantError("Invalid share grant")
            consumer = next((a for a in apps if _id(a) == frm), None)
            if not consumer:
                raise GrantError("Consumer not installed")
            if approve and not any(isinstance(m, dict) and m.get("app") == to and m.get("share") == share
                                   for m in consumer["manifest"].get("mounts") or []):
                raise GrantError("No declared mount")
            if approve and not any(_id(a) == to and any(isinstance(s, dict) and s.get("id") == share
                                                        for s in a["manifest"].get("shares") or []) for a in apps):
                raise GrantError("Provider/share not installed")
            match = lambda g: g.get("kind") == "share" and g.get("from") == frm and g.get("to") == to and g.get("share") == share  # noqa: E731
            f["grants"] = [g for g in f["grants"] if not match(g)]
            f["denied"] = [g for g in f["denied"] if not match(g)]
            (f["grants"] if approve else f["denied"]).append(
                {"kind": "share", "from": frm, "to": to, "share": share, "reason": "mounts", "approved": "owner", "created_at": now})
            self.write(f)
            self.shares(apps, f)        # the fork recreates the consumer (compose down + up); a revoke is durable first
            return consumer["urn"]

    def set_sensitive(self, app_id: str, on: bool) -> dict:
        if not ID.match(str(app_id)) or not isinstance(on, bool):
            raise GrantError("Invalid sensitive change")
        with self.serial():
            apps = self.apps()
            f = self.read()
            if on and not any(_id(a) == app_id for a in apps):
                raise GrantError("App not installed")
            if on and any(_id(a) == app_id and any(s in CORE_SLOTS for s in a["manifest"].get("provides") or []) for a in apps):
                raise GrantError("The shell, Agent Service and agent reach apps through the gateway and can’t be marked sensitive")
            rest = [x for x in sensitive_of(f) if x != app_id]
            if on and app_id not in sensitive_of(f):
                keep_open_connections(f, apps, app_id)
            f["sensitive"] = [*rest, app_id] if on else rest
            self.write(f)
            self._reconcile(apps, f)
            return self.state(f)

    def set_mode(self, mode: str) -> dict:
        if mode not in ("open", "declared"):
            raise GrantError("mode must be open or declared")
        with self.serial():
            apps = self.apps()
            f = self.read()
            f["mode"] = mode
            self.write(f)
            self._reconcile(apps, f)
            return self.state(f)

    def reset(self) -> dict:
        with self.serial():
            apps = self.apps()
            f = self.read()
            reset_to_defaults(f)
            self.write(f)
            self._reconcile(apps, f)
            return self.state(f)

    def mark_notices(self, ids) -> dict:
        if not isinstance(ids, list) or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{12}", x) for x in ids):
            raise GrantError("delivered must be a list of notice ids")
        with self.serial():
            f = self.read()
            f["notices"] = [{**n, "delivered": True} if n.get("id") in ids else n for n in notices_of(f)]
            self.write(f)
            return self.state(f)

    def remove(self, app_id: str, apps: list[dict] | None = None) -> None:
        """WizardStorage.remove, for an app that is no longer installed: its grants go, its denials stay."""
        f = self.read()
        f["grants"] = [g for g in f["grants"] if g.get("from") != app_id and g.get("to") != app_id]
        f["seeded"] = [x for x in f.get("seeded") or [] if x != app_id]
        self.write(f)
        self._reconcile(self.apps() if apps is None else apps, f)

    def reconcile(self) -> dict:
        """Bring the state and every app's grants file in line with what is installed now. An app that was seeded and
        is gone was uninstalled (stock Runtipi has no hook for wam): its grants go, as the fork's remove() does."""
        with self.serial():
            installed = installed_urns(self.rt)
            apps = read_apps(self.root, installed, self.files)
            f = self.read()
            here = {i for i, _ in installed}
            gone = [i for i in f.get("seeded") or [] if i not in here]
            if gone:
                f["grants"] = [g for g in f["grants"] if g.get("from") not in gone and g.get("to") not in gone]
                f["seeded"] = [x for x in f.get("seeded") or [] if x not in gone]
            before = json.dumps(f, sort_keys=True)
            self._reconcile(apps, f)
            return {"apps": len(apps), "removed": gone, "changed": before != json.dumps(f, sort_keys=True)}


# ---------------------------------------------------------------- the routes (WizardSharesController)

_APP_ROUTE = re.compile(r"^/apps/([a-z0-9][a-z0-9-]{1,40})(?::[a-z0-9][a-z0-9-]*)?/(proposed-grants|app-grants|sensitive|share-grants)$")
_BOX_ROUTE = re.compile(r"^/apps/wizard/(grants|connections|connection-mode|connections-reset|connection-notices)$")


def handles(path: str) -> bool:
    p = path.split("?", 1)[0]
    return bool(_BOX_ROUTE.match(p) or _APP_ROUTE.match(p))


def handle(g: Grants, method: str, path: str, body: dict | None):
    """Answer one fork route. Raises GrantError for what the fork answered 400."""
    p = path.split("?", 1)[0]
    body = body if isinstance(body, dict) else {}
    box = _BOX_ROUTE.match(p)
    if box:
        what = box.group(1)
        if method == "GET" and what == "grants":
            return g.grants()
        if method == "GET" and what == "connections":
            return g.connections()
        if method == "POST" and what == "connection-mode":
            if body.get("mode") not in ("open", "declared"):
                raise GrantError("mode must be open or declared")
            return g.set_mode(body["mode"])
        if method == "POST" and what == "connections-reset":
            return g.reset()
        if method == "POST" and what == "connection-notices":
            return g.mark_notices(body.get("delivered"))
        raise GrantError(f"{method} {p} is not a route")
    m = _APP_ROUTE.match(p)
    if not m:
        raise GrantError(f"{method} {p} is not a route")
    app_id, what = m.group(1), m.group(2)
    if method == "GET" and what == "proposed-grants":
        return g.proposed(app_id)
    if method == "POST" and what == "app-grants":
        if not isinstance(body.get("approve"), bool) or not isinstance(body.get("to"), str):
            raise GrantError("to and approve required")
        g.change({"kind": "app", "from": app_id, "to": body["to"]}, body["approve"])
        return {"grants": g.grants(), "connections": g.connections()}
    if method == "POST" and what == "sensitive":
        if not isinstance(body.get("sensitive"), bool):
            raise GrantError("sensitive must be boolean")
        return g.set_sensitive(app_id, body["sensitive"])
    if method == "POST" and what == "share-grants":
        if not isinstance(body.get("approve"), bool):
            raise GrantError("approve must be boolean")
        g.change({"kind": "share", "from": app_id, "to": body.get("to"), "share": body.get("share")}, body["approve"])
        return {"grants": g.grants()}
    raise GrantError(f"{method} {p} is not a route")


# ---------------------------------------------------------------- tick (what the fork did before every start)

_stamps: dict[str, str] = {}
_last_full = {"at": 0.0}
FULL_EVERY = 600.0


def _stamp(root: Path, store: str = STORE) -> str:
    """Cheap: which apps Runtipi has installed copies of (and when their recipe changed), and every app's request file."""
    parts = []
    for base, name in ((Path(root) / "apps" / store, "wizard.md"), (Path(root) / "app-data" / store, f"{REQUESTS_DIR}/{REQUESTS_FILE}")):
        try:
            ids = sorted(x for x in os.listdir(base) if ID.match(x))
        except OSError:
            ids = []
        for i in ids:
            try:
                st = os.stat(base / i / name)
                parts.append(f"{base.name}/{i}:{st.st_mtime_ns}:{st.st_size}:{st.st_ino}")
            except OSError:
                parts.append(f"{base.name}/{i}:")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def tick(root: Path, rt, home: Path | None = None, force: bool = False, now: float | None = None) -> dict | None:
    """Reconcile when an app was installed, updated or removed, or an app's request file changed (and every 10
    minutes anyway). None when nothing changed. A failed reconcile is tried again on the next tick."""
    now = time.monotonic() if now is None else now
    key = str(Path(root))
    stamp = _stamp(root)
    if not force and _stamps.get(key) == stamp and now - _last_full["at"] < FULL_EVERY and _last_full["at"]:
        return None
    res = Grants(root, rt, home).reconcile()
    _stamps[key] = stamp
    _last_full["at"] = now
    return res


def for_runtipi(rt) -> Grants | None:
    """The wam-side grants for this Runtipi client, when the stock switch is on and the client knows its root."""
    from . import stock_runtipi
    root = getattr(rt, "root", None)
    if root is None or not stock_runtipi.on():
        return None
    return Grants(root, rt)


__all__ = ["Grants", "GrantError", "handle", "handles", "tick", "for_runtipi", "reconcile_app_grants",
           "apply_connection_policy", "targets", "providers", "provider_endpoint"]
