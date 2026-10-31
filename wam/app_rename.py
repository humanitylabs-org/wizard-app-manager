"""App renames: an app's id follows its name, and an installed app moves to its new id without losing anything.

An app is renamed by its publisher: the new release changes `id:` and lists the old id(s) in wizard.md
`renamed_from: [old-id]`. The index then offers the app under the new id. `migrate()` moves an installed old-id app
to that id on this box:

  1. stop the old app and park its Runtipi port on a free one, so the new install can take the same port
  2. copy its data (app-data) and owner overrides (user-config: app.env, compose override) to the new id
  3. carry the owner's connection decisions (wizard-grants.json, as consumer and as provider), its gateway route
     (<wizard-os home>/routes.json, incl. staged_as), Connection Router routes and update holds
  4. install the new id with the old app's saved settings (same port, same flags, same env values)
  5. verify the new app: containers up, its health URL / MCP endpoint answers, the data copy is complete
  6. only then: keep the old data as `<old>.old-<date>` and uninstall the old app

Any failure before step 6 rolls everything back (the new app is uninstalled, every touched file restored, the old
app gets its port back and is started again). Nothing is moved-and-deleted: the old data stays on disk.

Every file under the Runtipi root is touched through the `runtipi` container (root), because Runtipi owns them.
"""
from __future__ import annotations

import fcntl
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
RUNTIPI_CONTAINER = "runtipi"
LOCK = "app-rename.lock"
CARRY_STATE_FILES = ("app-updates.json",)   # <wizard-os home> files keyed by app id that move with the app


# ---------------------------------------------------------------- naming rule (also used by app_standard)

def kebab(name: str) -> str:
    """The id an app name stands for: lowercase words joined by hyphens ("Connected accounts" -> connected-accounts)."""
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def id_matches_name(app_id: str, name: str) -> bool:
    """An app id must be its name in kebab-case, optionally with the `wizard-` prefix (wizard-calendar = Calendar)."""
    k = kebab(name)
    return bool(k) and app_id in (k, f"wizard-{k}")


def renamed_from(w: dict) -> list[str]:
    """The validated `renamed_from` ids of a wizard.md front matter (a string or a list), never the app's own id."""
    v = (w or {}).get("renamed_from")
    v = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    return [x for x in dict.fromkeys(v) if isinstance(x, str) and ID.fullmatch(x) and x != (w or {}).get("id")]


# ---------------------------------------------------------------- root-owned files through the runtipi container

