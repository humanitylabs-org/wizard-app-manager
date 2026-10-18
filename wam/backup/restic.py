"""restic: the pinned binary and one repository (docs/BACKUP.md, "Scheduled backups").

The binary is restic's official static release for Linux (github.com/restic/restic, BSD-2-Clause), pinned by
version and sha256 below. `ensure()` finds it or installs it into ~/.local/bin (install.sh calls
`wam backup restic-install`; `schedule set` and `run` install it lazily on boxes made before), always checking the
pinned sha256 of the downloaded archive and refusing on any mismatch.

A `Repo` runs restic against one target with the owner's recovery key as the repository password. The key goes to
restic only as RESTIC_PASSWORD_FILE: a 0600 file in a 0700 folder created for this run and deleted after it. It is
never on a command line, never in the environment, never logged. S3 keys reach restic as AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY in restic's own environment only (built from scratch, not the box's), and are scrubbed from
any error text.
"""
from __future__ import annotations

import bz2
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .box import Box, log

VERSION = "0.19.1"
# sha256 of restic_<VERSION>_linux_<arch>.bz2, from the release's SHA256SUMS (signature checked against the
# restic maintainers' key when the pin was taken).
PINS = {
    "amd64": "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
    "arm64": "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465",
}
URL = "https://github.com/restic/restic/releases/download/v{v}/restic_{v}_linux_{arch}.bz2"
MIN_VERSION = (0, 17, 0)

# restic exit codes (0.17+)
RC_NO_REPO, RC_LOCKED, RC_WRONG_KEY = 10, 11, 12


class ResticError(RuntimeError):
    pass


class WrongKey(ResticError):
    pass


class NoRepo(ResticError):
    pass


class Refused(ResticError):
    """The storage refused a delete or overwrite (Object Lock, or a policy without delete rights)."""


# ---------------------------------------------------------------- the binary

def arch(machine: str | None = None) -> str:
    m = (machine or platform.machine()).lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise ResticError(f"no pinned restic build for this machine ({m})")


def _home(box: Box) -> Path:
    return Path(box.env.get("HOME") or Path.home())


def default_dest(box: Box) -> Path:
    return _home(box) / ".local" / "bin" / "restic"


def own_dest(box: Box) -> Path:
    """Where wam keeps its pinned restic when ~/.local/bin/restic is someone else's (another version)."""
    return _home(box) / ".local" / "share" / "wizard-backup" / "bin" / f"restic-{VERSION}"


