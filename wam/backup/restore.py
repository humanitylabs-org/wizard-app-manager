"""`wam backup verify|import|undo|activate`: restore or move a Wizard onto this box (docs/BACKUP.md, "Import").

Order of work, and what each step may touch:
 1. unlock     header + key slot. A wrong key or changed header stops here; nothing on the box is touched.
 2. stage      the whole stream is decrypted, every member checked (bundle.scan) and written into staging on the same
               filesystem as its destination: <wizard home>/restores/<stamp>/stage, and <files>/.wizard-restore/<stamp>/
               stage for the owner's files (that folder may be a mount point, so staging stays inside it). Any failure
               removes the staging and leaves the box exactly as it was.
 3. plan       manifest against this box: apps to install, apps to replace, apps left alone, agents, architecture.
 4. set aside  the undo snapshot. Wizard home entries, agent home entries and the owner's files are renamed into
               before/ (same filesystem, instant). App data belongs to the app containers and only Runtipi (root) may
               move it, so each app the restore replaces gets a Runtipi per-app backup first. Every step is written to
               journal.json before the next, so `undo` also works after a crash half way.
 5. apply      install missing apps from the bundled package, restore app data with Runtipi's own restore, import
               each agent with stock `hermes import --force` (keeping this box's Wizard wiring), move the Wizard home
               items and files in. This box's identity (api-token, mcp-token, runtipi-operator.json, agent-runtime/,
               alongside.json, linked.json, app-manager.json) is never replaced.
 6. one copy   messaging off (tokens kept), cron paused, scheduled backups copied but off with a new box id.
 7. start      regenerate apps.json/skills, start what ran at export, health-check, write receipt.json.

`undo` stops, moves the restored entries aside, moves before/ back, uninstalls apps the restore installed, restores
the replaced apps from their Runtipi snapshots, starts, and compares the digest with the one taken before.
`activate --old-is-off` turns on exactly the messaging platforms, cron jobs and backup schedule that were on at export.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import tarfile
import time
import uuid
from pathlib import Path

from . import agents as agentlib, bundle, fmt
from .box import (MAIN_AGENT, PER_MACHINE, RESTORE_DIR, STORE, Agent, Box, HermesError, RuntipiError, SpaceError,
                  box_digest, human, log, need_space, skip_home_entry)

MARGIN = 256 << 20


class RestoreError(RuntimeError):
    pass


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dst.exists() or dst.is_symlink():
        raise RestoreError(f"refusing to overwrite {dst}")
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))   # a different filesystem (e.g. an agent home on its own mount)


def _rm(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink(missing_ok=True)
    elif p.exists():
        shutil.rmtree(p)


# ---------------------------------------------------------------- open, verify

def _open(path: Path, keys: fmt.Keys):
    f = open(path, "rb")
    try:
        header, signed, mac = fmt.read_header(f)
        file_key = fmt.unlock(header, signed, mac, keys)
        return f, fmt.Reader(f, header, mac, file_key)
    except BaseException:
        f.close()
        raise


def verify(box: Box, path: Path, keys: fmt.Keys) -> dict:
    """Decrypt and check everything, write nothing."""
    t0 = time.time()
    f, reader = _open(path, keys)
    with f:
        res = bundle.scan(reader, None, progress=log)
    m = res["manifest"]
    return {"ok": True, "file": str(path), "bytes": path.stat().st_size, "entries": res["entries"],
            "content_bytes": res["bytes"], "seconds": round(time.time() - t0, 1), "manifest": summary(m),
            "plan": plan(box, m, probe=False)}


def verify_stream(stream, keys: fmt.Keys) -> dict:
    """Verify a backup read from any stream (the scheduled run's read-back from S3)."""
    header, signed, mac = fmt.read_header(stream)
    file_key = fmt.unlock(header, signed, mac, keys)
    reader = fmt.Reader(stream, header, mac, file_key)
    res = bundle.scan(reader, None)
    return {"ok": True, "entries": res["entries"], "content_bytes": res["bytes"],
            "manifest": summary(res["manifest"]), "cipher_bytes": reader.cipher_bytes}


def summary(m: dict) -> dict:
    return {"created_at": m.get("created_at"), "name": m.get("name"), "source": m.get("source"),
            "agents": [a.get("id") for a in m.get("agents", [])],
            "apps": [{"id": a.get("id"), "version": a.get("version"), "commit": a.get("commit")} for a in m.get("apps", [])],
            "files": m.get("files"),
            "active": {"agents": {k: {"messaging": len(v.get("messaging", [])), "cron": len(v.get("cron", []))}
                                  for k, v in (m.get("active", {}).get("agents") or {}).items()},
                       "schedule": (m.get("active") or {}).get("schedule")}}


# ---------------------------------------------------------------- plan

def plan(box: Box, m: dict, probe: bool = True) -> dict:
    out = {"mode_here": None, "mode_bundle": (m.get("source") or {}).get("mode"), "install": [], "replace": [],
           "leave": [], "agents": [], "warnings": []}
    try:
        out["mode_here"] = box.mode()
        here = box.installed() if probe else {}
    except (RuntipiError, OSError) as e:
        if probe:
            raise RestoreError(f"cannot reach Runtipi on this box: {e}") from None
        here = {}
    want = {a["urn"]: a for a in m.get("apps", [])}
    for urn, a in sorted(want.items()):
        (out["replace"] if urn in here else out["install"]).append(a["id"])
    out["leave"] = sorted(info["id"] for urn, info in here.items() if urn not in want)
    out["agents"] = [a.get("id") for a in m.get("agents", [])]
    arch = (m.get("source") or {}).get("arch")
    import platform as _p
    if arch and arch != _p.machine():
        out["warnings"].append(f"the backup was made on {arch}, this box is {_p.machine()}: apps must publish images for both")
    if out["mode_bundle"] and out["mode_here"] and out["mode_bundle"] != out["mode_here"]:
        out["warnings"].append(f"the backup is from a {out['mode_bundle']} install and this box is {out['mode_here']}: "
                               "the main agent is imported into this box's main agent")
    return out


# ---------------------------------------------------------------- import

class _Restore:
    def __init__(self, box: Box, stamp: str | None = None) -> None:
        self.box = box
        self.stamp = stamp or _now_stamp()
        self.root = box.home / "restores" / self.stamp
        self.files_root = box.files_root()
        self.froot = self.files_root / RESTORE_DIR / self.stamp
        self.journal: dict = {"stamp": self.stamp, "status": "staging", "steps": []}

    def save(self) -> None:
        _write_json(self.root / "journal.json", self.journal)

    def step(self, **kw) -> None:
        self.journal["steps"].append(kw)
        self.save()


def _space_check(box: Box, r: _Restore, m: dict) -> None:
    files = (m.get("files") or {}).get("bytes", 0) if (m.get("files") or {}).get("included") else 0
    rest = max(int(m.get("estimate_bytes") or 0) - files, 0)
    home_dev = os.stat(_existing(box.home)).st_dev
    files_dev = os.stat(_existing(r.files_root)).st_dev
    if home_dev == files_dev:
        need_space(box, box.home, rest + files + MARGIN, "restoring this backup")
    else:
        need_space(box, box.home, rest + MARGIN, "restoring this backup's apps, agents and settings")
        need_space(box, r.files_root, files + MARGIN, "restoring this backup's files")


def _existing(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def stage(box: Box, path: Path, keys: fmt.Keys, stamp: str | None = None) -> tuple[_Restore, dict]:
    """Unlock, verify and stage. On any failure everything this created is removed again."""
    f, reader = _open(path, keys)     # wrong key: raises here, nothing created yet
    r = _Restore(box, stamp)
    created = [p for p in (box.home / "restores", r.files_root / RESTORE_DIR, r.files_root) if not p.exists()]
    try:
        with f:
            def on_manifest(m):
                _space_check(box, r, m)
                r.root.mkdir(parents=True, mode=0o700)
                r.froot.mkdir(parents=True, mode=0o700)
            sink = bundle.StageSink(r.root / "stage", r.froot / "stage")
            res = bundle.scan(reader, sink, on_manifest=on_manifest, progress=log)
        r.journal["manifest"] = res["manifest"]
        return r, res
    except BaseException:
        shutil.rmtree(r.root, ignore_errors=True)
        shutil.rmtree(r.froot, ignore_errors=True)
        for p in created:   # deepest first: only folders this run made
            try:
                p.rmdir()
            except OSError:
                pass
        raise


def _home_entries(home: Path) -> list[str]:
    try:
        return sorted(n for n in os.listdir(home) if not skip_home_entry(n, (home / n).is_dir()))
    except FileNotFoundError:
        return []


def _files_entries(root: Path) -> list[str]:
    try:
        return sorted(n for n in os.listdir(root) if n != RESTORE_DIR)
    except FileNotFoundError:
        return []


def _bundle_agents(r: _Restore) -> list[dict]:
    return list(r.journal["manifest"].get("agents") or [])


def _target_agent(box: Box, a: dict, existing: dict[str, Agent]) -> Agent:
    """Where a bundled agent goes on this box: the main agent into this box's main agent (whatever its kind),
    an extra agent into <agents root>/<id> as Runtipi app agent-<id>."""
    if a.get("main") or a["id"] == MAIN_AGENT:
        if MAIN_AGENT in existing:
            return existing[MAIN_AGENT]
        home = box.runtipi_root / "app-data" / STORE / "hermes-agent" / "data"
        return Agent(MAIN_AGENT, True, home, "container", f"hermes-agent:{STORE}")
    if a["id"] in existing:
        return existing[a["id"]]
    folder = box.agents_root() / a["id"]
    return Agent(a["id"], False, folder / "home", "container", f"{a.get('app') or 'agent-' + a['id']}:{STORE}", folder)


def import_bundle(box: Box, path: Path, keys: fmt.Keys, *, yes: bool = False, confirm=None) -> dict:
    t0 = time.time()
    before_digest_agents = box.agents()
    log("checking the whole file before changing anything")
    r, res = stage(box, path, keys)
    m = r.journal["manifest"]
    try:
        p = plan(box, m)
    except BaseException:
        discard(r)
        raise
    if not yes:
        ok = confirm(p, m) if confirm else False
        if not ok:
            discard(r)
            return {"ok": False, "verified": True, "applied": False, "plan": p, "manifest": summary(m),
                    "error": "not confirmed: nothing was changed (pass --yes to restore without asking)"}
    r.journal.update({"status": "setting-aside", "file": str(path), "plan": p, "started_at": int(t0)})
    r.save()
    try:
        _apply(box, r, p, before_digest_agents)
    except BaseException as e:
        r.journal["status"] = "failed"
        r.journal["error"] = str(e)[:500]
        r.save()
        _write_json(box.home / "restores" / "last.json", {"stamp": r.stamp})
        raise RestoreError(f"the restore stopped part way: {e}. The previous Wizard is kept in "
                           f"{r.root / 'before'}; run `wam backup undo` to put it back") from e
    receipt = r.journal["receipt"]
    receipt["seconds"] = round(time.time() - t0, 1)
    _write_json(r.root / "receipt.json", receipt)
    return receipt


def discard(r: _Restore) -> None:
    shutil.rmtree(r.root, ignore_errors=True)
    shutil.rmtree(r.froot, ignore_errors=True)
    for p in (r.files_root / RESTORE_DIR, r.box.home / "restores"):
        try:
            p.rmdir()
        except OSError:
            pass


def _stop_agent(box: Box, a: Agent, installed: dict) -> bool:
    if a.runtime == "host":
        return box.runner.gateway(a, "stop")
    if a.urn in installed and installed[a.urn].get("status") == "running":
        box.rt.stop(a.urn)
        box.wait_status(a.urn, {"stopped"}, timeout=300)
        return True
    return False


def _start_agent(box: Box, a: Agent) -> None:
    if a.runtime == "host":
        box.runner.gateway(a, "start")
    else:
        box.rt.start(a.urn)


def _runtipi_snapshot(box: Box, urn: str) -> str:
    before = {b.get("id") for b in box.rt.list_backups(urn)}
    box.rt.backup_app(urn)
    st = box.wait_status(urn, set(), timeout=3 * 3600, not_in={"backing_up"})
    new = [b for b in box.rt.list_backups(urn) if b.get("id") not in before]
    if not new:
        raise RestoreError(f"Runtipi could not back up {urn} before the restore (status {st}); nothing replaced")
    return sorted(new, key=lambda b: b.get("date") or 0)[-1]["id"]


def _runtipi_restore(box: Box, urn: str, archive: Path, name: str) -> str:
    app_id, store = urn.split(":")
    dst_dir = box.runtipi_root / "backups" / store / app_id
    try:   # Runtipi's backups folder, when this user may write it; else Runtipi's own upload endpoint
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, dst_dir / name)
        placed = True
    except PermissionError:
        placed = False
    if not placed:
        box.rt.upload_backup(urn, archive, name)
    prior = box.app_status(urn)
    box.rt.restore_backup(urn, name)
    st = box.wait_status(urn, set(), timeout=3 * 3600, not_in={"restoring"})
    try:
        box.rt.delete_backup(urn, name)
    except RuntipiError:
        pass
    if prior == "running" and st != "running":
        st = box.wait_status(urn, {"running"}, timeout=600)
        if st != "running":
            raise RestoreError(f"Runtipi's restore of {urn} did not finish (status {st}); see Runtipi's app log")
    return st


def _uc(name: str) -> bool:
    while name.startswith("./"):
        name = name[2:]
    return name == "user-config" or name.startswith("user-config/")


def _keep_box_user_config(box: Box, urn: str, arch: Path, snapshot: str, stage: Path) -> Path:
    """The bundle's app-data/ and app/ with this box's own user-config/, taken from the Runtipi snapshot made just
    before (readable even when Runtipi wrote the live files as root)."""
    app_id, store = urn.split(":")
    snap = box.runtipi_root / "backups" / store / app_id / snapshot
    tmp_snap = None
    if not (snap.is_file() and os.access(snap, os.R_OK)):
        tmp_snap = stage / f"snapshot-{app_id}.tar.gz"
        box.rt.download_backup(urn, snapshot, tmp_snap)
        snap = tmp_snap
    out = arch.with_name("data-boxconfig.tar.gz")
    try:
        with tarfile.open(out, "w:gz") as w:
            for src, keep in ((arch, lambda n: not _uc(n)), (snap, _uc)):
                with tarfile.open(src, "r:gz") as t:
                    for m in t:
                        if keep(m.name):
                            w.addfile(m, t.extractfile(m) if m.isreg() else None)
        with open(out, "rb") as f:
            bundle.check_inner_archive(f)
    finally:
        if tmp_snap:
            tmp_snap.unlink(missing_ok=True)
    arch.unlink()
    return out


def _install_app(box: Box, r: _Restore, rec: dict) -> str:
    """Install a bundled app that this box doesn't have, from the bundled package at the same version."""
    urn, app_id, store = rec["urn"], rec["id"], rec["store"]
    stage = r.root / "stage" / "apps" / app_id
    pkg = stage / "package"
    meta = json.loads((stage / "app.json").read_text()) if (stage / "app.json").is_file() else {}
    repo_pkg = box.store_package(store, app_id)
    placed = False
    if pkg.is_dir() and not repo_pkg.exists():
        try:   # the App Manager writes this folder for its own store; Runtipi installs from it
            shutil.copytree(pkg, repo_pkg)
            placed = True
        except OSError as e:
            log(f"note: could not place the bundled package for {app_id}: {e}")
    uc = _highest_missing(box.runtipi_root / "user-config" / store / app_id, box.runtipi_root)
    info = {"user_config_new": str(uc) if uc else None, "package_placed": str(repo_pkg) if placed else None}
    r.step(kind="app_installing", urn=urn, **info)
    box.rt.install(urn, meta.get("form") or {})
    r.step(kind="app_installed", urn=urn, **info)
    st = box.wait_status(urn, {"running", "stopped"}, timeout=1800)
    if st not in ("running", "stopped"):
        raise RestoreError(f"installing {app_id} did not finish (status {st})")
    return st


def _apply(box: Box, r: _Restore, p: dict, current_agents: list[Agent]) -> None:
    m = r.journal["manifest"]
    stage = r.root / "stage"
    before = r.root / "before"
    installed = box.installed()
    r.journal["installed_before"] = {u: {"status": i.get("status")} for u, i in installed.items()}
    existing = {a.id: a for a in current_agents}
    targets = {a["id"]: _target_agent(box, a, existing) for a in _bundle_agents(r)}
    r.journal["digest_before"] = box_digest(box, current_agents)
    r.journal["agents"] = {k: {"home": str(a.home), "runtime": a.runtime, "urn": a.urn, "existed": k in existing,
                               "folder": str(a.folder) if a.folder else None, "main": a.main}
                           for k, a in targets.items()}
    r.save()

    # 4) set aside ------------------------------------------------------------
    apps = [a for a in m.get("apps", [])]
    data_apps = [a for a in apps if a.get("data") == "runtipi-backup"]
    for a in data_apps:
        if a["urn"] in installed:
            log(f"app {a['id']}: Runtipi snapshot for undo")
            name = _runtipi_snapshot(box, a["urn"])
            r.step(kind="app_snapshot", urn=a["urn"], backup=name)
    stopped = []
    for k, a in targets.items():
        if k in existing and _stop_agent(box, a, installed):
            stopped.append(k)
    r.journal["stopped_agents"] = stopped
    r.save()
    for n in _home_entries(box.home):
        _move(box.home / n, before / "wizard-os" / n)
    r.step(kind="set_aside", area="wizard-os")
    for k, a in targets.items():
        if k not in existing or not a.home.is_dir():
            continue
        for rel in agentlib.movable_entries(a.home):
            _move(a.home / rel, before / "agents" / k / "home" / rel)
        if a.folder and (a.folder / "agent.json").is_file():
            _move(a.folder / "agent.json", before / "agents" / k / "agent.json")
        r.step(kind="set_aside", area=f"agent:{k}")
    if (m.get("files") or {}).get("included"):
        for n in _files_entries(r.files_root):
            _move(r.files_root / n, r.froot / "before" / n)
        r.step(kind="set_aside", area="files")
    r.journal["status"] = "applying"
    r.save()

    # 5) apply ----------------------------------------------------------------
    for k, a in targets.items():
        if k not in existing:
            _prepare_agent_folder(r, k, a)
    for a in apps:
        if a["urn"] not in installed:
            log(f"app {a['id']}: installing the bundled version")
            _install_app(box, r, a)
    snapshots = {s["urn"]: s["backup"] for s in r.journal["steps"] if s["kind"] == "app_snapshot"}
    for a in data_apps:
        arch = stage / "apps" / a["id"] / "data.tar.gz"
        if arch.is_file():
            if a["urn"] in snapshots:
                # user-config/ is this box's wiring (alongside overrides, trusted origins, ports): keep it
                arch = _keep_box_user_config(box, a["urn"], arch, snapshots[a["urn"]], stage)
            log(f"app {a['id']}: restoring its data with Runtipi")
            _runtipi_restore(box, a["urn"], arch, f"{a['id']}-wizard-restore-{r.stamp}.tar.gz")
            r.step(kind="app_restored", urn=a["urn"])
            arch.unlink(missing_ok=True)

    # Wizard home (identity entries never come from a bundle, and the box keeps its own)
    src = stage / "wizard-os"
    if src.is_dir():
        for n in sorted(os.listdir(src)):
            if n in PER_MACHINE or skip_home_entry(n, (src / n).is_dir()):
                continue
            _move(src / n, box.home / n)
    _merge_back(before / "wizard-os", box.home)
    _schedule_off_after_restore(box)
    r.step(kind="placed", area="wizard-os")

    # agents
    agent_reports = {}
    for k, a in targets.items():
        agent_reports[k] = _import_agent(box, r, k, a, existing, installed)

    if (m.get("files") or {}).get("included"):
        fs = r.froot / "stage"
        for n in sorted(os.listdir(fs)) if fs.is_dir() else []:
            _move(fs / n, r.files_root / n)
        r.step(kind="placed", area="files")

    # 7) start and check -----------------------------------------------------
    notes = []
    if box.after_restore is not None:
        try:
            box.after_restore(box)
        except Exception as e:  # noqa: BLE001
            notes.append(f"regenerating apps.json/skills failed: {e}")
    else:
        notes += _regenerate(box)
    health = {}
    for a in apps:
        want_running = a.get("status") == "running"
        st = box.app_status(a["urn"])
        if want_running and st != "running":
            try:
                box.rt.start(a["urn"])
            except RuntipiError as e:
                notes.append(f"{a['id']}: start failed: {e}")
            st = box.wait_status(a["urn"], {"running"}, timeout=600)
        elif a.get("status") == "stopped" and st == "running" and a.get("data") != "agent":
            try:   # the owner had it stopped at export: keep it that way
                box.rt.stop(a["urn"])
            except RuntipiError as e:
                notes.append(f"{a['id']}: stop failed: {e}")
            st = box.wait_status(a["urn"], {"stopped"}, timeout=300)
        health[a["id"]] = st
    for k, a in targets.items():
        try:
            _start_agent(box, a)
        except (RuntipiError, HermesError) as e:
            notes.append(f"agent {k}: start failed: {e}")
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(r.froot / "stage", ignore_errors=True)
    kept = sorted(n for n in PER_MACHINE if (box.home / n).exists())
    receipt = {"ok": True, "stamp": r.stamp, "file": r.journal.get("file"), "restored_at": int(time.time()),
               "manifest": summary(m), "plan": p, "apps": health, "agents": agent_reports,
               "installed_by_restore": [s["urn"] for s in r.journal["steps"] if s["kind"] == "app_installed"],
               "kept_identity": kept, "active_at_export": m.get("active") or {},
               "schedule": "copied, off", "notes": notes, "undo": "wam backup undo",
               "activate": "wam backup activate --old-is-off"}
    r.journal["receipt"] = receipt
    r.journal["status"] = "restored"
    r.save()
    _write_json(box.home / "restores" / "last.json", {"stamp": r.stamp})


MERGE_DIRS = ("secrets", "app-tokens")


def _merge_back(held: Path, home: Path) -> None:
    """Per-app secrets and tokens of apps the backup doesn't carry (apps this box keeps) stay usable: entries of
    secrets/ and app-tokens/ missing from the bundle are copied back from the set-aside copy (copied, so the
    undo snapshot stays whole)."""
    for d in MERGE_DIRS:
        src = held / d
        if not src.is_dir():
            continue
        dst = home / d
        if not dst.exists():
            continue
        for n in sorted(os.listdir(src)):
            if not (dst / n).exists() and (src / n).is_file() and not (src / n).is_symlink():
                shutil.copy2(src / n, dst / n)


def _schedule_off_after_restore(box: Box) -> None:
    from . import schedule
    schedule.after_restore(box)


def _regenerate(box: Box) -> list[str]:
    try:
        from .. import cli
        cli.write_registry(cli.Config(), box.rt)
        return []
    except (Exception, SystemExit) as e:  # noqa: BLE001
        return [f"regenerating apps.json/skills failed: {e}; run `wam apps`"]


def _highest_missing(path: Path, stop: Path) -> Path | None:
    """The outermost folder of `path` (below `stop`) that doesn't exist yet: what creating `path` adds."""
    path, stop = Path(path), Path(stop)
    if path.exists():
        return None
    top = path
    while top.parent != stop and top.parent != top.parent.parent and not top.parent.exists():
        top = top.parent
    return top


def _prepare_agent_folder(r: _Restore, k: str, a: Agent) -> None:
    """A new extra agent: its folder and home exist before its app is installed (so the bind mount is the owner's),
    and its small files come from the bundle (runtime.key etc. match the imported .env)."""
    if not a.folder:
        if not a.home.exists():
            a.home.mkdir(parents=True)
            r.step(kind="agent_folder_created", agent=k, path=str(a.home))
        return
    if not a.folder.exists():
        top = _highest_missing(a.folder, r.box.home)
        a.folder.mkdir(parents=True, mode=0o700)
        r.step(kind="agent_folder_created", agent=k, path=str(top or a.folder))
    fsrc = r.root / "stage" / "agents" / k / "folder"
    for n in sorted(os.listdir(fsrc)) if fsrc.is_dir() else []:
        if not (a.folder / n).exists():
            _move(fsrc / n, a.folder / n)
    a.home.mkdir(parents=True, exist_ok=True)


def _import_agent(box: Box, r: _Restore, k: str, a: Agent, existing: dict, installed: dict) -> dict:
    stage = r.root / "stage" / "agents" / k
    zip_in = stage / "hermes-backup.zip"
    rep: dict = {"target": a.runtime, "app": (a.urn or ":").split(":")[0] or None}
    if a.folder and (stage / "agent.json").is_file():
        meta = json.loads((stage / "agent.json").read_text())
        if isinstance(meta, dict) and meta.get("id") == k:
            _move(stage / "agent.json", a.folder / "agent.json")
    a.home.mkdir(parents=True, exist_ok=True)
    if a.runtime == "container" and a.urn and a.urn not in installed and a.urn not in \
            [s.get("urn") for s in r.journal["steps"] if s["kind"] == "app_installed"]:
        rec = next((x for x in r.journal["manifest"].get("apps", []) if x["urn"] == a.urn), None)
        if not rec:
            raise RestoreError(f"agent {k}: its app {a.urn} is neither on this box nor in the backup")
        _install_app(box, r, rec)
    if a.runtime == "container" and box.app_status(a.urn) == "running":
        box.rt.stop(a.urn)
        box.wait_status(a.urn, {"stopped"}, timeout=300)
    # wiring: what this box's own agent home had (read from the set-aside copy)
    held = r.root / "before" / "agents" / k / "home"
    wiring = agentlib.wizard_wiring(held) if k in existing else {"present": False}
    env_lines = agentlib.wizard_env_lines(held) if k in existing else []
    filtered = r.root / "stage" / f"agent-{k}.zip"
    rep["archive"] = agentlib.check_and_filter(zip_in, filtered)
    zip_in.unlink()
    log(f"agent {k}: hermes import")
    box.runner.import_zip(a, filtered)
    filtered.unlink(missing_ok=True)      # the archive is deleted after import
    if wiring.get("present"):
        rep["replaced_env_keys"] = agentlib.env_merge(a.home, env_lines)
    out = box.runner.python(a, agentlib.POST_IMPORT, json.dumps(wiring))
    rep.update(agentlib.last_json_line(out))
    rep["wiring_kept"] = bool(wiring.get("present"))
    r.step(kind="agent_imported", agent=k)
    return rep


# ---------------------------------------------------------------- undo

def last_restore(box: Box) -> tuple[Path, dict]:
    try:
        stamp = json.loads((box.home / "restores" / "last.json").read_text())["stamp"]
        root = box.home / "restores" / stamp
        return root, json.loads((root / "journal.json").read_text())
    except (OSError, ValueError, KeyError):
        raise RestoreError("there is no restore to undo or activate on this box") from None


def undo(box: Box) -> dict:
    root, j = last_restore(box)
    if j.get("status") == "undone":
        raise RestoreError(f"the last restore ({j['stamp']}) was already undone")
    if j.get("status") == "staging":
        raise RestoreError("the last restore never changed anything")
    stamp = j["stamp"]
    files_root = box.files_root()
    froot = files_root / RESTORE_DIR / stamp
    before, undone = root / "before", root / "undone"
    steps = j.get("steps", [])
    areas = {s["area"] for s in steps if s["kind"] == "set_aside"}
    notes = []
    targets = {k: Agent(k, v.get("main", False), Path(v["home"]), v["runtime"], v.get("urn"),
                        Path(v["folder"]) if v.get("folder") else None) for k, v in (j.get("agents") or {}).items()}
    # stop the restored agents
    for k, a in targets.items():
        try:
            if a.runtime == "host":
                box.runner.gateway(a, "stop")
            elif box.app_status(a.urn) == "running":
                box.rt.stop(a.urn)
                box.wait_status(a.urn, {"stopped"}, timeout=300)
        except (RuntipiError, HermesError) as e:
            notes.append(f"agent {k}: stop: {e}")
    # Wizard home
    if "wizard-os" in areas:
        for n in _home_entries(box.home):
            _move(box.home / n, undone / "wizard-os" / n)
        for n in sorted(os.listdir(before / "wizard-os")) if (before / "wizard-os").is_dir() else []:
            _move(before / "wizard-os" / n, box.home / n)
    # agents
    for k, a in targets.items():
        if f"agent:{k}" in areas:
            for rel in agentlib.movable_entries(a.home):
                _move(a.home / rel, undone / "agents" / k / "home" / rel)
            held = before / "agents" / k / "home"
            for rel in _walk_back(held):
                _move(held / rel, a.home / rel)
            if (before / "agents" / k / "agent.json").is_file() and a.folder:
                if (a.folder / "agent.json").exists():
                    _move(a.folder / "agent.json", undone / "agents" / k / "agent.json")
                _move(before / "agents" / k / "agent.json", a.folder / "agent.json")
    for s in steps:
        if s["kind"] == "agent_folder_created" and Path(s["path"]).exists():
            _move(Path(s["path"]), undone / "agent-folders" / s["agent"] / Path(s["path"]).name)
    # files
    if "files" in areas:
        for n in _files_entries(files_root):
            _move(files_root / n, froot / "undone" / n)
        fb = froot / "before"
        for n in sorted(os.listdir(fb)) if fb.is_dir() else []:
            _move(fb / n, files_root / n)
    # apps: uninstall what the restore installed, put back what it replaced
    done = {s["urn"] for s in steps if s["kind"] == "app_installed"}
    for s in reversed(steps):
        if s["kind"] in ("app_installed", "app_installing") and (s["kind"] == "app_installed" or s["urn"] not in done):
            if s["kind"] == "app_installing" and box.app_status(s["urn"]) == "missing":
                pass   # the install never registered
            else:
                log(f"uninstalling {s['urn']} (installed by the restore)")
                try:
                    box.rt.uninstall(s["urn"])
                    box.wait_status(s["urn"], {"missing"}, timeout=900)
                except RuntipiError as e:
                    notes.append(f"{s['urn']}: uninstall failed: {e}")
            # Runtipi's uninstall keeps user-config/; the restore brought it, so it goes too
            uc = Path(s["user_config_new"]) if s.get("user_config_new") else None
            if uc and uc.exists():
                try:
                    shutil.rmtree(uc)
                except OSError as e:
                    notes.append(f"{s['urn']}: could not remove {uc}: {e}")
            if s.get("package_placed"):
                shutil.rmtree(s["package_placed"], ignore_errors=True)
    for s in steps:
        if s["kind"] == "app_snapshot":
            log(f"putting back {s['urn']}")
            try:
                prior = box.app_status(s["urn"])
                box.rt.restore_backup(s["urn"], s["backup"])
                st = box.wait_status(s["urn"], set(), timeout=3 * 3600, not_in={"restoring"})
                if prior == "running" and st != "running":
                    box.wait_status(s["urn"], {"running"}, timeout=600)
                box.rt.delete_backup(s["urn"], s["backup"])
            except RuntipiError as e:
                notes.append(f"{s['urn']}: restore of the snapshot failed: {e}")
    # start what ran before
    was = j.get("installed_before") or {}
    for urn, info in was.items():
        if info.get("status") == "running" and box.app_status(urn) != "running":
            try:
                box.rt.start(urn)
            except RuntipiError as e:
                notes.append(f"{urn}: start failed: {e}")
    for k in j.get("stopped_agents") or []:
        a = targets.get(k)
        if a and a.runtime == "host":
            box.runner.gateway(a, "start")
    if box.after_restore is not None:
        try:
            box.after_restore(box)
        except Exception as e:  # noqa: BLE001
            notes.append(f"regenerating apps.json failed: {e}")
    else:
        notes += _regenerate(box)
    current = [a for a in box.agents() if a.id in targets or a.runtime == "host"]
    after = box_digest(box, current)
    want = j.get("digest_before") or {}
    mismatch = sorted(k for k in set(want) | set(after) if want.get(k) != after.get(k))
    j["status"] = "undone"
    j["undone_at"] = int(time.time())
    j["undo_digest_match"] = not mismatch
    _write_json(root / "journal.json", j)
    if not mismatch:
        shutil.rmtree(undone, ignore_errors=True)
        shutil.rmtree(before, ignore_errors=True)
        shutil.rmtree(froot, ignore_errors=True)
        for p in (files_root / RESTORE_DIR,):
            try:
                p.rmdir()
            except OSError:
                pass
    return {"ok": not mismatch, "stamp": stamp, "digest_match": not mismatch, "mismatch": mismatch,
            "notes": notes, "kept": None if not mismatch else str(undone)}


def _walk_back(held: Path) -> list[str]:
    """Entries to move back into an agent home: top-level ones, descending into folders that exist in the home
    because they hold kept per-machine entries (plugins/)."""
    out = []

    def walk(d: Path, prefix: str) -> None:
        for n in sorted(os.listdir(d)) if d.is_dir() else []:
            rel = prefix + n
            if agentlib._keep_parent(rel) and (d / n).is_dir():
                walk(d / n, rel + "/")
            else:
                out.append(rel)
    walk(held, "")
    return out


# ---------------------------------------------------------------- activate

def activate(box: Box, old_is_off: bool) -> dict:
    if not old_is_off:
        raise RestoreError("confirm the old Wizard is off first: run `wam backup activate --old-is-off` "
                           "(both copies answering messages or running jobs would double everything)")
    root, j = last_restore(box)
    if j.get("status") != "restored":
        raise RestoreError(f"the last restore is {j.get('status')}; nothing to activate")
    if j.get("activated_at"):
        raise RestoreError("this restore was already activated")
    active = (j.get("manifest") or {}).get("active") or {}
    out = {"ok": True, "stamp": j["stamp"], "agents": {}, "schedule": False, "notes": []}
    for k, v in (j.get("agents") or {}).items():
        want = (active.get("agents") or {}).get(k) or {"messaging": [], "cron": []}
        a = Agent(k, v.get("main", False), Path(v["home"]), v["runtime"], v.get("urn"),
                  Path(v["folder"]) if v.get("folder") else None)
        rep = agentlib.last_json_line(box.runner.python(a, agentlib.ACTIVATE, json.dumps(want)))
        out["agents"][k] = rep
        try:
            if a.runtime == "host":
                box.runner.gateway(a, "restart")
            else:
                box.rt.restart(a.urn)
        except (RuntipiError, HermesError) as e:
            out["notes"].append(f"agent {k}: restart: {e}")
    if active.get("schedule"):
        from . import schedule
        out["schedule"] = schedule.enable(box)["enabled"]
    j["activated_at"] = int(time.time())
    j["activation"] = out
    _write_json(root / "journal.json", j)
    return out


__all__ = ["verify", "verify_stream", "import_bundle", "undo", "activate", "RestoreError", "SpaceError", "human"]