def _sh(script: str, *args: str, stdin: bytes | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    from .agent_link import _docker
    return subprocess.run(_docker() + ["exec", "-i", RUNTIPI_CONTAINER, "sh", "-c", script, "_", *args],
                          input=stdin, capture_output=True, timeout=timeout)


def _ok(p: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if p.returncode:
        raise RuntimeError(f"{what}: {(p.stderr or p.stdout or b'').decode(errors='replace').strip()[:200]}")
    return p


def rt_read(path: str) -> bytes | None:
    """A file under /data in the runtipi container, or None when it does not exist."""
    p = _sh('[ -f "$1" ] || exit 3; cat "$1"', path)
    if p.returncode == 3:
        return None
    return _ok(p, f"read {path}").stdout


def rt_write(path: str, data: bytes, mode: str | None = None) -> None:
    """Replace a file atomically (keeps the existing file's mode unless `mode` is given)."""
    _ok(_sh('d=$(dirname "$1"); mkdir -p "$d"; t="$1.wam-rename.$$"; cat > "$t" || exit 1; '
            'if [ -n "$2" ]; then chmod "$2" "$t"; elif [ -f "$1" ]; then chmod "$(stat -c %a "$1")" "$t"; fi; '
            'mv -f "$t" "$1"', path, mode or "", stdin=data), f"write {path}")


def rt_exists(path: str) -> bool:
    return _sh('[ -e "$1" ]', path).returncode == 0


def rt_nonempty_dir(path: str) -> bool:
    return _sh('[ -d "$1" ] && [ -n "$(ls -A "$1")" ]', path).returncode == 0


def rt_copy_tree(src: str, dst: str) -> None:
    """cp -a src dst (dst must not exist), built beside dst and renamed into place only when complete."""
    _ok(_sh('[ ! -e "$2" ] || { echo "$2 already exists"; exit 1; }; t="$2.wam-rename-copy"; rm -rf "$t"; '
            'cp -a "$1" "$t" && mv "$t" "$2" || { rm -rf "$t"; exit 1; }', src, dst), f"copy {src} -> {dst}")


def rt_move(src: str, dst: str) -> None:
    _ok(_sh('[ ! -e "$2" ] || { echo "$2 already exists"; exit 1; }; mv "$1" "$2"', src, dst), f"move {src} -> {dst}")


def rt_tree_summary(path: str) -> dict:
    """{files, bytes} of a tree (for the copy check)."""
    p = _sh('[ -d "$1" ] || { echo "0 0"; exit 0; }; '
            'echo "$(find "$1" -type f | wc -l) $(find "$1" -type f -exec cat {} + 2>/dev/null | wc -c)"', path)
    files, size = (_ok(p, f"summarise {path}").stdout.decode().split() + ["0", "0"])[:2]
    return {"files": int(files), "bytes": int(size)}


# ---------------------------------------------------------------- id rewriting in owner state

def rename_in_grants(doc: dict, old: str, new: str) -> dict:
    """The App Manager's wizard-grants.json with every reference to `old` (consumer, provider, sensitive, Layer 2
    bookkeeping, notices) pointing at `new`. Slots (`slot:...`) and other apps are untouched."""
    def one(x):
        return new if x == old else x

    out = json.loads(json.dumps(doc))
    for key in ("grants", "denied", "pending", "notices"):
        for g in out.get(key) or []:
            if isinstance(g, dict):
                for f in ("from", "to"):
                    if g.get(f) == old:
                        g[f] = new
    for key in ("sensitive", "layer2_removing", "seeded"):
        if isinstance(out.get(key), list):
            out[key] = list(dict.fromkeys(one(x) if isinstance(x, str) else x for x in out[key]))
    l2 = out.get("layer2")
    if isinstance(l2, dict):
        for key in ("managed", "endpoints"):
            m = l2.get(key)
            if isinstance(m, dict):
                l2[key] = {one(k): (list(dict.fromkeys(one(x) for x in v)) if isinstance(v, list)
                                    else {one(kk): vv for kk, vv in v.items()} if isinstance(v, dict) else v)
                           for k, v in m.items()}
        if isinstance(l2.get("adopted"), list):
            l2["adopted"] = list(dict.fromkeys(one(x) for x in l2["adopted"]))
    return out


def rename_in_routes(doc: dict, old: str, new: str) -> dict:
    """<wizard-os home>/routes.json: the app's entry moves to the new id; a staging label that carries the old id
    (cinequeue-next) carries the new one (movies-next). The gateway path (`view`) is the owner's gateway config and
    stays as it is."""
    out = {}
    for k, v in (doc or {}).items():
        if k == old and isinstance(v, dict):
            v = dict(v)
            st = v.get("staged_as")
            if isinstance(st, str) and (st == old or st.startswith(old + "-")):
                v["staged_as"] = new + st[len(old):]
            k = new
        out[k] = v
    return out


def rename_in_router(doc: dict, old: str, new: str) -> dict:
    """Connection Router routes.json: `<app>/<feature>` routes and `<app>/<service>` services of `old`."""
    out = json.loads(json.dumps(doc))
    routes = out.get("routes")
    if isinstance(routes, dict):
        nr = {}
        for k, v in routes.items():
            app, sep, feat = k.partition("/")
            if app == old:
                k = f"{new}{sep}{feat}"
                if isinstance(v, dict) and v.get("app") == old:
                    v["app"] = new
            if isinstance(v, dict) and v.get("provider") == old:
                v["provider"] = new
            nr[k] = v
        out["routes"] = nr
    services = out.get("services")
    if isinstance(services, dict):
        out["services"] = {(new + k[len(old):] if k.startswith(old + "/") else k): (new if v == old else v)
                           for k, v in services.items()}
    if isinstance(out.get("provider_order"), list):
        out["provider_order"] = [new if x == old else x for x in out["provider_order"]]
    return out


def rename_in_updates_state(doc: dict, old: str, new: str) -> dict:
    out = dict(doc or {})
    if isinstance(out.get("holds"), list):
        out["holds"] = sorted({new if x == old else x for x in out["holds"]})
    if isinstance(out.get("failed"), dict):
        out["failed"] = {(new if k == old else k): v for k, v in out["failed"].items() if k != old}
    return out


def rename_compose_override(text: str, old: str, new: str, services: set[str]) -> str:
    """The owner's user-config compose override: a service block named after the old id is renamed when the new
    recipe's service is named after the new id, and so is its name in wam's x-wizard-networks record (so `wam network
    reconcile` still finds what it wrote). Line-based: overrides use compose tags (!reset, !override) that a plain
    YAML loader refuses. Aliases (wizard-connectors-calendar), other services and every value stay as they are."""
    if new not in services:
        return text
    word = re.compile(rf"(?<![\w-]){re.escape(old)}(?![\w-])")
    out, block = [], None
    for line in text.splitlines(keepends=True):
        top = re.match(r"^([A-Za-z0-9_.-]+):", line)
        if top:
            block = top.group(1)
        if block == "services" and re.match(rf"^(\s+){re.escape(old)}:\s*(#.*)?$", line):
            line = re.sub(rf"^(\s+){re.escape(old)}:", rf"\g<1>{new}:", line)
        elif block == "x-wizard-networks" and not top:
            line = word.sub(new, line)
        out.append(line)
    return "".join(out)


# ---------------------------------------------------------------- finding renames the index offers

def _source(runtipi_root: Path, store: str, app_id: str) -> dict:
    try:
        return json.loads((Path(runtipi_root) / "apps" / store / app_id / ".wizard-source").read_text())
    except (OSError, ValueError):
        return {}


def due(c, rt=None, installed: list[dict] | None = None, fetch=None) -> list[dict]:
    """Installed apps whose source now publishes them under a new id that names this one in `renamed_from`.
    Public-index apps are checked through their recorded repo's index record (GitHub keeps the old repo name
    redirecting after a repo rename, and the index re-reads it). Private sources are moved with `wam app-rename`."""
    from . import cli, index
    rt = rt or c.runtipi()
    rows = installed if installed is not None else rt.installed()
    have = {(r.get("app") or {}).get("appName") for r in rows
            if (r.get("app") or {}).get("appStoreSlug") == cli.STORE}
    fetch = fetch or (lambda repo: index._get(f"{c.index}/v1/apps/{repo}"))
    out = []
    for old in sorted(x for x in have if x):
        src = _source(c.runtipi_root, cli.STORE, old)
        repo = src.get("repo")
        if not isinstance(repo, str) or src.get("private") or src.get("local") or "/" not in repo:
            continue
        try:
            rec = fetch(repo)
        except Exception:  # noqa: BLE001 - the index being down never blocks updates
            continue
        new = rec.get("id")
        if not isinstance(new, str) or new == old or new in have or not rec.get("listed", True):
            continue
        if old in renamed_from(rec.get("manifest") or {}):
            out.append({"from": old, "to": new, "repo": rec.get("repo") or repo, "name": rec.get("name") or new})
    return out


# ---------------------------------------------------------------- the move

class RenameError(RuntimeError):
    pass


def _paths(store: str, app_id: str) -> dict:
    return {"data": f"/data/app-data/{store}/{app_id}", "user": f"/data/user-config/{store}/{app_id}"}


def _new_record(c, old: str, new: str, repo: str | None = None) -> dict:
    """The new id's install record, from the same source the old app came from, or from `repo` when the app moved
    to its own repo (e.g. a package that lived in a folder of a bigger repo)."""
    from . import cli, private
    if repo:
        from . import index
        try:
            rec = index.resolve(c.index, repo)
        except (index.IndexError_, OSError, ValueError):
            rec = None
        if rec and rec.get("id") == new:
            return rec
        return private.prepare(c.runtipi_root, cli.STORE, repo, expected_id=new)
    src = _source(c.runtipi_root, cli.STORE, old)
    if (src.get("private") or src.get("local")) and isinstance(src.get("repo"), str):
        from . import test_catalog
        if not test_catalog.resolve(new) and new not in c.private_apps:
            return private.prepare(c.runtipi_root, cli.STORE, src["repo"], expected_id=new)
    src_repo = src.get("repo")
    repo = src_repo if isinstance(src_repo, str) and "/" in src_repo else None
    if repo and not src.get("private") and not src.get("local"):
        from . import index
        try:
            rec = index.resolve(c.index, repo)   # the same source the old app came from, never a namesake
        except (index.IndexError_, OSError, ValueError):
            rec = None
        if rec and rec.get("id") == new:
            # The publisher usually renames the repo too (GitHub redirects the old name), so the index may list the
            # same commit under both names; record the current name.
            sha = (rec.get("install") or {}).get("commit")
            for h in (index.search(c.index, id=new) if sha else []):
                if h.get("repo") in (None, rec.get("repo")):
                    continue
                try:
                    other = index.resolve(c.index, h["repo"])
                except (index.IndexError_, OSError, ValueError):
                    continue
                if (other.get("install") or {}).get("commit") == sha and other.get("id") == new:
                    return other
            return rec
    return cli.resolve_any(c, new)


def _wizard_md(c, new: str) -> dict:
    from . import cli, registry
    for d in (Path(c.runtipi_root) / "repos" / cli.STORE / "apps" / new, Path(c.runtipi_root) / "apps" / cli.STORE / new):
        if (d / "wizard.md").is_file():
            return registry._front((d / "wizard.md").read_text())
    return {}


def _record_source(c, new: str, rec: dict) -> None:
    """The store fetches an index app by id and records the first repo the index names; when the publisher renamed
    the repo, record the current name we resolved (same commit), so updates follow the repo, not its redirect."""
    from . import cli
    repo = rec.get("repo")
    git = (rec.get("install") or {}).get("git")
    if not (isinstance(repo, str) and isinstance(git, str) and git.endswith("/" + repo)):
        return
    for d in (f"/data/apps/{cli.STORE}/{new}", f"/data/repos/{cli.STORE}/apps/{new}"):
        raw = rt_read(d + "/.wizard-source")
        try:
            src = json.loads(raw) if raw else None
        except ValueError:
            continue
        if not isinstance(src, dict) or src.get("source") != "standard" or src.get("private") or src.get("repo") == repo:
            continue
        src.update(repo=repo, git=git)
        rt_write(d + "/.wizard-source", (json.dumps(src, separators=(",", ":")) + "\n").encode())


def _recipe_services(c, new: str) -> set[str]:
    import yaml
    d = Path(c.runtipi_root) / "repos" / "wizardapps" / "apps" / new
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        try:
            doc = yaml.safe_load((d / name).read_text()) or {}
            return set((doc.get("services") or {}).keys())
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            continue
    try:
        doc = json.loads((d / "docker-compose.json").read_text())
        return {str(s.get("name")) for s in doc.get("services") or [] if isinstance(s, dict) and s.get("name")}
    except (OSError, ValueError):
        return set()


def _verify(c, rt, new: str, want_status: str, *, settle: float, probe_seconds: float, sleep=time.sleep) -> str | None:
    from . import app_updates, cli
    urn = f"{new}:{cli.STORE}"
    status = rt.wait_status(urn, {"running", "stopped", "missing", "install_error", "start_error"}, 900)
    if status != want_status:
        return f"the new app is {status} (the old one was {want_status})"
    if want_status != "running":
        return None
    sleep(settle)
    states = app_updates._containers(urn)
    if not states or "restarting" in states or states.count("running") < 1:
        return f"its containers are not up ({', '.join(states) or 'none'})"
    ep = app_updates._endpoint(c, rt, new)
    if ep:
        deadline = time.monotonic() + probe_seconds
        while not app_updates._probe(ep[0], ep[1], 10):
            if time.monotonic() >= deadline:
                return ("its MCP endpoint does not answer" if ep[1] else "its health check fails") + f" ({ep[0]})"
            sleep(5)
    return None


def migrate(c=None, old: str = "", new: str | None = None, *, dry_run: bool = False, settle: float = 15,
            probe_seconds: float = 120, backup_root: Path | None = None, repo: str | None = None) -> dict:
    """Move installed app `old` to id `new` (default: what its source now publishes). See the module docstring."""
    from . import cli
    c = c or cli.Config()
    home = Path(c.home)
    home.mkdir(parents=True, exist_ok=True)
    lock = open(home / LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"error": "another app rename is running"}
    try:
        return _migrate(c, old, new, dry_run=dry_run, settle=settle, probe_seconds=probe_seconds,
                        backup_root=backup_root, repo=repo)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _migrate(c, old, new, *, dry_run, settle, probe_seconds, backup_root, repo=None):
    from . import cli, network
    store = cli.STORE
    rt = c.runtipi()
    if not ID.fullmatch(old or ""):
        return {"error": f"bad app id {old!r}"}
    have = cli.installed_ids(rt)
    if old not in have:
        return {"error": f"{old} is not installed"}
    if new is None:
        found = [d for d in due(c, rt) if d["from"] == old]
        if not found:
            return {"error": f"{old}: its source does not publish a renamed app (wizard.md renamed_from: [{old}])"}
        new = found[0]["to"]
    if not ID.fullmatch(new) or new == old:
        return {"error": f"bad new id {new!r}"}
    if new in have:
        return {"error": f"{new} is already installed"}
    old_urn, new_urn = f"{old}:{store}", f"{new}:{store}"
    row = (rt.app(old_urn) or {}).get("app") or {}
    saved = dict(row.get("config") or {})
    old_port, old_status = row.get("port"), row.get("status")
    if old_status not in ("running", "stopped"):
        return {"error": f"{old} is {old_status}; try again when it is running or stopped"}
    if repo is not None and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo):
        return {"error": f"bad repo {repo!r} (owner/name)"}
    if new is None and repo:
        return {"error": "--repo needs the new id"}
    rec = _new_record(c, old, new, repo)
    if rec.get("id") != new:
        return {"error": f"the source of {old} publishes {rec.get('id')}, not {new}"}
    info = (rt.app(new_urn) or {}).get("info") or {}   # fetches the new recipe into the store folder
    w = _wizard_md(c, new)
    if old not in renamed_from(w):
        return {"error": f"{new} does not declare renamed_from: [{old}] in its wizard.md; refusing to move {old}'s data"}
    po, pn = _paths(store, old), _paths(store, new)
    if rt_nonempty_dir(pn["data"]) or rt_exists(pn["user"]):
        return {"error": f"{new} already has data or settings on this box ({pn['data']}); nothing was changed"}
    wos = Path(c.home)   # <wizard-os home>
    router = f"/data/app-data/{store}/wizard-connection-router/data/routes.json"
    plan = {"from": old, "to": new, "name": w.get("name") or info.get("name") or new, "status": old_status,
            "port": old_port, "data": po["data"], "settings": sorted(k for k in saved if k.isupper())}
    if dry_run:
        return {"dry_run": True, **plan}

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bdir = Path(backup_root or (wos / "renames")) / f"{old}-to-{new}-{ts}"
    bdir.mkdir(parents=True, exist_ok=False)
    bdir.chmod(0o700)
    files = {"grants": "/data/state/wizard-grants.json", "router": router,
             "override": po["user"] + "/docker-compose.yml"}
    saved_files: dict[str, bytes | None] = {k: rt_read(p) for k, p in files.items()}
    local = {"routes": wos / "routes.json", **{f: wos / f for f in CARRY_STATE_FILES}}
    saved_local = {k: (p.read_bytes() if p.is_file() else None) for k, p in local.items()}
    for k, v in {**saved_files, **saved_local}.items():
        if v is not None:
            (bdir / f"{k}.bak").write_bytes(v)
    (bdir / "app.json").write_text(json.dumps({"from": old, "to": new, "port": old_port, "status": old_status,
                                               "config": saved, "files": files,
                                               "local": {k: str(p) for k, p in local.items()}}, indent=2))
    (bdir / "app.json").chmod(0o600)
    before = {"files": 0, "bytes": 0}
    steps: list[str] = []
    added: dict = {"secret": None, "host_access": False}
    log = lambda s: (steps.append(s), print(f"rename {old} -> {new}: {s}", file=sys.stderr, flush=True))  # noqa: E731

    def rollback(reason: str) -> dict:
        errs = []
        for name, fn in (
            ("uninstall new", lambda: (rt.uninstall(new_urn), rt.wait_status(new_urn, {"missing"}, 300))
             if new in cli.installed_ids(rt) else None),
            ("drop new data", lambda: _sh('rm -rf "$1" "$2"', pn["data"], pn["user"])),
            ("restore files", lambda: [rt_write(files[k], v) for k, v in saved_files.items() if v is not None]),
            ("restore owner files", lambda: [local[k].write_bytes(v) for k, v in saved_local.items() if v is not None]),
            ("drop copied passwords", lambda: added["secret"].unlink() if added["secret"] else None),
            ("host access", lambda: network.set_host_access_allowed(c.home, new, False) if added["host_access"] else None),
            ("old port back", lambda: rt.req("PATCH", f"/app-lifecycle/{old_urn}/update-config",
                                             {**saved, "port": old_port}, timeout=120) if old_port else None),
            ("start old", lambda: (rt.start(old_urn), rt.wait_status(old_urn, {"running"}, 300))
             if old_status == "running" else None),
            ("networks", lambda: cli.network_step(c, rt)),
            ("registry", lambda: cli.write_registry(c, rt)),
        ):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - every step is tried
                errs.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
        (bdir / "result.json").write_text(json.dumps({"rolled_back": not errs, "reason": reason, "steps": steps,
                                                      "rollback_errors": errs}, indent=2))
        return {"from": old, "to": new, "rolled_back": not errs, "error": reason, "steps": steps,
                **({"rollback_errors": errs} if errs else {}), "backup": str(bdir)}

    try:
        if old_status == "running":
            res = cli.app_lifecycle(c, "stop", old)
            if res.get("error"):
                raise RenameError(f"could not stop {old}: {res['error']}")
            log("old app stopped")
        if old_port:
            park = cli.free_port(int(old_port) + 1000 if int(old_port) < 64000 else 20000, avoid={int(old_port)})
            rt.req("PATCH", f"/app-lifecycle/{old_urn}/update-config", {**saved, "port": park}, timeout=120)
            log(f"old app's port {old_port} parked on {park}")
        # measured once the old app is stopped and its settings (app.env) rewritten for the parked port
        before.update(rt_tree_summary(po["data"]))
        rt_copy_tree(po["data"], pn["data"]) if rt_exists(po["data"]) else None
        after = rt_tree_summary(pn["data"])
        if after != before:
            raise RenameError(f"the data copy is incomplete ({after} of {before})")
        log(f"data copied ({before['files']} files, {before['bytes']} bytes)")
        if rt_exists(po["user"]):
            rt_copy_tree(po["user"], pn["user"])
            ov = saved_files.get("override")
            if ov is not None:
                text = rename_compose_override(ov.decode(), old, new, _recipe_services(c, new))
                rt_write(pn["user"] + "/docker-compose.yml", text.encode())
            log("owner settings copied")
        if (g := saved_files["grants"]) is not None:
            doc = json.loads(g)
            rt_write(files["grants"], (json.dumps(rename_in_grants(doc, old, new), indent=2) + "\n").encode())
            log("connections carried")
        if (r := saved_files["router"]) is not None:
            doc = json.loads(r)
            rt_write(router, (json.dumps(rename_in_router(doc, old, new), indent=2) + "\n").encode())
            log("Connection Router routes carried")
        if (r := saved_local["routes"]) is not None:
            doc = json.loads(r)
            local["routes"].write_text(json.dumps(rename_in_routes(doc, old, new), indent=2) + "\n")
            log("gateway route carried")
        for f in CARRY_STATE_FILES:
            if (s := saved_local[f]) is not None:
                local[f].write_text(json.dumps(rename_in_updates_state(json.loads(s), old, new),
                                               indent=2) + "\n")
        if old in network.host_access_allowed(c.home) and new not in network.host_access_allowed(c.home):
            network.set_host_access_allowed(c.home, new, True)
            added["host_access"] = True
        sec_old, sec_new = Path(c.secrets) / f"{old}.json", Path(c.secrets) / f"{new}.json"
        if sec_old.is_file() and not sec_new.exists():
            # generated passwords (build_form): a later reinstall of the new id reuses them; the old file stays
            sec_new.write_bytes(sec_old.read_bytes())
            sec_new.chmod(0o600)
            added["secret"] = sec_new
            log("saved passwords copied")
        base = {k: v for k, v in saved.items() if k != "port"}
        if old_port:
            base["port"] = int(old_port)
        res = cli.install(c, new, {k: str(v) for k, v in saved.items() if k.isupper() and v is not None},
                          _record=rec, _form_base=base)
        log(f"new app installed ({res.get('status')})")
        _record_source(c, new, rec)
        if res.get("status") == "running" and old_status == "stopped":
            cli.app_lifecycle(c, "stop", new)
        problem = _verify(c, rt, new, old_status, settle=settle, probe_seconds=probe_seconds)
        if problem:
            raise RenameError(problem)
        log("new app verified")
    except (Exception, SystemExit) as e:  # noqa: BLE001 - anything before the old app goes is rolled back
        return rollback(f"{type(e).__name__}: {e}"[:400] if not isinstance(e, RenameError) else str(e))

    # The new app works. Keep the old data under a dated name, then uninstall the old app.
    keep = f"{po['data']}.old-{time.strftime('%Y%m%d', time.gmtime())}"
    result = {"from": old, "to": new, "name": plan["name"], "renamed": True, "steps": steps, "backup": str(bdir),
              "port": old_port, "data": {"files": before["files"], "bytes": before["bytes"]}}
    try:
        if rt_exists(po["data"]):
            n = 1
            while rt_exists(keep if n == 1 else f"{keep}.{n}"):
                n += 1
            keep = keep if n == 1 else f"{keep}.{n}"
            rt_move(po["data"], keep)
            result["old_data"] = keep
        rt.uninstall(old_urn)
        st = rt.wait_status(old_urn, {"missing"}, 300)
        if st != "missing":
            raise RenameError(f"the old app is {st}")
        log("old app uninstalled")
        # The App Manager's own bookkeeping may have noted the old id again while it was going; point it at the new.
        g = rt_read(files["grants"])
        if g is not None:
            fixed = rename_in_grants(json.loads(g), old, new)
            if fixed != json.loads(g):
                rt_write(files["grants"], (json.dumps(fixed, indent=2) + "\n").encode())
    except Exception as e:  # noqa: BLE001 - the new app stays; the old one is left stopped with its data
        if result.get("old_data") and not rt_exists(po["data"]):
            try:
                rt_move(keep, po["data"])
                result.pop("old_data")
            except Exception:  # noqa: BLE001
                pass
        result["warning"] = f"{new} works, but {old} was not removed ({str(e)[:200]}); remove it with `wam uninstall {old}`"
    try:
        cli.network_step(c, rt)
        cli.write_registry(c, rt)
    except Exception as e:  # noqa: BLE001
        result.setdefault("warning", f"registry refresh: {str(e)[:160]}")
    (bdir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def rename_due(c=None, rt=None) -> list[dict]:
    """The nightly app-update run: move every installed app its source has renamed. One at a time, each verified
    or rolled back on its own."""
    from . import cli
    c = c or cli.Config()
    rt = rt or c.runtipi()
    out = []
    for d in due(c, rt):
        print(f"renaming {d['from']} -> {d['to']}", file=sys.stderr, flush=True)
        try:
            out.append(migrate(c, d["from"], d["to"]))
        except Exception as e:  # noqa: BLE001 - one app never stops the others
            out.append({"from": d["from"], "to": d["to"], "error": f"{type(e).__name__}: {e}"[:300]})
        out[-1].setdefault("name", d.get("name"))
    return out
