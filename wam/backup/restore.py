"""`wam backup verify|import|undo|activate`: restore or move a Wizard onto this box (docs/BACKUP.md, "Import").

Order of work, and what each step may touch:
 1. unlock     header + key slot. A wrong key or changed header stops here; nothing on the box is touched.
 2. stage      the whole stream is decrypted, every member checked (bundle.scan) and written into staging on the same
               filesystem as its destination: <wizard home>/restores/<stamp>/stage, and <files>/.wizard-restore/<stamp>/
               stage for the owner's files (that folder may be a mount point, so staging stays inside it). Any failure
               removes the staging and leaves the box exactly as it was.
 3. plan       manifest against this box: apps to install, apps to replace, apps left alone, agents, architecture.
 4. set aside  the undo snapshot. Wizard home entries, agent home entries and the owner's files are renamed into
               before/ (same filesystem, instant). App data belongs to the app containers (root), so each app the
               restore replaces is stopped and archived as root first (appdata.py), and so is every named volume the
               restore will overwrite. Every step is written to journal.json before the next, so `undo` also works
               after a crash half way.
 5. apply      install missing apps from the bundled package, put app data back as root (this box keeps its own
               user-config/ for apps it already had, and the Agent Service keeps its agents folder), import
               each agent with stock `hermes import --force` (keeping this box's Wizard wiring), move the Wizard home
               items and files in. This box's identity (api-token, mcp-token, runtipi-operator.json, agent-runtime/,
               alongside.json, linked.json, app-manager.json) is never replaced.
 6. one copy   messaging off (tokens kept), cron paused, scheduled backups copied but off with a new box id.
 7. start      regenerate apps.json/skills, start what ran at export, health-check, write receipt.json.

`undo` stops, moves the restored entries aside, moves before/ back, uninstalls apps the restore installed, puts the
replaced apps and volumes back from their snapshots, starts, and compares the digest with the one taken before.
`activate --old-is-off` turns on exactly the messaging platforms, cron jobs and backup schedule that were on at export.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .. import slots
from . import agents as agentlib, bundle, fmt, versions
from .appdata import ROOTS as APP_ROOTS, AppDataError
from .box import (MAIN_AGENT, PER_MACHINE, RESTORE_DIR, STORE, Agent, Box, HermesError, RuntipiError, SpaceError,
                  box_digest, digest_differences, human, log, need_space, skip_home_entry)

MARGIN = 256 << 20


class RestoreError(RuntimeError):
    pass


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _mkdir_durable(path: Path) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in reversed(missing):
        _sync_dir(directory)
        _sync_dir(directory.parent)


def _write_json(path: Path, obj: dict) -> None:
    _mkdir_durable(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _sync_dir(path.parent)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _move(src: Path, dst: Path) -> None:
    _mkdir_durable(dst.parent)
    if dst.exists() or dst.is_symlink():
        raise RestoreError(f"refusing to overwrite {dst}")
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # A copy+delete fallback is not an atomic set-aside. Refuse without
        # touching the original rather than leave an ambiguous partial copy.
        raise RestoreError(f"cannot atomically move {src} to {dst}: different filesystems") from e


def _journal_move(j: dict, save, kind: str, area: str, src: Path, dst: Path) -> None:
    """Write intent durably before rename; reconcile a crash after rename on retry."""
    row = next((s for s in j["steps"] if s["kind"] == kind and s.get("src") == str(src)
                and s.get("dst") == str(dst)), None)
    if row is None:
        row = {"kind": kind, "area": area, "src": str(src), "dst": str(dst), "state": "pending"}
        j["steps"].append(row)
        save()
    if row["state"] == "done":
        return
    source = src.exists() or src.is_symlink()
    dest = dst.exists() or dst.is_symlink()
    if source and not dest:
        _move(src, dst)
    elif source or not dest:
        raise RestoreError(f"cannot reconcile {kind}: {src} -> {dst}; recovery copies kept")
    _sync_dir(src.parent)
    _sync_dir(dst.parent)
    row["state"] = "done"
    save()


def _aside(r: _Restore, area: str, src: Path, dst: Path) -> None:
    _journal_move(r.journal, r.save, "set_aside_entry", area, src, dst)


def _undo_journaled_trees(box: Box, root: Path, j: dict, targets: dict, files_root: Path, froot: Path) -> None:
    """Freeze the undo move plan once. Retrying never sets aside an already restored original."""
    save = lambda: _write_json(root / "journal.json", j)
    if "undo_moves" not in j:
        complete = {s["area"] for s in j["steps"] if s["kind"] == "set_aside"}
        originals = [s for s in j["steps"] if s["kind"] == "set_aside_entry"
                     and (Path(s["dst"]).exists() or Path(s["dst"]).is_symlink())]
        current = []
        if "wizard-os" in complete:
            current += [("wizard-os", box.home / n, root / "undone/wizard-os" / n)
                        for n in _home_entries(box.home)]
        for k, a in targets.items():
            if f"agent:{k}" in complete:
                current += [(f"agent:{k}", a.home / n, root / "undone/agents" / k / "home" / n)
                            for n in agentlib.movable_entries(a.home)]
                if a.folder and (a.folder / "agent.json").is_file():
                    current.append((f"agent:{k}", a.folder / "agent.json",
                                    root / "undone/agents" / k / "agent.json"))
        if "agents-root" in complete and j.get("agents_root"):
            ar = Path(j["agents_root"])
            current += [("agents-root", ar / n, root / "undone/agents-root" / n) for n in agentlib.root_files(ar)]
        if "files" in complete:
            current += [("files", files_root / n, froot / "undone" / n) for n in _files_entries(files_root)]
        # An interrupted set-aside has not started applying: untouched entries
        # must stay in place. Only an original actually held needs restoring.
        known = {str(src) for _, src, _ in current}
        for s in originals:
            src = Path(s["src"])
            if str(src) not in known and (src.exists() or src.is_symlink()):
                raise RestoreError(f"both original and set-aside copy exist: {src}; recovery copies kept")
        j["undo_moves"] = [dict(kind="undo_stash", area=a, src=str(s), dst=str(d)) for a, s, d in current]
        j["undo_moves"] += [dict(kind="undo_restore", area=s["area"], src=s["dst"], dst=s["src"])
                            for s in originals]
        save()
    for s in j["undo_moves"]:
        _journal_move(j, save, s["kind"], s["area"], Path(s["src"]), Path(s["dst"]))


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

SYSTEM_SLOTS = {"shell", "agent-service"}


def _app_slots(a: dict) -> set[str]:
    # Old registries/archives can predate `provides`; known default ids still fill their slots.
    fallback = {"wizard-ux": ("shell",), "wizard-agent-service": ("agent-service",)}
    return set(a.get("provides") or ()) | set(slots.BUNDLE_FILLS.get(a["id"], ())) | set(fallback.get(a["id"], ()))


def _here_apps(box: Box, installed: dict, extra=()) -> list[dict]:
    rows = box.registry_rows()
    for a in [*extra, *installed.values()]:
        previous = rows.get(a["id"], {})
        rows[a["id"]] = {**previous, **a,
                          "provides": sorted(_app_slots(previous) | _app_slots(a)) if previous else sorted(_app_slots(a))}
    return [{**rows[a["id"]], **a, "provides": rows[a["id"]]["provides"]} for a in installed.values()]


def _one_agent_service(apps: list[dict]) -> None:
    providers = sorted({a["urn"] for a in apps if "agent-service" in _app_slots(a)})
    if len(providers) > 1:
        raise RestoreError("more than one Agent Service would be installed: " + ", ".join(providers) +
                           "; keep one provider before restoring or activating")


def _here_release(box: Box, urn: str, installed_here: bool) -> dict | None:
    """The release of an app this box runs (installed package, else its apps.json row) or would install (its store
    package). None: not known until Runtipi installs it (a public app is fetched from the App Index then)."""
    app_id, store = urn.split(":")[0], urn.split(":")[1]
    if installed_here or box.app_package(store, app_id).is_dir():
        rel = versions.package_release(box.app_package(store, app_id))
        row = box.registry_rows().get(app_id) or {}
        if row.get("urn", urn) == urn:
            rel = {"version": (rel or {}).get("version") or (str(row["version"]) if row.get("version") else None),
                   "commit": (rel or {}).get("commit") or row.get("commit") or None}
        return rel or {"version": None, "commit": None}
    return versions.package_release(box.store_package(store, app_id))


def _version_check(box: Box, a: dict, installed_here: bool, stage: Path | None) -> dict:
    have = versions.backed_up(a, stage / "apps" / a["id"] / "package" if stage else None)
    return versions.check(a, have, _here_release(box, a["urn"], installed_here))


def plan(box: Box, m: dict, probe: bool = True, stage: Path | None = None) -> dict:
    """What a restore of manifest `m` does on this box. An app whose backup is newer than this box's release is
    `refused` (wizard-os#91): not installed, its data not put back, this box's copy left as it is."""
    out = {"mode_here": None, "mode_bundle": (m.get("source") or {}).get("mode"), "install": [], "replace": [],
           "leave": [], "agents": [], "warnings": [], "kept_here": [], "refused": [], "agents_skipped": []}
    try:
        out["mode_here"] = box.mode()
        here = box.installed() if probe else {}
    except (RuntipiError, OSError) as e:
        if probe:
            raise RestoreError(f"cannot reach Runtipi on this box: {e}") from None
        here = {}
    here_apps = _here_apps(box, here)
    have = {s: sorted({a["id"] for a in here_apps if s in _app_slots(a)}) for s in SYSTEM_SLOTS}
    want = {}
    for a in m.get("apps", []):
        provided = _app_slots(a)
        kept = set()
        for s in sorted(provided & SYSTEM_SLOTS):
            if have[s] and a["id"] not in have[s]:
                dest = have[s][0]
                out["kept_here"].append({"slot": s, "here": dest, "bundle": a["id"]})
                out["warnings"].append(f"keeping this box's {dest} for {s} instead of the backup's {a['id']}")
                kept.add(s)
        if not provided or not provided <= kept:
            want[a["urn"]] = a
    for urn, a in sorted(want.items()):
        v = _version_check(box, a, urn in here, stage)
        if v["verdict"] == "refuse":
            out["refused"].append(v)
            continue
        if v["verdict"] == "warn" and v["here"] is not None:
            out["warnings"].append(v["message"])
        (out["replace"] if urn in here else out["install"]).append(a["id"])
    mapped = {k["here"] for k in out["kept_here"] if k["slot"] == "agent-service"}
    out["leave"] = sorted(info["id"] for urn, info in here.items() if urn not in want and info["id"] not in mapped)
    for dest in sorted(mapped):
        out["warnings"].append(f"the backup's Agent Service data will go into {dest}; it stays stopped until activate")
    refused = {x["id"] for x in out["refused"]}
    out["agents"] = [a.get("id") for a in m.get("agents", []) if a.get("app") not in refused]
    out["agents_skipped"] = [a.get("id") for a in m.get("agents", []) if a.get("app") in refused]
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
    with f:
        return stage_stream(box, reader, stamp)


def stage_stream(box: Box, stream, stamp: str | None = None) -> tuple[_Restore, dict]:
    """Verify and stage a plain bundle stream (the decrypted file, or a snapshot read back from the restic
    repository). On any failure everything this created is removed again."""
    r = _Restore(box, stamp)
    created = [p for p in (box.home / "restores", r.files_root / RESTORE_DIR, r.files_root) if not p.exists()]
    try:
        def on_manifest(m):
            _space_check(box, r, m)
            r.root.mkdir(parents=True, mode=0o700)
            r.froot.mkdir(parents=True, mode=0o700)
        sink = bundle.StageSink(r.root / "stage", r.froot / "stage")
        res = bundle.scan(stream, sink, on_manifest=on_manifest, progress=log)
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


# A new import may start only when the last one ended in one of these: anything else (setting-aside, applying,
# failed, undo-failed, or an undo that stopped part way) leaves the box half restored, and a second import would set
# that half-restored state aside as its "before" and point restores/last.json away from the real original.
SETTLED = {"restored", "undone"}
NEVER_STARTED = {"staging"}   # nothing was set aside yet (`undo` refuses it as "never changed anything")


def check_can_import(box: Box) -> None:
    if not (box.home / "restores" / "last.json").exists():
        return
    try:
        _, j = last_restore(box)
    except RestoreError:
        raise RestoreError("restores/last.json names a restore whose journal cannot be read; refusing to import over "
                           "it (the previous Wizard may still be set aside there)") from None
    st = j.get("status")
    if st in NEVER_STARTED:
        return
    if st not in SETTLED or (st != "undone" and j.get("undo_error")):
        why = f"its undo stopped part way ({j['undo_error']})" if st in SETTLED else f"it is {st}"
        raise RestoreError(f"the last restore ({j.get('stamp')}) did not finish: {why}. Nothing was changed. Run "
                           f"`wam backup undo` to put the previous Wizard back first; a new import now would set the "
                           f"half-restored box aside instead of the original")


def import_bundle(box: Box, path: Path, keys: fmt.Keys | None, *, yes: bool = False, confirm=None,
                  stager=None) -> dict:
    """`stager()` (optional) replaces the file's unlock+stage with another verified source (a restic snapshot:
    wam/backup/snapshot.py); everything after staging is the same path."""
    t0 = time.time()
    check_can_import(box)
    before_digest_agents = box.agents()
    log("checking the whole file before changing anything")
    r, res = stager() if stager is not None else stage(box, path, keys)
    m = r.journal["manifest"]
    try:
        p = plan(box, m, stage=r.root / "stage")
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
    # Publish the recovery pointer BEFORE the first mutation, not only in an
    # exception handler (SIGKILL/power loss never runs that handler).
    _write_json(box.home / "restores" / "last.json", {"stamp": r.stamp})
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
    if receipt.get("refused"):
        _write_json(r.root / "journal.json", r.journal)
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
    if a.urn in installed and installed[a.urn].get("status") in LIVE:
        box.rt.stop(a.urn)
        box.wait_status(a.urn, {"stopped"}, timeout=300)
        return True
    return False


def _start_agent(box: Box, a: Agent) -> None:
    if a.runtime == "host":
        box.runner.gateway(a, "start")
    else:
        box.rt.start(a.urn)


DATA_KINDS = ("archive", "runtipi-backup")     # "runtipi-backup": bundles made before appdata.py


# An app Runtipi reports in any of these states has containers up (it says "starting" until health checks pass).
LIVE = ("running", "starting", "updating")


def _stop_app(box: Box, urn: str) -> str:
    st = box.app_status(urn)
    if st in LIVE:
        box.rt.stop(urn)
        st = box.wait_status(urn, {"stopped"}, timeout=600)
        if st != "stopped":
            raise RestoreError(f"{urn} did not stop (status {st}); nothing of it was replaced")
    return st


def _snapshot_app(box: Box, r: "_Restore", a: dict) -> None:
    """The undo copy of an app this restore replaces: stopped, then its folders and named volumes archived as root
    into before/apps/ (inside the Wizard home, so on the same disk as the restore's other set-aside copies)."""
    store, app_id = a["urn"].split(":")[1], a["urn"].split(":")[0]
    _stop_app(box, a["urn"])
    out = r.root / "before" / "apps" / f"{app_id}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        rep = box.appdata.archive(store, app_id, out)
    except AppDataError as e:
        raise RestoreError(f"could not keep an undo copy of {app_id}: {e}; nothing of it was replaced") from None
    r.step(kind="app_snapshot", urn=a["urn"], archive=str(out), volumes=rep["volumes"], skipped=len(rep["skipped"]))


def _snapshot_volumes(box: Box, r: "_Restore", a: dict, taken: set[str]) -> None:
    """Named volumes an app the restore installs would overwrite, when this box already has them (shared with an
    app it keeps, an AI account login say)."""
    vols = [v for v in (a.get("volumes") or []) if v not in taken and box.appdata.volume_exists(v)]
    if not vols:
        return
    out = r.root / "before" / "volumes" / f"{a['id']}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        box.appdata.archive_volumes(vols, out)
    except AppDataError as e:
        raise RestoreError(f"could not keep an undo copy of volumes {', '.join(vols)}: {e}") from None
    taken.update(vols)
    r.step(kind="volume_snapshot", urn=a["urn"], archive=str(out), volumes=vols)


def _path_rewrite(box: Box, m: dict) -> list[tuple[str, str]]:
    """(source path, this box's path) pairs, most specific first, for compose overrides carried in user-config/."""
    src = ((m.get("source") or {}).get("paths")) or {}
    here = {"agents_root": str(box.agents_root()), "runtipi_root": str(box.runtipi_root),
            "wizard_home": str(box.home), "home": str(box.home.parent)}
    pairs = [(src[k], here[k]) for k in ("agents_root", "runtipi_root", "wizard_home", "home")
             if src.get(k) and src[k] != here[k] and len(src[k]) > 1]
    return sorted(pairs, key=lambda p: -len(p[0]))


def _put_back(box: Box, urn: str, archive: Path, parts=APP_ROOTS, rewrite=(), *, data_only=False) -> dict:
    store, app_id = urn.split(":")[1], urn.split(":")[0]
    try:
        return box.appdata.restore(store, app_id, archive, parts=parts, rewrite=rewrite, data_only=data_only)
    except AppDataError as e:
        raise RestoreError(str(e)) from None


def _install_app(box: Box, r: _Restore, rec: dict) -> str:
    """Install a bundled app that this box doesn't have, from the bundled package at the same version."""
    urn, app_id, store = rec["urn"], rec["id"], rec["store"]
    stage = r.root / "stage" / "apps" / app_id
    pkg = stage / "package"
    meta = json.loads((stage / "app.json").read_text()) if (stage / "app.json").is_file() else {}
    repo_pkg = box.store_package(store, app_id)
    placed = False
    if pkg.is_dir() and not repo_pkg.exists():
        # Runtipi installs from its store folder (repos/<store>/apps/<id>). A public app is fetched there from the
        # App Index at install time; a private or local app (an extra agent's package) exists only in the bundle.
        # The folder is root's on a fresh box, so the package is placed as root.
        try:
            box.appdata.place_package(store, app_id, pkg)
            placed = True
        except AppDataError as e:
            log(f"note: could not place the bundled package for {app_id}: {e}")
    uc = _highest_missing(box.runtipi_root / "user-config" / store / app_id, box.runtipi_root)
    info = {"user_config_new": str(uc) if uc else None, "package_placed": str(repo_pkg) if placed else None}
    r.step(kind="app_installing", urn=urn, **info)
    arch = stage / "data.tar.gz"
    if arch.is_file():
        # the bundle's user-config/ goes in first: Runtipi's install starts the app with its compose override (an
        # extra agent's home mount, say); without it the first start creates folders at the package's defaults
        _put_back(box, urn, arch, ("user-config",), _path_rewrite(box, r.journal["manifest"]))
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
    refused_ids = {x["id"] for x in p.get("refused") or []}
    # an agent whose app is refused is not imported: its home on this box stays as it is
    targets = {a["id"]: _target_agent(box, a, existing) for a in _bundle_agents(r) if a.get("app") not in refused_ids}
    r.journal["agents"] = {k: {"home": str(a.home), "runtime": a.runtime, "urn": a.urn, "existed": k in existing,
                               "folder": str(a.folder) if a.folder else None, "main": a.main}
                           for k, a in targets.items()}
    apps = [a for a in m.get("apps", []) if a["id"] in p["install"] + p["replace"]]
    here_apps = _here_apps(box, installed)
    # Runtipi install starts containers: reject conflicts BEFORE even the first install, then check again below.
    projected = {a["urn"]: a for a in here_apps}
    projected.update({a["urn"]: a for a in apps})
    _one_agent_service(_here_apps(box, projected, [*here_apps, *apps]))
    mappings = []
    for kept in p["kept_here"]:
        if kept["slot"] != "agent-service":
            continue
        source = next(a for a in m["apps"] if a["id"] == kept["bundle"])
        dest = next(a for a in here_apps if a["id"] == kept["here"])
        if source.get("data") in DATA_KINDS:
            mappings.append((source, dest))
    if len(mappings) > 1:
        raise RestoreError("the backup has multiple Agent Service states; choose one before restoring")
    data_apps = [a for a in apps if a.get("data") in DATA_KINDS]
    replaced = list({a["urn"]: a for a in [
        *[a for a in data_apps if a["urn"] in installed], *[dest for _, dest in mappings]]}.values())
    if replaced:
        need = sum(bundle.tree_size(box.runtipi_root / "app-data" / a["store"] / a["id"])["bytes"] for a in replaced)
        need_space(box, r.root, need + MARGIN, "the undo copies of the apps this restore replaces")
    r.save()

    r.journal["move_journal_version"] = 1
    r.save()
    # 4) set aside ------------------------------------------------------------
    # Everything the restore replaces is stopped first and only then digested: a running app keeps rewriting its
    # files (heartbeats; some rewrite their database at every start), so only a stopped box can be compared exactly.
    # undo takes its digest at the same point (put back, not yet started).
    stopped = []
    for k, a in targets.items():
        if k in existing and _stop_agent(box, a, installed):
            stopped.append(k)
    r.journal["stopped_agents"] = stopped
    r.save()
    for a in replaced:
        _stop_app(box, a["urn"])
    detail = {}
    r.journal["digest_before"] = box_digest(box, current_agents, entries=detail)
    _write_json(r.root / "digest-before.json", detail)
    r.save()
    vols_taken: set[str] = set()
    for a in replaced:
        log(f"app {a['id']}: keeping an undo copy")
        _snapshot_app(box, r, a)
        vols_taken.update(r.journal["steps"][-1].get("volumes") or [])
    for a in data_apps:
        if a["urn"] not in installed:
            _snapshot_volumes(box, r, a, vols_taken)
    for n in _home_entries(box.home):
        _aside(r, "wizard-os", box.home / n, before / "wizard-os" / n)
    r.step(kind="set_aside", area="wizard-os")
    for k, a in targets.items():
        if k not in existing or not a.home.is_dir():
            continue
        for rel in agentlib.movable_entries(a.home):
            _aside(r, f"agent:{k}", a.home / rel, before / "agents" / k / "home" / rel)
        if a.folder and (a.folder / "agent.json").is_file():
            _aside(r, f"agent:{k}", a.folder / "agent.json", before / "agents" / k / "agent.json")
        r.step(kind="set_aside", area=f"agent:{k}")
    if (m.get("files") or {}).get("included"):
        for n in _files_entries(r.files_root):
            _aside(r, "files", r.files_root / n, r.froot / "before" / n)
        r.step(kind="set_aside", area="files")
    aroot = box.agents_root()
    held = agentlib.root_files(aroot)
    if held or (stage / "agents-root").is_dir():
        r.journal["agents_root"] = str(aroot)
        for n in held:
            _aside(r, "agents-root", aroot / n, before / "agents-root" / n)
        r.step(kind="set_aside", area="agents-root")
    r.journal["status"] = "applying"
    r.save()

    # 5) apply ----------------------------------------------------------------
    for k, a in targets.items():
        if k not in existing:
            _prepare_agent_folder(r, k, a)
    late_refused: set[str] = set()
    for a in apps:
        if a["urn"] not in installed:
            log(f"app {a['id']}: installing this box's release")
            _install_app(box, r, a)
            # what Runtipi actually installed (the App Index's release can differ from any store copy seen before)
            v = _version_check(box, a, True, stage)
            if v["verdict"] == "refuse":
                # Runtipi installed an older release than the backup's: take it off again (undo cleans up the
                # rest), put none of the backup's data in it, and go on with the other apps
                log(v["message"])
                p["refused"].append(v)
                late_refused.add(a["urn"])
                r.step(kind="app_refused", urn=a["urn"], message=v["message"])
                try:
                    box.rt.uninstall(a["urn"])
                    box.wait_status(a["urn"], {"missing"}, timeout=900)
                except RuntipiError as e:
                    p["warnings"].append(f"{a['id']}: could not uninstall the older release it got: {e}")
            elif v["verdict"] == "warn":
                p["warnings"].append(v["message"])
    for a in data_apps:
        if a["urn"] in late_refused:
            continue
        arch = stage / "apps" / a["id"] / "data.tar.gz"
        if arch.is_file():
            # user-config/ of an app this box already had is this box's wiring (alongside overrides, trusted
            # origins, ports): it stays. app/ is the installed package (compose file, config.json, wizard.md): this
            # box's release stays, so a restore never puts an older build back over it (wizard-os#91)
            parts = [x for x in APP_ROOTS if x != "app" and not (x == "user-config" and a["urn"] in installed)]
            _stop_app(box, a["urn"])
            log(f"app {a['id']}: putting its data back")
            got = _put_back(box, a["urn"], arch, parts, _path_rewrite(box, m))
            r.step(kind="app_restored", urn=a["urn"], entries=got["entries"], volumes=got["volumes"])
            arch.unlink(missing_ok=True)

    for source, dest in mappings:
        arch = stage / "apps" / source["id"] / "data.tar.gz"
        if not arch.is_file():
            raise RestoreError(f"missing Agent Service state for {source['id']}")
        _stop_app(box, dest["urn"])
        got = _put_back(box, dest["urn"], arch, ("app-data",), data_only=True)
        r.step(kind="app_data_mapped", urn=dest["urn"], source=source["id"], entries=got["entries"])
        arch.unlink(missing_ok=True)

    # Wizard home (identity entries never come from a bundle, and the box keeps its own)
    src = stage / "wizard-os"
    if src.is_dir():
        for n in sorted(os.listdir(src)):
            if n in PER_MACHINE or skip_home_entry(n, (src / n).is_dir()):
                continue
            _move(src / n, box.home / n)
    _merge_back(before / "wizard-os", box.home)
    _keep_refused_secrets(before / "wizard-os", box.home, [x["id"] for x in p.get("refused") or []])
    _schedule_off_after_restore(box)
    r.step(kind="placed", area="wizard-os")

    # agents
    src = stage / "agents-root"
    if src.is_dir():
        aroot = Path(r.journal.get("agents_root") or box.agents_root())
        aroot.mkdir(parents=True, exist_ok=True, mode=0o700)
        for n in agentlib.root_files(src):
            _move(src / n, aroot / n)
        r.step(kind="placed", area="agents-root")
    agent_reports = {}
    for k, a in list(targets.items()):
        if a.urn in late_refused:
            p["agents_skipped"].append(k)
            p["agents"] = [x for x in p["agents"] if x != k]
            # only an app this box did not have is refused here, so this agent is new: nothing of it was set aside
            targets.pop(k)
            continue
        agent_reports[k] = _import_agent(box, r, k, a, existing, installed)

    if (m.get("files") or {}).get("included"):
        fs = r.froot / "stage"
        for n in sorted(os.listdir(fs)) if fs.is_dir() else []:
            _move(fs / n, r.files_root / n)
        r.step(kind="placed", area="files")

    # 7) start and check -----------------------------------------------------
    _one_agent_service(_here_apps(box, box.installed(), [*here_apps, *apps]))
    notes = []
    if box.after_restore is not None:
        try:
            box.after_restore(box)
        except Exception as e:  # noqa: BLE001
            notes.append(f"regenerating apps.json/skills failed: {e}")
    else:
        notes += _regenerate(box)
    health = {dest["id"]: box.app_status(dest["urn"]) for _, dest in mappings}
    for a in apps:
        if a["urn"] in late_refused:
            continue
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
    refused = list(p.get("refused") or [])
    receipt = {"ok": not refused, "partial": bool(refused), "stamp": r.stamp, "file": r.journal.get("file"), "restored_at": int(time.time()),
               "manifest": summary(m), "plan": p, "apps": health, "agents": agent_reports,
               "installed_by_restore": [s["urn"] for s in r.journal["steps"] if s["kind"] == "app_installed"],
               "kept_identity": kept, "active_at_export": m.get("active") or {},
               "schedule": "copied, off", "notes": notes, "undo": "wam backup undo",
               "activate": "wam backup activate --old-is-off",
               "refused": [{"id": x["id"], "name": x["name"], "message": x["message"], "backup": x["backup"],
                            "here": x["here"]} for x in refused],
               "not_restored": [x["message"] for x in refused]}
    if refused:
        receipt["error"] = "partly restored: " + " ".join(x["message"] for x in refused)
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


def _keep_refused_secrets(held: Path, home: Path, refused: list[str]) -> None:
    """A refused app keeps this box's data, so it keeps this box's secrets and app token too (a bundled DB password
    would not open this box's database): secrets/<id>[.*] and app-tokens/<id>[.*] are made what they were here."""
    for app_id in refused:
        for d in MERGE_DIRS:
            def mine(n: str) -> bool:
                return n == app_id or n.startswith(app_id + ".")
            dst = home / d
            for n in sorted(os.listdir(dst)) if dst.is_dir() else []:
                if mine(n) and not (held / d / n).exists():
                    _rm(dst / n)
            src = held / d
            for n in sorted(os.listdir(src)) if src.is_dir() else []:
                if mine(n) and (src / n).is_file() and not (src / n).is_symlink():
                    dst.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _rm(dst / n)
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
    if a.runtime == "container" and box.app_status(a.urn) in LIVE:
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
    try:
        return _undo(box)
    except Exception as e:
        # Undo that stops part way leaves the box mixed: say why in the journal, which status shows.
        try:
            root, j = last_restore(box)
            if j.get("status") != "undone":
                j["undo_error"] = f"{type(e).__name__}: {e}"[:500]
                j["undo_failed_at"] = int(time.time())
                _write_json(root / "journal.json", j)
        except Exception:  # noqa: BLE001 - the original error is the one to raise
            pass
        raise


def _undo(box: Box) -> dict:
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
            elif box.app_status(a.urn) in LIVE:
                # a container agent left up keeps writing its home while it is put back, and its restart policy
                # re-creates a removed home: every live state is stopped, "starting" included
                box.rt.stop(a.urn)
                box.wait_status(a.urn, {"stopped"}, timeout=300)
        except (RuntipiError, HermesError) as e:
            notes.append(f"agent {k}: stop: {e}")
    if j.get("move_journal_version") == 1:
        _undo_journaled_trees(box, root, j, targets, files_root, froot)
    else:
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
        if "agents-root" in areas and j.get("agents_root"):
            aroot = Path(j["agents_root"])
            for n in agentlib.root_files(aroot):
                _move(aroot / n, undone / "agents-root" / n)
            for n in agentlib.root_files(before / "agents-root"):
                _move(before / "agents-root" / n, aroot / n)
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
            # app_status also returns "missing" for API errors: require a successful installed-list read
            # before removing an orphan's compose project or folders.
            try:
                missing = s["urn"] not in box.installed()
            except (RuntipiError, OSError) as e:
                notes.append(f"{s['urn']}: cannot confirm Runtipi record; skipped uninstall/cleanup: {e}")
                continue
            if missing:
                notes.append(f"{s['urn']}: missing from Runtipi; skipped uninstall, cleaning restore leftovers")
                try:
                    aid, st = s["urn"].split(":")
                    box.appdata.remove_missing_app(st, aid)
                except AppDataError as e:
                    notes.append(f"{s['urn']}: leftover cleanup failed: {e}")
            else:
                log(f"uninstalling {s['urn']} (installed by the restore)")
                try:
                    box.rt.uninstall(s["urn"])
                    box.wait_status(s["urn"], {"missing"}, timeout=900)
                except RuntipiError as e:
                    notes.append(f"{s['urn']}: uninstall failed: {e}")
            # Runtipi's uninstall keeps user-config/; the restore brought it, so it goes too
            uc = Path(s["user_config_new"]) if s.get("user_config_new") else None
            if uc and (uc.exists() or uc.is_symlink()):
                try:
                    box.appdata.remove_runtipi_path(uc)       # put there as root (the bundle's owners)
                except AppDataError as e:
                    notes.append(f"{s['urn']}: could not remove {uc}: {e}")
            if s.get("package_placed"):
                try:
                    st, aid = s["urn"].split(":")[1], s["urn"].split(":")[0]
                    box.appdata.remove_package(st, aid)
                except AppDataError as e:
                    notes.append(f"{s['urn']}: could not remove the package the restore placed: {e}")
    # folders the restore made for new agents go only after their apps are uninstalled: a container still up
    # would re-create its bind-mounted home as soon as the folder moved
    for s in steps:
        if s["kind"] == "agent_folder_created" and (Path(s["path"]).exists() or Path(s["path"]).is_symlink()):
            _move(Path(s["path"]), undone / "agent-folders" / s["agent"] / Path(s["path"]).name)
    for s in steps:
        if s["kind"] in ("app_snapshot", "volume_snapshot") and s.get("archive"):
            log(f"putting back {s['urn']}" + (" (its volumes)" if s["kind"] == "volume_snapshot" else ""))
            try:
                if s["kind"] == "app_snapshot":
                    _stop_app(box, s["urn"])
                _put_back(box, s["urn"], Path(s["archive"]),
                          APP_ROOTS if s["kind"] == "app_snapshot" else ("volumes",))
            except (RuntipiError, RestoreError) as e:
                notes.append(f"{s['urn']}: putting back its undo copy failed: {e}")
        elif s["kind"] == "app_snapshot" and s.get("backup"):   # a journal from before appdata.py
            try:
                box.rt.restore_backup(s["urn"], s["backup"])
                box.wait_status(s["urn"], set(), timeout=3 * 3600, not_in={"restoring"})
                box.rt.delete_backup(s["urn"], s["backup"])
            except RuntipiError as e:
                notes.append(f"{s['urn']}: restore of the snapshot failed: {e}")
    # compare now, before anything starts again and rewrites its files (import digested at the same point)
    current = [a for a in box.agents() if a.id in targets or a.runtime == "host"]
    detail = {}
    after = box_digest(box, current, entries=detail, owners=(root / "digest-before.json").is_file())
    _write_json(root / "digest-after.json", detail)
    detail_before = root / "digest-before.json"
    differences = digest_differences(json.loads(detail_before.read_text()), detail) if detail_before.is_file() else None
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
    want = j.get("digest_before") or {}
    mismatch = sorted(k for k in set(want) | set(after) if want.get(k) != after.get(k))
    j["status"] = "undo-failed" if mismatch else "undone"
    if not mismatch:
        j["undone_at"] = int(time.time())
    j["undo_digest_match"] = not mismatch
    j["undo_differences"] = differences
    j["undo_notes"] = notes
    _write_json(root / "journal.json", j)
    # A mismatched undo remains retryable, with every recovery copy retained.
    if not mismatch:
        shutil.rmtree(root / "stage", ignore_errors=True)
        shutil.rmtree(froot / "stage", ignore_errors=True)
        shutil.rmtree(undone, ignore_errors=True)
        shutil.rmtree(before, ignore_errors=True)
        shutil.rmtree(froot, ignore_errors=True)
        for p in (files_root / RESTORE_DIR,):
            try:
                p.rmdir()
            except OSError:
                pass
    return {"ok": not mismatch, "stamp": stamp, "digest_match": not mismatch, "mismatch": mismatch,
            "differences": differences, "notes": notes, "kept": None if not mismatch else str(undone)}


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
    _one_agent_service(_here_apps(box, box.installed(), (j.get("manifest") or {}).get("apps", [])))
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
    for urn in sorted({s["urn"] for s in j.get("steps", []) if s["kind"] == "app_data_mapped"}):
        if box.app_status(urn) != "running":
            box.rt.start(urn)
    if active.get("schedule"):
        from . import schedule
        out["schedule"] = schedule.enable(box)["enabled"]
    j["activated_at"] = int(time.time())
    j["activation"] = out
    _write_json(root / "journal.json", j)
    return out


__all__ = ["verify", "verify_stream", "import_bundle", "check_can_import", "SETTLED", "undo", "activate", "RestoreError", "SpaceError", "human"]
