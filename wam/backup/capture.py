"""`wam backup export`: write this whole Wizard into one encrypted `.wizardbackup` file (docs/BACKUP.md).

The file is encrypted as it is written (straight into `<out>.part`, renamed when complete), so nothing readable ever
lands on disk outside the box's own folders. What goes in, in order:

  manifest.json             what is inside (no secrets): source, agents, apps, files totals, the `active` record
  wizard-os/...             the Wizard home, without per-machine identity, regenerated state and agent homes
  agents/<id>/agent.json    each agent; `hermes-backup.zip` from stock `hermes backup`; extra agents also carry
  agents/<id>/folder/...    their small Wizard files (runtime.key, gateway.key, service-state.json, package/)
  apps/<id>/app.json        the app's apps.json row, Runtipi urn/version/status and install form
  apps/<id>/package/...     the exact Runtipi package installed (runtipi/apps/<store>/<id>)
  apps/<id>/data.tar.gz     Runtipi's own per-app backup (app-data + installed package + user-config), made by Runtipi
                            as root with the app stopped and restarted after
  files/...                 the owner's files (Runtipi media/), one opaque tree
  checksums.json            sha256 and size of every entry

Export never changes the box beyond the brief per-app stop Runtipi's backup needs; the Runtipi archives it asked for
and the agent zips are deleted again once they are in the file.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import time
from pathlib import Path

from . import EXT, agents as agentlib, bundle, crypto, fmt, keys as keylib
from .box import (MAIN_AGENT, Agent, Box, HermesError, RuntipiError, SpaceError, human, log, need_space,
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


def _runtipi_archive(box: Box, urn: str, work: Path, status_before: str | None) -> tuple[Path, str, bool]:
    """Ask Runtipi for a per-app backup and return (path, file name, is_temp_copy). Runtipi stops the app, archives
    app-data + app + user-config as root, and starts it again if it was running."""
    store, app_id = urn.split(":")[1], urn.split(":")[0]
    before = {b.get("id") for b in box.rt.list_backups(urn)}
    box.rt.backup_app(urn)
    st = box.wait_status(urn, set(), timeout=3 * 3600, not_in={"backing_up"})
    if st in ("missing", "unknown"):
        raise ExportError(f"Runtipi lost track of {urn} while backing it up")
    new = [b for b in box.rt.list_backups(urn) if b.get("id") not in before]
    if not new:
        raise ExportError(f"Runtipi's backup of {urn} failed (status {st}); see Runtipi's app log")
    name = sorted(new, key=lambda b: b.get("date") or 0)[-1]["id"]
    if status_before == "running" and st != "running":
        box.wait_status(urn, {"running"}, timeout=600)
    direct = box.runtipi_root / "backups" / store / app_id / name
    if direct.is_file() and os.access(direct, os.R_OK):
        return direct, name, False
    tmp = work / f"{app_id}-{name}"
    box.rt.download_backup(urn, name, tmp)
    return tmp, name, True


def _du(path: Path) -> int:
    return bundle.tree_size(path)["bytes"] if path.is_dir() else 0


def export(box: Box, out: Path | None = None, *, keys: fmt.Keys | None = None, passphrase: str | None = None,
           include_files: bool = True, key_file: str | None = None, scrypt_n: int = crypto.SCRYPT_N) -> dict:
    t0 = time.time()
    if keys is None:
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
    if not keys.recovery and not keys.passphrases:
        raise ExportError("no backup key on this box: run `wam backup key new` (or give --passphrase)")
    out = Path(out) if out else default_output(box)
    if out.is_dir():
        out = default_output(box, out)
    out = out.expanduser().absolute()
    if out.exists():
        raise ExportError(f"{out} already exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(box.work_dir()) / f"export-{os.getpid()}-{int(t0)}"
    work.mkdir(parents=True, mode=0o700)

    installed = box.installed()
    rows = box.registry_rows()
    agents = box.agents(installed)
    agent_urns = {a.urn for a in agents if a.urn}
    files_root = box.files_root()
    files_size = bundle.tree_size(files_root, _files_skip) if include_files else {"count": 0, "bytes": 0}
    home_size = bundle.tree_size(box.home, skip_home_entry)
    est = files_size["bytes"] + home_size["bytes"]
    for a in agents:
        est += _du(a.home)
    for urn, info in installed.items():
        if urn not in agent_urns:
            est += _du(box.runtipi_root / "app-data" / info["store"] / info["id"]) + \
                _du(box.runtipi_root / "user-config" / info["store"] / info["id"])
    need_space(box, out.parent, est + MARGIN, "the backup file")
    log(f"about {human(est)} to back up")

    temps: list[Path] = []
    runtipi_made: list[tuple[str, str]] = []
    part = out.with_name(out.name + ".part")
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
            agent_zips[a.id] = z
            agent_rows.append({**a.describe(), "bytes": z.stat().st_size, "active": agentlib.active_from_zip(z)})

        # 2) apps: Runtipi's per-app backup (agent apps carry no data here: their home is the agent above)
        app_rows, app_files = [], {}
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
                   "data": "agent" if is_agent else "runtipi-backup"}
            try:
                full = box.rt.app(urn) or {}
            except RuntipiError:
                full = {}
            form = ((full.get("app") or {}).get("config")) or {}
            if not is_agent:
                log(f"app {app_id}: Runtipi backup")
                path, name, tmp = _runtipi_archive(box, urn, work, info.get("status"))
                runtipi_made.append((urn, name))
                if tmp:
                    temps.append(path)
                rec["bytes"] = path.stat().st_size
                app_files[app_id] = path
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
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw:
            w = fmt.Writer(raw, keys, scrypt_n=scrypt_n)
            bw = bundle.BundleWriter(w)
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
                    bw.add_file(f"apps/{app_id}/data.tar.gz", app_files[app_id])
            if include_files and files_root.is_dir():
                log(f"files: {files_size['count']} files, {human(files_size['bytes'])}")
                bw.add_dir("files")
                bw.add_tree("files", files_root, _files_skip, progress=log)
            sums = bw.finish()
            w.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        for p in temps:
            p.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        for urn, name in runtipi_made:
            try:
                box.rt.delete_backup(urn, name)
            except RuntipiError as e:
                log(f"note: could not delete Runtipi's temporary backup of {urn}: {e}")
    size = out.stat().st_size
    res = {"ok": True, "file": str(out), "bytes": size, "size": human(size), "seconds": round(time.time() - t0, 1),
           "key_ids": w.key_ids, "slots": w.slot_kinds, "entries": sums["count"], "content_bytes": sums["bytes"],
           "agents": [a["id"] for a in manifest["agents"]], "apps": [a["id"] for a in manifest["apps"]],
           "files": manifest["files"], "changed_while_reading": bw.changed[:20], "skipped_links": bw.skipped[:20]}
    if bw.changed:
        log(f"note: {len(bw.changed)} files changed while being read (their new size was cut/padded)")
    return res


def _files_skip(rel: str, is_dir: bool) -> bool:
    return rel.split("/", 1)[0] == ".wizard-restore"


__all__ = ["export", "ExportError", "SpaceError", "default_output", "Agent", "MAIN_AGENT"]
