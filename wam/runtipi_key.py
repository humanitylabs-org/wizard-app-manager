"""Which secret wam signs its Runtipi CLI tokens with (WAM#839).

Runtipi 4.12 signs `sub=cli` tokens with a private key kept in the backend's `runtipi_auth` volume
(`/data/.secrets/cli-secret`, 64 hex characters, mode 0600); upstream's runtipi-cli reads it with
`docker exec runtipi cat /data/.secrets/cli-secret`. Runtipi 4.10 used `JWT_SECRET` from `~/runtipi/.env`, which
Runtipi keeps world-readable (0664). The fork still accepts both (`verifyCliToken`, packages/backend/src/modules/auth/
auth.middleware.ts) until every tool reads the new key.

On for everyone (show-and-tell yes, WAM#839; the switch `wizard/runtipi-cli-key` is gone):
  - wam reads the key from the running `runtipi` container and signs with it; when the container has no such key (a
    backend older than 4.12) or it can't be read right now, wam signs with JWT_SECRET as before;
  - wam keeps a private copy in `<wizard home>/runtipi-cli-key` (0600, this user only) so the wizard-ux loopback
    tools (tools/store-data-origin.mjs, system-resources.mjs, home-resources-origin.mjs) sign with the same key.
    Those tools use the copy when it holds a valid key and JWT_SECRET otherwise. Whenever wam can't read the key it
    removes the copy, so the tools never keep signing with a key the backend no longer has.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

KEY_FILE = "runtipi-cli-key"            # under the wizard home (~/.wizard-os)
CONTAINER = "runtipi"
CONTAINER_PATH = "/data/.secrets/cli-secret"
KEY = re.compile(r"[a-f0-9]{64}")
CACHE_S = 60                            # one docker exec per minute per process, at most

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _home(home: Path | None) -> Path:
    from .updates import _home as h
    return h(home)


def container_key(docker: list[str] | None = None, timeout: int = 10) -> str | None:
    """The CLI key of the running Runtipi backend, or None (no 4.12 key, container down, docker unreachable)."""
    try:
        if docker is None:
            from .agent_link import _docker
            docker = _docker()
        p = subprocess.run(docker + ["exec", CONTAINER, "cat", CONTAINER_PATH], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    key = p.stdout.strip() if p.returncode == 0 else ""
    return key if KEY.fullmatch(key) else None


def key_path(home: Path | None = None) -> Path:
    return _home(home) / KEY_FILE


def _write_copy(path: Path, key: str) -> None:
    try:
        if path.is_file() and not path.is_symlink() and path.read_text().strip() == key \
                and path.stat().st_mode & 0o077 == 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, key.encode())
        finally:
            os.close(fd)
        tmp.replace(path)
    except OSError:
        pass                            # the shell's tools then keep using JWT_SECRET; wam itself is unaffected


def _drop_copy(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def secret(root: Path, jwt_secret: str, home: Path | None = None, *, read=None, now=None) -> str:
    """The secret to sign wam's CLI tokens with for the Runtipi install at `root` (see the module docstring)."""
    t = (now or time.monotonic)()
    ck = f"{Path(root)}|{_home(home)}"
    with _lock:
        hit = _cache.get(ck)
        if hit and t - hit[0] < CACHE_S:
            return hit[1] or jwt_secret
    path = key_path(home)
    key = (read or container_key)()
    if key:
        _write_copy(path, key)
    else:                               # a backend without the key, or unreadable now: the shell's tools follow
        _drop_copy(path)                # wam back to JWT_SECRET (a stale copy would 401 forever; Codex P2)
    with _lock:
        _cache[ck] = (t, key or "")
    return key or jwt_secret


def clear_cache() -> None:
    with _lock:
        _cache.clear()
