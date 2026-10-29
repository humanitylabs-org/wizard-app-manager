"""The tar stream inside a backup: what may be in it, checksums.json, and the one streaming pass that verifies it.

Members are regular files or directories with relative, `..`-free names under `wizard-os/`, `agents/`, `apps/` or
`files/`, plus `manifest.json` (first) and `checksums.json` (last). Links, devices, FIFOs and sparse files are refused
when they are read, before anything is written for them, so no member can point a later one outside the staging
folder. `scan()` reads the whole stream once: it checks every member, hashes every file, compares with
checksums.json (no missing, extra or changed entry) and reads to the container's authenticated end. With a `sink`
it also writes each member into staging; without one it changes nothing (`wam backup verify`).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import time
import zipfile
import zlib
from .filesystem import Path
from . import filesystem as datafs

from .fmt import Damaged

ROOTS = ("wizard-os/", "agents/", "agents-root/", "apps/", "files/")
MANIFEST, CHECKSUMS = "manifest.json", "checksums.json"
COPY = 1 << 20
MAX_JSON = 64 << 20


class Unsafe(Damaged):
    pass


# ---------------------------------------------------------------- writing

class _SizedReader:
    """Exactly `size` bytes of `f`, hashed as they pass. A file that shrank while being read is padded with zeros and
    one that grew is cut, so the tar stays well formed; `changed` says so and the checksum is of what went in."""

    def __init__(self, f, size: int) -> None:
        self.f, self.left, self.h, self.changed = f, size, hashlib.sha256(), False

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        b = self.f.read(n)
        if len(b) < n:
            self.changed = True
            b += b"\x00" * (n - len(b))
        self.left -= len(b)
        self.h.update(b)
        return b


class BundleWriter:
    def __init__(self, stream) -> None:
        self.tar = tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT)
        self.sums: dict[str, dict] = {}
        self.bytes = 0
        self.changed: list[str] = []
        self.skipped: list[str] = []

    def _info(self, name: str, size: int, mode: int, mtime: float, kind=tarfile.REGTYPE) -> tarfile.TarInfo:
        check_name(name)
        if name in self.sums:
            raise ValueError(f"duplicate bundle entry {name}")
        ti = tarfile.TarInfo(name)
        ti.size, ti.mode, ti.mtime, ti.type = size, mode & 0o777, int(mtime), kind
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    def add_bytes(self, name: str, data: bytes, mode: int = 0o600) -> None:
        self.tar.addfile(self._info(name, len(data), mode, time.time()), io.BytesIO(data))
        self.sums[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        self.bytes += len(data)

    def add_json(self, name: str, obj) -> None:
        self.add_bytes(name, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())

    def add_dir(self, name: str, mode: int = 0o700, mtime: float | None = None) -> None:
        name = name.rstrip("/")
        self.tar.addfile(self._info(name, 0, mode, mtime or time.time(), tarfile.DIRTYPE))
        self.sums[name] = {"type": "dir"}

    def add_file(self, name: str, path: Path, st: os.stat_result | None = None) -> int:
        st = st or datafs.lstat(path)
        with datafs.open(path, "rb") as f:
            r = _SizedReader(f, st.st_size)
            self.tar.addfile(self._info(name, st.st_size, stat.S_IMODE(st.st_mode), st.st_mtime), r)
        if r.changed:
            self.changed.append(name)
        self.sums[name] = {"sha256": r.h.hexdigest(), "size": st.st_size}
        self.bytes += st.st_size
        return st.st_size

    def add_gunzipped(self, name: str, path: Path) -> int:
        """A .tar.gz stored decompressed (a plain tar), so a repository that deduplicates by content (restic) only
        keeps what changed. Two passes over the archive (the tar header needs the size first); no copy on disk."""
        size = 0
        try:
            with gzip.open(path, "rb") as g:
                while b := g.read(COPY):
                    size += len(b)
            st = os.stat(path)
            with gzip.open(path, "rb") as g:
                r = _SizedReader(g, size)
                self.tar.addfile(self._info(name, size, 0o600, st.st_mtime), r)
        except (OSError, EOFError, zlib.error) as e:
            raise ValueError(f"cannot read {path.name} ({str(e)[:100]})") from None
        if r.changed:
            raise ValueError(f"{path.name} changed while it was being read")
        self.sums[name] = {"sha256": r.h.hexdigest(), "size": size}
        self.bytes += size
        return size

    def add_tree(self, prefix: str, root: Path, skip=None, progress=None) -> dict:
        """Every regular file and directory under `root` as `prefix/<rel>`. Links and special files are skipped and
        listed (they never travel). `skip(rel, is_dir)` excludes entries."""
        count = size = 0
        root = Path(root)
        if not root.is_dir():
            return {"count": 0, "bytes": 0}
        for dirpath, dirnames, filenames in datafs.walk(root):
            dp = Path(dirpath)
            rel_dir = dp.relative_to(root).as_posix()
            keep = []
            for d in sorted(dirnames):
                rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                p = dp / d
                if (skip and skip(rel, True)):
                    continue
                if p.is_symlink():
                    self.skipped.append(f"{prefix}/{rel}")
                    continue
                keep.append(d)
                st = datafs.lstat(p)
                self.add_dir(f"{prefix}/{rel}", stat.S_IMODE(st.st_mode), st.st_mtime)
            dirnames[:] = keep
            for fn in sorted(filenames):
                rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                if skip and skip(rel, False):
                    continue
                p = dp / fn
                try:
                    st = datafs.lstat(p)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    self.skipped.append(f"{prefix}/{rel}")
                    continue
                try:
                    size += self.add_file(f"{prefix}/{rel}", p, st)
                except PermissionError:
                    raise PermissionError(f"cannot read {p}: run the backup as the owner of these files") from None
                count += 1
                if progress and count % 500 == 0:
                    progress(f"{prefix}: {count} files")
        return {"count": count, "bytes": size}

    def finish(self) -> dict:
        files = [v for v in self.sums.values() if "sha256" in v]
        body = {"entries": self.sums, "count": len(self.sums), "files": len(files), "bytes": sum(v["size"] for v in files)}
        data = (json.dumps(body, sort_keys=True) + "\n").encode()
        ti = self._info(CHECKSUMS, len(data), 0o600, time.time())
        self.tar.addfile(ti, io.BytesIO(data))
        self.tar.close()
        return body


def zip_stored(src: Path, dst: Path) -> int:
    """Rewrite a zip with every member stored (uncompressed), same names, dates and modes: a stock `hermes backup`
    archive becomes dedupe-friendly for restic and still imports with stock `hermes import`. Returns dst's size."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED, allowZip64=True) as zout:
        for info in zin.infolist():
            ni = zipfile.ZipInfo(info.filename, info.date_time)
            ni.external_attr, ni.create_system, ni.compress_type = info.external_attr, info.create_system, \
                zipfile.ZIP_STORED
            if info.is_dir():
                zout.writestr(ni, b"")
                continue
            with zin.open(info) as s, zout.open(ni, "w", force_zip64=info.file_size >= (1 << 31)) as d:
                shutil.copyfileobj(s, d, COPY)
    os.chmod(dst, 0o600)
    return dst.stat().st_size