def version_of(path: str | Path) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([str(path), "version"], capture_output=True, text=True, timeout=30,
                             env={"PATH": "/usr/bin:/bin"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"restic (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def _pinned() -> tuple[int, ...]:
    return tuple(int(x) for x in VERSION.split("."))


def find(box: Box) -> str | None:
    if box.env.get("WAM_RESTIC_BIN"):
        p = box.env["WAM_RESTIC_BIN"]
        if not (version_of(p) or (0,)) >= MIN_VERSION:
            raise ResticError(f"WAM_RESTIC_BIN ({p}) is not restic {'.'.join(map(str, MIN_VERSION))} or newer")
        return p
    for p in (default_dest(box), own_dest(box)):
        if p.is_file() and os.access(p, os.X_OK) and version_of(p) == _pinned():
            return str(p)
    return None


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as r:   # noqa: S310 - pinned https URL (or a test file://)
        return r.read()


def install(box: Box, dest: Path | None = None, *, fetch=None, machine: str | None = None) -> dict:
    """Download restic's release archive, check its pinned sha256, unpack and install it (atomically)."""
    a = arch(machine)
    url = (box.env.get("WAM_RESTIC_URL") or URL).format(v=VERSION, arch=a)
    if dest is None:
        dest = default_dest(box)
        if dest.exists() and version_of(dest) != _pinned():
            dest = own_dest(box)       # never replace a restic the owner installed
    log(f"installing restic {VERSION} ({a})")
    data = (fetch or _fetch)(url)
    got = hashlib.sha256(data).hexdigest()
    if got != PINS[a]:
        raise ResticError(f"the downloaded restic archive does not match its pinned checksum (got {got[:16]}…, "
                          f"want {PINS[a][:16]}…); nothing was installed")
    try:
        binary = bz2.decompress(data)
    except (OSError, ValueError):
        raise ResticError("the downloaded restic archive is not readable; nothing was installed") from None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
        with os.fdopen(fd, "wb") as f:
            f.write(binary)
        os.chmod(tmp, 0o755)
        if version_of(tmp) != _pinned():
            raise ResticError(f"the installed restic does not report version {VERSION}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "version": VERSION, "path": str(dest), "sha256": got, "arch": a}


def ensure(box: Box, *, fetch=None) -> str:
    p = find(box)
    if p:
        return p
    if box.env.get("WAM_RESTIC_NO_INSTALL"):
        raise ResticError("restic is not installed on this box (run `wam backup restic-install`)")
    return install(box, fetch=fetch)["path"]


# ---------------------------------------------------------------- one repository

def repo_url(t: dict) -> str:
    k = t.get("kind")
    if k == "s3":
        prefix = (t.get("prefix") or "").strip("/")
        return f"s3:{t['endpoint'].rstrip('/')}/{t['bucket']}" + (f"/{prefix}" if prefix else "")
    if k == "path":
        return str(t["path"])
    if k == "sftp":
        return str(t["url"])
    raise ResticError(f"unknown backup target kind {k!r}")


def options(t: dict) -> list[str]:
    out: list[str] = []
    if t.get("kind") == "s3":
        if t.get("region"):
            out += ["-o", f"s3.region={t['region']}"]
        if t.get("path_style"):
            out += ["-o", "s3.bucket-lookup=path"]
    return out


class Repo:
    """restic against one target. Use as a context manager: the password file exists only inside it."""

    def __init__(self, box: Box, target: dict, password: str, creds: dict | None = None,
                 binary: str | None = None) -> None:
        self.box, self.target = box, target
        self._password = password
        self._creds = creds or {}
        self.binary = binary or ensure(box)
        self.url = repo_url(target)
        self.argv_log: list[list[str]] = []     # every command run (tests check no secret is on any of them)
        self._dir: Path | None = None
        self.last_dump_error: ResticError | None = None

    # ---- secrets live only inside this block
    def __enter__(self) -> "Repo":
        self._dir = Path(tempfile.mkdtemp(prefix="restic-", dir=self.box.work_dir()))
        os.chmod(self._dir, 0o700)
        pw = self._dir / "password"
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self._password)
        return self

    def __exit__(self, *exc) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def cache_dir(self) -> Path:
        return Path(self.box.env.get("WAM_RESTIC_CACHE") or _home(self.box) / ".cache" / "wizard-backup" / "restic")

    def env(self) -> dict:
        if not self._dir:
            raise ResticError("restic used outside its `with Repo(...)` block")
        e = self.box.env
        out = {"PATH": e.get("PATH") or "/usr/local/bin:/usr/bin:/bin", "HOME": str(_home(self.box)),
               "TMPDIR": str(self._dir), "LANG": "C.UTF-8",
               "RESTIC_REPOSITORY": self.url, "RESTIC_PASSWORD_FILE": str(self._dir / "password"),
               "RESTIC_CACHE_DIR": str(self.cache_dir()), "RESTIC_PROGRESS_FPS": "0.0166"}
        for k in ("SSH_AUTH_SOCK", "USER", "LOGNAME"):     # sftp: the box's own ssh config and agent
            if e.get(k):
                out[k] = e[k]
        if self.target.get("kind") == "s3":
            if not (self._creds.get("access_key_id") and self._creds.get("secret_access_key")):
                raise ResticError("no S3 credentials for this target")
            out["AWS_ACCESS_KEY_ID"] = self._creds["access_key_id"]
            out["AWS_SECRET_ACCESS_KEY"] = self._creds["secret_access_key"]
        return out

    def _argv(self, args: list[str]) -> list[str]:
        argv = [self.binary, *options(self.target), *args]
        self.argv_log.append(argv)
        return argv

    def scrub(self, text: str) -> str:
        for s in (self._password, self._creds.get("secret_access_key"), self._creds.get("access_key_id")):
            if s:
                text = text.replace(s, "***")
        return text

    def _fail(self, what: str, rc: int, err: str) -> ResticError:
        tail = self.scrub((err or "").strip())[-600:]
        if rc == RC_WRONG_KEY or "wrong password" in tail:
            return WrongKey("the backup repository does not open with this recovery key (wrong key)")
        if rc == RC_NO_REPO or "repository does not exist" in tail:
            return NoRepo(f"there is no backup repository at {self.url}")
        if re.search(r"Access Denied|AccessDenied|WORM protected|ObjectLocked|object is locked", tail, re.I):
            return Refused(f"restic {what}: the storage refused it ({tail.splitlines()[-1][:200] if tail else ''})")
        return ResticError(f"restic {what} failed (exit {rc}): {tail}")

    def run(self, args: list[str], *, timeout: int = 6 * 3600, what: str | None = None) -> str:
        try:
            p = subprocess.run(self._argv(args), capture_output=True, text=True, timeout=timeout, env=self.env(),
                               stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise ResticError(f"restic is not installed at {self.binary}") from None
        except subprocess.TimeoutExpired:
            raise ResticError(f"restic {what or args[0]} timed out") from None
        if p.returncode:
            raise self._fail(what or args[0], p.returncode, p.stderr or p.stdout)
        return p.stdout

    def run_json(self, args: list[str], **kw):
        out = self.run([*args, "--json"], **kw)
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:   # several JSON lines (forget/prune): the last parseable one
            last = None
            for line in out.splitlines():
                try:
                    last = json.loads(line)
                except ValueError:
                    continue
            return last

    # ---- repository
    def exists(self) -> bool:
        try:
            self.run(["cat", "config"], what="open", timeout=600)
            return True
        except NoRepo:
            return False

    def init(self) -> dict:
        if self.target.get("kind") == "path":
            Path(self.url).parent.mkdir(parents=True, exist_ok=True)
        out = self.run_json(["init"], what="init", timeout=600) or {}
        return {"initialized": True, "id": out.get("id")}

    def open_or_init(self) -> dict:
        return {"initialized": False} if self.exists() else self.init()

    def backup_stream(self, write, *, filename: str, host: str, tags: list[str]) -> dict:
        """`restic backup --stdin`: `write(stream)` writes the whole capture into restic's stdin. If `write` fails
        (or restic dies), restic is killed before its stdin is closed, so no partial snapshot is ever saved."""
        argv = self._argv(["backup", "--stdin", "--stdin-filename", filename, "--host", host,
                           *sum((["--tag", t] for t in tags), []), "--json"])
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=self.env())
        got: dict = {"summary": None, "err": []}

        def read_out():
            for line in p.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("message_type") == "summary":
                    got["summary"] = d
                elif d.get("message_type") == "error":
                    got["err"].append(json.dumps(d.get("error") or d)[:300])

        def read_err():
            for line in p.stderr:
                got["err"].append(line.decode(errors="replace").rstrip())
                del got["err"][:-40]
        threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
        for t in threads:
            t.start()
        try:
            write(p.stdin)
            p.stdin.close()
        except BaseException as e:
            p.kill()
            p.wait()
            for t in threads:
                t.join(10)
            if isinstance(e, (BrokenPipeError, ConnectionResetError)):
                raise self._fail("backup", p.returncode or 1, "\n".join(got["err"])) from None
            raise
        rc = p.wait()
        for t in threads:
            t.join(30)
        if rc or not got["summary"]:
            raise self._fail("backup", rc or 1, "\n".join(got["err"]))
        return got["summary"]

    def forget(self, policy: dict, *, host: str | None, prune: bool = True, max_unused: str | None = None) -> dict:
        args = ["forget", "--group-by", "host"]
        if host:
            args += ["--host", host]
        for k in ("daily", "weekly", "monthly"):
            if int(policy.get(k) or 0) > 0:
                args += [f"--keep-{k}", str(int(policy[k]))]
        if prune:
            args += ["--prune"]
            if max_unused:
                args += ["--max-unused", max_unused]
        out = self.run_json(args, what="forget", timeout=12 * 3600)
        removed = sum(len(g.get("remove") or []) for g in out) if isinstance(out, list) else None
        kept = sum(len(g.get("keep") or []) for g in out) if isinstance(out, list) else None
        return {"removed": removed, "kept": kept, "pruned": prune}

    def check(self, read_data_subset: str | None = None, read_data: bool = False) -> dict:
        args = ["check"]
        if read_data:
            args.append("--read-data")
        elif read_data_subset:
            args.append(f"--read-data-subset={read_data_subset}")
        t0 = time.time()
        self.run(args, what="check", timeout=24 * 3600)
        return {"ok": True, "at": int(time.time()), "seconds": round(time.time() - t0, 1),
                "read_data": "all" if read_data else read_data_subset}

    def snapshots(self, host: str | None = None) -> list[dict]:
        args = ["snapshots"] + (["--host", host] if host else [])
        return self.run_json(args, what="snapshots", timeout=900) or []

    def stats_raw(self) -> dict:
        return self.run_json(["stats", "--mode", "raw-data"], what="stats", timeout=3600) or {}

    @contextmanager
    def dump(self, snapshot: str, path: str):
        """`restic dump <snapshot> <path>` as a readable stream. On leaving: restic must have exited 0."""
        p = subprocess.Popen(self._argv(["dump", snapshot, path]), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        err: list[str] = []

        def read_err():
            for line in p.stderr:
                err.append(line.decode(errors="replace").rstrip())
                del err[:-40]
        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        ok = False
        killed = False
        try:
            yield p.stdout
            ok = True
        finally:
            if not ok:
                try:
                    p.wait(timeout=5)     # restic stopped on its own (damaged data): keep its own error
                except subprocess.TimeoutExpired:
                    p.kill()          # we stopped reading: restic's own exit status says nothing
                    killed = True
            else:
                while p.stdout.read(1 << 20):
                    pass
            rc = p.wait()
            t.join(30)
            self.last_dump_error = self._fail("dump", rc, "\n".join(err)) if rc and not killed else None
            if ok and rc:
                raise self.last_dump_error


__all__ = ["VERSION", "PINS", "ensure", "install", "find", "Repo", "ResticError", "WrongKey", "NoRepo", "Refused",
           "repo_url"]
