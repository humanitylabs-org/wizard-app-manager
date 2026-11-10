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
import shutil
import signal
import threading
import time
from pathlib import Path

from . import bundle, capture, restore
from .box import Box, _existing_parent, human, log
from .fmt import Damaged
from .restic import Repo, ResticError

FILENAME = "wizard.tar"
PATH = "/" + FILENAME
TAGS = ["wizard-backup", "format-1"]
FREE_POLL_SECONDS = 15


class LowDiskStop(KeyboardInterrupt):
    """Unwind capture's existing interruption cleanup, not its recoverable-error handlers."""

    def __init__(self, path: Path, free: int, floor: int) -> None:
        self.path, self.free, self.floor = path, free, floor
        super().__init__(f"Backup stopped to protect your Wizard: only {human(free)} free, needs {human(floor)}. "
                         "Free some space; nothing was changed.")


def validate_floor(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("min_free_bytes must be a positive integer number of bytes")
    return value


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
    Runtipi's backups folder (per-app archives), above what was there before. Also interrupts capture on low space.

    The CLI runs capture on the main thread. A scoped real-time alarm interrupts even a blocked archive subprocess
    or pipe write; checking only between stream writes would miss the largest on-disk staging phase. The first low
    reading disarms the alarm so capture's finally blocks can restart an app and remove only its own export folder.
    The sampling thread never raises into or signals another thread. No files are deleted by this monitor.
    """

    def __init__(self, paths: list[Path], every: float = 0.25, *, disk_free=None,
                 min_free_bytes: int | None = None) -> None:
        self.paths = [p for p in paths if p]
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.min_free_bytes = validate_floor(min_free_bytes)
        self._armed = False
        self.base = {p: _tree_bytes(p) for p in self.paths}
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self.every = every

    def check_free(self) -> None:
        for path in self.paths:
            p = _existing_parent(path)  # Runtipi's backups folder need not exist yet; never create it to probe.
            floor = self.min_free_bytes
            if floor is None:
                floor = max(2 << 30, (shutil.disk_usage(p).total + 19) // 20)
            free = self.disk_free(p)
            if free < floor:
                if self._armed:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    self._armed = False
                raise LowDiskStop(p, free, floor)

    def _alarm(self, signum, frame) -> None:
        try:
            self.check_free()
        except BaseException:
            signal.setitimer(signal.ITIMER_REAL, 0)
            raise

    def sample(self) -> None:
        now = sum(max(_tree_bytes(p) - self.base[p], 0) for p in self.paths)
        self.peak = max(self.peak, now)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.every)

    def __enter__(self) -> "PeakDisk":
        self.check_free()
        # Do not silently run an unguarded capture from an embedding worker or steal an existing alarm.
        if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
            raise RuntimeError("backup capture needs the main thread with no active real-time alarm")
        self._t.start()
        self._handler = signal.signal(signal.SIGALRM, self._alarm)
        self._armed = True
        signal.setitimer(signal.ITIMER_REAL, FREE_POLL_SECONDS, FREE_POLL_SECONDS)
        return self

    def __exit__(self, *exc) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        self._armed = False
        signal.signal(signal.SIGALRM, self._handler)
        self._stop.set()
        self._t.join(10)


# ---------------------------------------------------------------- backup

def backup(box: Box, repo: Repo, host: str, *, min_free_bytes: int | None = None) -> dict:
    t0 = time.time()
    got: dict = {}

    def write(stream):
        got.update(capture.export(box, plain_stream=stream))
        peak.check_free()  # Final admission before restic sees EOF and is allowed to publish a snapshot.
    with PeakDisk([box.work_dir(), box.runtipi_root / "backups"], disk_free=box.disk_free,
                  min_free_bytes=min_free_bytes) as peak:
        summary = repo.backup_stream(write, filename=FILENAME, host=host, tags=TAGS)
        peak.sample()
    cache = repo.cache_dir()
    return {"snapshot": summary.get("snapshot_id"), "short_id": (summary.get("snapshot_id") or "")[:8],
            "data_added": summary.get("data_added"), "data_added_packed": summary.get("data_added_packed"),
            "capture_bytes": summary.get("total_bytes_processed"), "entries": got.get("entries"),
            "agents": got.get("agents"), "apps": got.get("apps"), "files": got.get("files"),
            "changed_while_reading": got.get("changed_while_reading"),
            "apps_without_data": got.get("apps_without_data") or [],
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


def import_from_repo(box: Box, repo: Repo, snapshot: str | None = "latest", *, yes: bool = False,
                     confirm=None) -> dict:
    snap = resolve(repo, snapshot)
    res = restore.import_bundle(box, Path(f"restic-snapshot-{snap['short_id']}"), None, yes=yes, confirm=confirm,
                                stager=lambda: stage_from_repo(box, repo, snap))
    if isinstance(res, dict):
        res["snapshot"] = {"id": snap["id"], "time": snap.get("time"), "host": snap.get("host")}
    return res


__all__ = ["backup", "list_snapshots", "resolve", "import_from_repo", "stage_from_repo", "host_name", "FILENAME"]