def gzip_file(src: Path, dst: Path) -> None:
    """Plain tar → .tar.gz (an app archive read back from a restic repository, for Runtipi's restore)."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(src, "rb") as s, os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6,
                                                                          mtime=0) as g:
        shutil.copyfileobj(s, g, COPY)


def tree_size(root: Path, skip=None) -> dict:
    """Count and bytes of what add_tree would take, without reading file contents."""
    count = size = 0
    root = Path(root)
    if not root.is_dir():
        return {"count": 0, "bytes": 0}
    for dirpath, dirnames, filenames in datafs.walk(root):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if not (dp / d).is_symlink()
                       and not (skip and skip(d if rel_dir == "." else f"{rel_dir}/{d}", True))]
        for fn in filenames:
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            if skip and skip(rel, False):
                continue
            try:
                st = datafs.lstat(dp / fn)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                count += 1
                size += st.st_size
    return {"count": count, "bytes": size}


# ---------------------------------------------------------------- member rules

def check_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise Unsafe(f"unsafe name in the backup: {str(name)[:120]!r}")
    parts = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise Unsafe(f"unsafe name in the backup: {name[:120]!r}")
    clean = "/".join(parts)
    if clean not in (MANIFEST, CHECKSUMS) and not any(clean.startswith(r) or clean == r[:-1] for r in ROOTS):
        raise Unsafe(f"unexpected entry in the backup: {clean[:120]!r}")
    return clean


def check_member(m: tarfile.TarInfo) -> str:
    name = check_name(m.name)
    if m.issym() or m.islnk():
        raise Unsafe(f"the backup holds a link ({name[:120]}); links are refused")
    if m.ischr() or m.isblk() or m.isfifo():
        raise Unsafe(f"the backup holds a device or FIFO ({name[:120]}); refused")
    if m.issparse() or not (m.isreg() or m.isdir()):
        raise Unsafe(f"the backup holds an unsupported entry type ({name[:120]}); refused")
    if m.isdir() and name in (MANIFEST, CHECKSUMS):
        raise Unsafe(f"{name} must be a file")
    if not m.isdir() and name + "/" in ROOTS:
        raise Unsafe(f"{name} must be a folder")
    return name


def check_inner_archive(fileobj, mode: str = "r|gz") -> dict:
    """An app data archive (appdata.py; also an older bundle's Runtipi per-app backup): safe relative names under
    app-data/, app/, user-config/ and volumes/<name>/; files, folders, hard links to earlier files and symlinks that
    stay inside the root they live in, nothing below a symlink. Anything else is refused. Reads it to the end."""
    from .appdata import MemberGuard, UnsafeMember
    g = MemberGuard()
    try:
        with tarfile.open(fileobj=fileobj, mode=mode) as t:
            for m in t:
                g.check(m)
        g.finish()
    except UnsafeMember as e:
        raise Unsafe(str(e)) from None
    except (tarfile.TarError, EOFError, OSError, ValueError) as e:
        if isinstance(e, Damaged):
            raise
        raise Damaged(f"an app data archive is not readable ({str(e)[:120]})") from None
    return {"entries": g.entries, "bytes": g.bytes}


class _HashIn:
    def __init__(self, f) -> None:
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(n)
        self.h.update(b)
        self.n += len(b)
        return b

    def drain(self) -> None:
        while self.read(COPY):
            pass


# ---------------------------------------------------------------- staging sink

class StageSink:
    """Writes verified members under staging roots: `files/...` into `files_root`, everything else into `root`.
    Never follows a link: every parent is created here or checked to be a real directory, and every file is created
    new (O_EXCL|O_NOFOLLOW)."""

    def __init__(self, root: Path, files_root: Path | None = None) -> None:
        self.root, self.files_root = Path(root), Path(files_root) if files_root else None
        self.dirs: list[tuple[Path, int]] = []
        self.made: list[Path] = []

    def _dest(self, name: str) -> tuple[Path, list[str]]:
        if name == "files" or name.startswith("files/"):
            if not self.files_root:
                raise Unsafe("files/ entries but no files staging folder")
            return self.files_root, name.split("/")[1:]
        return self.root, name.split("/")

    def _parent(self, base: Path, parts: list[str]) -> Path:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if base not in self.made:
            self.made.append(base)
        cur = base
        for p in parts:
            cur = cur / p
            try:
                st = datafs.lstat(cur)
            except FileNotFoundError:
                os.mkdir(cur, 0o700)
                continue
            if not stat.S_ISDIR(st.st_mode):
                raise Unsafe(f"staging path {cur} is not a plain folder")
        return cur

    def mkdir(self, name: str, mode: int) -> None:
        base, parts = self._dest(name)
        d = self._parent(base, parts)
        self.dirs.append((d, mode & 0o777))

    def write(self, name: str, src, mode: int, mtime: float) -> Path:
        base, parts = self._dest(name)
        parent = self._parent(base, parts[:-1])
        dst = parent / parts[-1]
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as out:
            while True:
                b = src.read(COPY)
                if not b:
                    break
                out.write(b)
        os.chmod(dst, mode & 0o777)
        os.utime(dst, (mtime, mtime))
        return dst

    def finish(self) -> None:
        for d, mode in reversed(self.dirs):
            os.chmod(d, mode | 0o700)   # keep owner rwx so the move-in and any undo can still work


# ---------------------------------------------------------------- the one pass

def scan(stream, sink: StageSink | None = None, on_manifest=None, progress=None) -> dict:
    """Verify (and with a sink, stage) the whole bundle. Returns {manifest, checksums, files, bytes}."""
    manifest = checksums = None
    seen: dict[str, dict] = {}
    files = total = 0
    try:
        tar = tarfile.open(fileobj=stream, mode="r|")
    except tarfile.TarError as e:
        raise Damaged(f"the backup's contents are not readable ({str(e)[:100]})") from None
    try:
        for i, m in enumerate(tar):
            name = check_member(m)
            if checksums is not None:
                raise Unsafe("entries after checksums.json")
            if i == 0 and name != MANIFEST:
                raise Damaged("manifest.json is not the first entry")
            if name in seen:
                raise Unsafe(f"duplicate entry {name[:120]}")
            if m.isdir():
                seen[name] = {"type": "dir"}
                if sink:
                    sink.mkdir(name, m.mode)
                continue
            src = _HashIn(tar.extractfile(m))
            if name in (MANIFEST, CHECKSUMS):
                if m.size > MAX_JSON:
                    raise Damaged(f"{name} is too large")
                data = src.read()
                try:
                    obj = json.loads(data)
                except ValueError:
                    raise Damaged(f"{name} is not readable") from None
                if name == MANIFEST:
                    manifest = obj
                    if not isinstance(manifest, dict) or manifest.get("format") != 1:
                        raise Damaged("manifest.json is from an unknown format")
                    if on_manifest:
                        on_manifest(manifest)
                    if sink:
                        sink.write(name, io.BytesIO(data), 0o600, m.mtime)
                else:
                    checksums = obj
                    continue
            elif sink:
                dst = sink.write(name, src, m.mode, m.mtime)
                if name.endswith("/data.tar.gz") and name.startswith("apps/"):
                    with open(dst, "rb") as f:
                        check_inner_archive(f)
                elif name.endswith("/data.tar") and name.startswith("apps/"):    # from a restic repository
                    with open(dst, "rb") as f:
                        check_inner_archive(f, "r|")
            elif name.endswith("/data.tar.gz") and name.startswith("apps/"):
                check_inner_archive(src)
                src.drain()
            elif name.endswith("/data.tar") and name.startswith("apps/"):
                check_inner_archive(src, "r|")
                src.drain()
            else:
                src.drain()
            if src.n != m.size:
                raise Damaged(f"{name[:120]} is cut short")
            seen[name] = {"sha256": src.h.hexdigest(), "size": src.n}
            files += 1
            total += src.n
            if progress and files % 500 == 0:
                progress(f"checked {files} files")
    except (tarfile.TarError, EOFError) as e:
        raise Damaged(f"the backup's contents are damaged ({str(e)[:100]})") from None
    if hasattr(stream, "drain"):
        stream.drain()
    if manifest is None:
        raise Damaged("the backup has no manifest.json")
    if checksums is None:
        raise Damaged("the backup has no checksums.json (cut short?)")
    entries = checksums.get("entries") if isinstance(checksums, dict) else None
    if not isinstance(entries, dict):
        raise Damaged("checksums.json is not readable")
    missing = sorted(set(entries) - set(seen))
    extra = sorted(set(seen) - set(entries))
    if missing:
        raise Damaged(f"{len(missing)} entries listed in checksums.json are missing (e.g. {missing[0][:120]})")
    if extra:
        raise Damaged(f"{len(extra)} entries are not listed in checksums.json (e.g. {extra[0][:120]})")
    bad = sorted(n for n, v in seen.items() if entries[n] != v)
    if bad:
        raise Damaged(f"{len(bad)} entries do not match checksums.json (e.g. {bad[0][:120]})")
    if checksums.get("count") != len(seen):
        raise Damaged("checksums.json entry count does not match")
    if sink:
        sink.finish()
    return {"manifest": manifest, "entries": len(seen), "files": files, "bytes": total}
