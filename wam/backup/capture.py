"""`wam backup export`: write this whole Wizard into one encrypted `.wizardbackup` file (docs/BACKUP.md).

The file is encrypted as it is written (straight into `<out>.part`, renamed when complete), so nothing readable ever
lands on disk outside the box's own folders. What goes in, in order:

  manifest.json             what is inside (no secrets): source, agents, apps, files totals, the `active` record
  wizard-os/...             the Wizard home, without per-machine identity, regenerated state and agent homes
  agents/<id>/agent.json    each agent; `hermes-backup.zip` from stock `hermes backup`; extra agents also carry
  agents/<id>/folder/...    their small Wizard files (runtime.key, gateway.key, service-state.json, package/)
  apps/<id>/app.json        the app's apps.json row, Runtipi urn/version/status and install form
  apps/<id>/package/...     the exact Runtipi package installed (runtipi/apps/<store>/<id>)
  apps/<id>/data.tar.gz     the app's data (app-data + installed package + user-config, plus the named Docker volumes
                            its compose file declares under volumes/<name>/), archived as root with the app stopped and
                            started again after (appdata.py: Runtipi's own per-app backup loses data on a real box)
  files/...                 the owner's files (Runtipi media/), one opaque tree
  checksums.json            sha256 and size of every entry

Export never changes the box beyond the brief per-app stop and start (Runtipi regenerates app.env and opens up
the app folder's permissions whenever it starts an app); the app archives and agent zips are written to a work
folder and deleted again once they are in the file.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

from . import EXT, agents as agentlib, bundle, crypto, fmt, keys as keylib
from .appdata import AppDataError
from .box import (MAIN_AGENT, Agent, Box, HermesError, RuntipiError, SpaceError, human, log, need_space, need_spaces,
                  skip_home_entry)

MARGIN = 256 << 20


class ExportError(RuntimeError):
    pass


def box_name(box: Box) -> str:
    for f in ("settings.json", "launcher.json"):
        try:
            d = json.loads((box.home / f).read_text())
            n = d.get("name") or d.get("box_name") or (d.get("box") or {}).get("name")
            if n:
                return _slug(str(n))
        except (OSError, ValueError, AttributeError):
            pass
    return _slug(socket.gethostname().split(".")[0] or "wizard")


def _slug(s: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in s.lower()).strip("-")
    return (out or "wizard")[:40]


def default_output(box: Box, folder: Path | None = None) -> Path:
    return Path(folder or Path.cwd()) / f"{box_name(box)}-{time.strftime('%Y%m%d-%H%M%S')}{EXT}"


def schedule_on(box: Box) -> bool:
    try:
        return bool(json.loads((box.home / "backup.json").read_text()).get("enabled"))
    except (OSError, ValueError, AttributeError):
        return False


def _app_archive(box: Box, urn: str, info: dict, work: Path, volumes: list[str]) -> tuple[Path, dict]:
    """Stop the app through Runtipi, archive its folders and named volumes as root (appdata.AppData), start it again
    if it was running. Returns (archive path, report)."""
    store, app_id = info["store"], info["id"]
    was_running = info.get("status") == "running"
    if was_running:
        box.rt.stop(urn)
        st = box.wait_status(urn, {"stopped"}, timeout=600)
        if st != "stopped":
            raise ExportError(f"{app_id} did not stop for its backup (status {st})")
    out = work / f"app-{app_id}.tar.gz"
    try:
        rep = box.appdata.archive(store, app_id, out, volumes=volumes)
    except AppDataError as e:
        raise ExportError(f"app {app_id}: {e}") from None
    finally:
        if was_running:
            try:
                box.rt.start(urn)
                if box.wait_status(urn, {"running"}, timeout=900) != "running":
                    log(f"note: {app_id} did not report running again after its backup")
            except RuntipiError as e:
                log(f"note: could not start {app_id} again after its backup: {e}")
    return out, rep


def _emit_app_archive(box: Box, bw, app_id: str, urn: str, info: dict, vols: list[str], work: Path,
                      plain: bool, skipped: list[str]) -> None:
    """Archive one app (stopped and started again around it), put it into the bundle, delete it. A plain (restic)
    stream gets it decompressed as apps/<id>/data.tar, the file as apps/<id>/data.tar.gz."""
    log(f"app {app_id}: archiving its data")
    path, rep = _app_archive(box, urn, info, work, vols)
    try:
        if plain:
            bw.add_gunzipped(f"apps/{app_id}/data.tar", path)
        else:
            bw.add_file(f"apps/{app_id}/data.tar.gz", path)
    finally:
        path.unlink(missing_ok=True)
    skipped += [f"{app_id}: {x}" for x in rep["skipped"]]


def _du(path: Path) -> int:
    return bundle.tree_size(path)["bytes"] if path.is_dir() else 0


def export(box: Box, out: Path | None = None, *, keys: fmt.Keys | None = None, passphrase: str | None = None,
           include_files: bool = True, key_file: str | None = None, scrypt_n: int = crypto.SCRYPT_N,
           plain_stream=None) -> dict:
    """With `plain_stream` (a writable stream; the scheduled run's pipe into `restic backup --stdin`) the same
    entries go into it as a plain tar, in a dedupe-friendly form: agent archives rewritten with stored members and
    Runtipi's app archives as `apps/<id>/data.tar` (decompressed); restic encrypts it. No file is written."""
    t0 = time.time()
    if plain_stream is not None:
        keys = keys or fmt.Keys()
    elif keys is None:
        keys = fmt.Keys()
        if key_file:
            keylib.parse_secret(Path(key_file).expanduser().read_text(), keys)
        if box.env.get("WIZARD_BACKUP_KEY"):
            keys.recovery.append(crypto.decode_recovery_key(box.env["WIZARD_BACKUP_KEY"]))
        stored = keylib.stored_key(box.home)
        if stored and stored not in keys.recovery:
            keys.recovery.append(stored)
    if passphrase:
        keys.passphrases.append(passphrase)
    if plain_stream is None and not keys.recovery and not keys.passphrases:
        raise ExportError("no backup key on this box: run `wam backup key new` (or give --passphrase)")
    if plain_stream is None:
        out = Path(out) if out else default_output(box)
        if out.is_dir():
            out = default_output(box, out)
        out = out.expanduser().absolute()
        if out.exists():
            raise ExportError(f"{out} already exists")
        out.parent.mkdir(parents=True, exist_ok=True)
    installed = box.installed()
    rows = box.registry_rows()
    agents = box.agents(installed)
    agent_urns = {a.urn for a in agents if a.urn}
    files_root = box.files_root()
    files_size = bundle.tree_size(files_root, _files_skip) if include_files else {"count": 0, "bytes": 0}
    home_size = bundle.tree_size(box.home, skip_home_entry)
    est = files_size["bytes"] + home_size["bytes"]
    agents_bytes = sum(_du(a.home) for a in agents)
    app_sizes = [_du(box.runtipi_root / "app-data" / info["store"] / info["id"])
                 + _du(box.runtipi_root / "user-config" / info["store"] / info["id"])
                 for urn, info in installed.items() if urn not in agent_urns]
    est += agents_bytes + sum(app_sizes)
    # the work folder holds every agent's zip (twice for a stream: snapshot + stored rewrite) and ONE app archive at
    # a time; the output and the work folder may share a disk, so the needs are added per filesystem
    work_need = (2 if plain_stream is not None else 1) * agents_bytes + max(app_sizes, default=0) + MARGIN
    if plain_stream is None:
        need_spaces(box, [(out.parent, est + MARGIN, "the backup file"), (box.work_dir(), work_need, "the work folder")])
    else:
        need_space(box, box.work_dir(), work_need, "the work folder")
    log(f"about {human(est)} to back up")
    work = Path(tempfile.mkdtemp(prefix="export-", dir=box.work_dir()))

    temps: list[Path] = []
    part = out.with_name(out.name + ".part") if plain_stream is None else None
    try:
        # 1) agents: stock `hermes backup` into the work folder (the manifest needs their active record)
        agent_rows, agent_zips = [], {}
        for a in agents:
            z = work / f"agent-{a.id}.zip"
            log(f"agent {a.id}: hermes backup")
            try:
                box.runner.backup(a, z)
            except HermesError as e:
                raise ExportError(f"agent {a.id}: {e}") from None
            temps.append(z)
            if plain_stream is not None:
                stored = work / f"agent-{a.id}-stored.zip"
                bundle.zip_stored(z, stored)
                temps.append(stored)
                z.unlink()
                z = stored
            agent_zips[a.id] = z
            agent_rows.append({**a.describe(), "bytes": z.stat().st_size, "active": agentlib.active_from_zip(z)})

        # 2) apps: each app's folders and volumes (agent apps carry no data here: their home is the agent above)
        app_rows, app_files, app_skipped, volumes_taken = [], {}, [], set()
        for urn in sorted(installed):
            info = installed[urn]
            app_id, store = info["id"], info["store"]
            row = rows.get(app_id) if (rows.get(app_id) or {}).get("urn", urn) == urn else None
            pkg = box.app_package(store, app_id)
            is_agent = urn in agent_urns or box.is_agent_app(app_id, row)
            rec = {"id": app_id, "urn": urn, "store": store, "name": (row or {}).get("name") or app_id,
                   "type": (row or {}).get("type"), "version": (row or {}).get("version"),
                   "commit": (row or {}).get("commit"), "repo": (row or {}).get("repo"),
                   "tipi_version": info.get("version"), "status": info.get("status"),
                   "provides": (row or {}).get("provides") or [], "package": pkg.is_dir(),
                   "data": "agent" if is_agent else "archive"}
            try:
                full = box.rt.app(urn) or {}
            except RuntipiError:
                full = {}
            form = ((full.get("app") or {}).get("config")) or {}
            if not is_agent:
                # each app is archived just in time, while the file reaches it, and deleted right after: at most one
                # app archive is on disk at a time. Its named volumes are known now (the manifest comes first).
                vols = [v for v in box.appdata.volumes(store, app_id) if v not in volumes_taken]
                volumes_taken.update(vols)
                rec["bytes"] = None
                rec["volumes"] = vols
                app_files[app_id] = (urn, info, vols)
            app_rows.append(rec)
            rec["_form"] = form
            rec["_row"] = row

        manifest = {
            "format": 1, "created_at": int(t0), "name": box_name(box), "source": box.source(),
            "agents": [{k: v for k, v in r.items() if k != "active"} for r in agent_rows],
            "apps": [{k: v for k, v in r.items() if not k.startswith("_")} for r in app_rows],
            "files": {"included": include_files, "count": files_size["count"], "bytes": files_size["bytes"]},
            "wizard_os": home_size,
            "active": {"agents": {r["id"]: r["active"] for r in agent_rows}, "schedule": schedule_on(box)},
            "estimate_bytes": est,
        }

        # 3) the file: encrypted straight into <out>.part
        if plain_stream is None:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with (os.fdopen(fd, "wb") if plain_stream is None else contextlib.nullcontext(plain_stream)) as raw:
            w = fmt.Writer(raw, keys, scrypt_n=scrypt_n) if plain_stream is None else None
            bw = bundle.BundleWriter(w if w is not None else raw)
            bw.add_json(bundle.MANIFEST, manifest)
            log("wizard home")
            bw.add_dir("wizard-os")
            bw.add_tree("wizard-os", box.home, skip_home_entry)
            for a in agents:
                bw.add_dir(f"agents/{a.id}")
                if a.folder and (a.folder / "agent.json").is_file():
                    bw.add_file(f"agents/{a.id}/agent.json", a.folder / "agent.json")
                else:
                    bw.add_json(f"agents/{a.id}/agent.json", {"schema": 1, "id": a.id, "main": a.main,
                                                               "runtime": {"kind": a.runtime, "app_id": (a.urn or ":").split(":")[0] or None}})
                bw.add_file(f"agents/{a.id}/hermes-backup.zip", agent_zips[a.id])
                if a.folder:
                    bw.add_dir(f"agents/{a.id}/folder")
                    bw.add_tree(f"agents/{a.id}/folder", a.folder,
                                lambda rel, d: rel.split("/")[0] in ("home", "upload-staging", "agent.json"))
            for rec in app_rows:
                app_id = rec["id"]
                bw.add_dir(f"apps/{app_id}")
                bw.add_json(f"apps/{app_id}/app.json", {"urn": rec["urn"], "store": rec["store"],
                                                         "row": rec["_row"], "form": rec["_form"],
                                                         "tipi_version": rec["tipi_version"], "status": rec["status"],
                                                         "data": rec["data"]})
                pkg = box.app_package(rec["store"], app_id)
                if pkg.is_dir():
                    bw.add_dir(f"apps/{app_id}/package")
                    bw.add_tree(f"apps/{app_id}/package", pkg)
                if app_id in app_files:
                    _emit_app_archive(box, bw, app_id, *app_files[app_id], work, plain_stream is not None,
                                      app_skipped)
            if include_files and files_root.is_dir():
                log(f"files: {files_size['count']} files, {human(files_size['bytes'])}")
                bw.add_dir("files")
                bw.add_tree("files", files_root, _files_skip, progress=log)
            sums = bw.finish()
            if w is not None:
                w.close()
                raw.flush()
                os.fsync(raw.fileno())
            else:
                raw.flush()
        if plain_stream is None:
            os.replace(part, out)
    except BaseException:
        if part is not None:
            part.unlink(missing_ok=True)
        raise
    finally:
        for p in temps:
            p.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        try:   # the work folder's parent, when this run made it and left nothing else in it
            box.work_dir().rmdir()
        except OSError:
            pass
    size = out.stat().st_size if plain_stream is None else sums["bytes"]
    res = {"ok": True, "file": str(out) if plain_stream is None else None, "bytes": size, "size": human(size),
           "seconds": round(time.time() - t0, 1),
           "key_ids": w.key_ids if w is not None else [], "slots": w.slot_kinds if w is not None else [],
           "entries": sums["count"], "content_bytes": sums["bytes"],
           "agents": [a["id"] for a in manifest["agents"]], "apps": [a["id"] for a in manifest["apps"]],
           "files": manifest["files"], "changed_while_reading": bw.changed[:20], "skipped_links": bw.skipped[:20],
           "skipped_in_apps": app_skipped[:50], "skipped_in_apps_count": len(app_skipped)}
    if bw.changed:
        log(f"note: {len(bw.changed)} files changed while being read (their new size was cut/padded)")
    if bw.skipped:
        log(f"note: {len(bw.skipped)} links were left out (a backup holds no links outside app data): "
            + ", ".join(bw.skipped[:5]))
    if app_skipped:
        log(f"note: {len(app_skipped)} entries in app data were left out (sockets, devices, links leaving their "
            "folder): " + ", ".join(app_skipped[:5]))
    return res


def _files_skip(rel: str, is_dir: bool) -> bool:
    return rel.split("/", 1)[0] == ".wizard-restore"


__all__ = ["export", "ExportError", "SpaceError", "default_output", "Agent", "MAIN_AGENT"]
