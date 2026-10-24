"""Captures in the restic repository (docs/BACKUP.md, "Scheduled backups").

One snapshot = one file, `/wizard.tar`: the same tar stream a `.wizardbackup` file holds (manifest.json first,
checksums.json last, same member rules), unencrypted on the box's side because restic encrypts it. It is written
straight into `restic backup --stdin`, so no copy of the capture is staged on disk; only the agents' database
snapshots (`hermes backup`) and Runtipi's per-app archives exist on disk while it runs. Dedupe-friendly form: agent
archives with stored (uncompressed) members and app archives decompressed (`apps/<id>/data.tar`).

Restore reads the snapshot back with `restic dump` straight into the same verify-and-stage pass as a file import
(restore.stage_stream: member rules, every checksum, free space), turns `data.tar` back into Runtipi's
`data.tar.gz`, and then follows exactly the file import's plan / set aside / apply / paused path.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import bundle, capture, restore
from .box import Box, human, log, managed_operation
from .fmt import Damaged
from .restic import Repo, ResticError

FILENAME = "wizard.tar"
PATH = "/" + FILENAME
TAGS = ["wizard-backup", "format-1"]


def host_name(box_id: str) -> str:
    return f"wizard-{box_id}"


# ---------------------------------------------------------------- extra disk a run needs

def _tree_bytes(root: Path) -> int:
    n = 0
    for dp, _, fns in os.walk(root, onerror=lambda e: None):
        for fn in fns:
            try:
                n += os.lstat(os.path.join(dp, fn)).st_size
            except OSError:
                pass
    return n


class PeakDisk:
    """Samples what a run adds on the box's disks while it runs: the engine's work folder (agent archives) and
    Runtipi's backups folder (per-app archives), above what was there before."""

    def __init__(self, paths: list[Path], every: float = 0.25) -> None:
        self.paths = [p for p in paths if p]
        self.base = {p: _tree_bytes(p) for p in self.paths}
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self.every = every

    def sample(self) -> None:
        now = sum(max(_tree_bytes(p) - self.base[p], 0) for p in self.paths)
        self.peak = max(self.peak, now)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.every)

    def __enter__(self) -> "PeakDisk":
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._t.join(10)


# ---------------------------------------------------------------- backup

@managed_operation
def backup(box: Box, repo: Repo, host: str) -> dict:
    t0 = time.time()
    got: dict = {}

    def write(stream):
        got.update(capture.export(box, plain_stream=stream))
    with PeakDisk([box.work_dir(), box.runtipi_root / "backups"]) as peak:
        summary = repo.backup_stream(write, filename=FILENAME, host=host, tags=TAGS)
        peak.sample()
    cache = repo.cache_dir()
    return {"snapshot": summary.get("snapshot_id"), "short_id": (summary.get("snapshot_id") or "")[:8],
            "data_added": summary.get("data_added"), "data_added_packed": summary.get("data_added_packed"),
            "capture_bytes": summary.get("total_bytes_processed"), "entries": got.get("entries"),
            "agents": got.get("agents"), "apps": got.get("apps"), "files": got.get("files"),
            "changed_while_reading": got.get("changed_while_reading"),
            "peak_extra_disk_bytes": peak.peak, "restic_cache_bytes": _tree_bytes(cache) if cache.is_dir() else 0,
            "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- snapshots

def list_snapshots(repo: Repo, host: str | None = None) -> list[dict]:
    out = []
    for s in repo.snapshots(host):
        if PATH not in (s.get("paths") or []):
            continue
        sm = s.get("summary") or {}
        out.append({"id": s.get("id"), "short_id": s.get("short_id") or (s.get("id") or "")[:8],
                    "time": s.get("time"), "host": s.get("hostname"), "tags": s.get("tags") or [],
                    "capture_bytes": sm.get("total_bytes_processed"), "data_added": sm.get("data_added")})
    out.sort(key=lambda s: s.get("time") or "")
    return out


def resolve(repo: Repo, snapshot: str | None) -> dict:
    snaps = list_snapshots(repo)
    if not snaps:
        raise restore.RestoreError("the backup repository holds no Wizard snapshot")
    if not snapshot or snapshot == "latest":
        return snaps[-1]
    hits = [s for s in snaps if (s["id"] or "").startswith(snapshot)]
    if len(hits) != 1:
        raise restore.RestoreError(f"no single snapshot matches {snapshot!r} (`wam backup snapshots` lists them)")
    return hits[0]


# ---------------------------------------------------------------- restore

def _gzip_app_archives(r) -> None:
    apps = r.root / "stage" / "apps"
    for d in sorted(apps.iterdir()) if apps.is_dir() else []:
        plain = d / "data.tar"
        if plain.is_file():
            bundle.gzip_file(plain, d / "data.tar.gz")
            plain.unlink()


def stage_from_repo(box: Box, repo: Repo, snap: dict):
    """restic dump → the file import's verify-and-stage pass. Returns (restore, scan result) like restore.stage."""
    log(f"reading snapshot {snap['short_id']} ({human(snap.get('capture_bytes') or 0)}) from the repository")
    staged = []
    try:
        with repo.dump(snap["id"], PATH) as stream:
            staged.append(restore.stage_stream(box, stream))
    except ResticError:
        if staged:
            restore.discard(staged[0][0])
        raise
    except (Damaged, OSError) as e:
        if repo.last_dump_error is not None:
            raise ResticError(f"the snapshot could not be read back intact: {repo.last_dump_error}") from e
        raise
    r, res = staged[0]
    try:
        _gzip_app_archives(r)
    except BaseException:
        restore.discard(r)
        raise
    r.journal["source"] = {"kind": "restic", "repository": repo.url, "snapshot": snap["id"]}
    return r, res


@managed_operation
def import_from_repo(box: Box, repo: Repo, snapshot: str | None = "latest", *, yes: bool = False,
                     confirm=None) -> dict:
    snap = resolve(repo, snapshot)
    res = restore.import_bundle(box, Path(f"restic-snapshot-{snap['short_id']}"), None, yes=yes, confirm=confirm,
                                stager=lambda: stage_from_repo(box, repo, snap))
    if isinstance(res, dict):
        res["snapshot"] = {"id": snap["id"], "time": snap.get("time"), "host": snap.get("host")}
    return res


__all__ = ["backup", "list_snapshots", "resolve", "import_from_repo", "stage_from_repo", "host_name", "FILENAME"]
