"""Reach Docker as the service account, including its existing rootless daemon.

Probe with the backup child environment, never the engine's credentials.
Socket failures stay unprivileged; host preparation and a fresh login fix group access.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

_cached: dict[tuple, tuple[float, tuple[str, ...]]] = {}
_lock = threading.Lock()


def docker(env: dict | None = None) -> list[str]:
    # Lazy import: box and filesystem also use this helper.
    from .box import child_env

    env = child_env(dict(os.environ if env is None else env))
    key = tuple(sorted((k, v) for k, v in env.items()
                       if k.startswith("DOCKER_") or k in ("PATH", "HOME", "XDG_RUNTIME_DIR")))
    with _lock:
        hit = _cached.get(key)
        if hit and time.monotonic() - hit[0] < 30:
            return list(hit[1])

        def run(argv):
            return subprocess.run(argv, capture_output=True, timeout=20, check=True,
                                  stdin=subprocess.DEVNULL, env=env)

        def remember(argv):
            # Cache successes briefly, not transient failures; bounded per process.
            if len(_cached) >= 32:
                _cached.clear()
            _cached[key] = (time.monotonic(), tuple(argv))
            return list(argv)

        try:
            run(["docker", "info", "--format", "{{.ID}}"])
            return remember(["docker"])
        except (OSError, subprocess.SubprocessError):
            pass

        # Rootless Docker (the installer's ~/bin/docker + /run/user/<uid>/docker.sock): a systemd unit's PATH and
        # environment do not carry it, so look for it before returning the original route.
        if not env.get("DOCKER_HOST") and not env.get("DOCKER_CONTEXT") and not shutil.which("docker", path=env.get("PATH")):
            sock = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "docker.sock"
            binary = Path(env.get("HOME") or Path.home()) / "bin" / "docker"
            if binary.is_file() and sock.is_socket():
                argv = [str(binary), "--host", f"unix://{sock}"]
                try:
                    run(argv + ["info", "--format", "{{.ID}}"])
                    return remember(argv)
                except (OSError, subprocess.SubprocessError):
                    pass

    return ["docker"]  # Preserve the caller's actual error; never prompt.


def verb(argv: list[str]) -> str:
    """The docker subcommand in argv, including the pinned rootless socket form."""
    i = argv.index("docker") if "docker" in argv else 0
    i += 1
    if argv[i:i + 1] == ["--host"]:
        i += 2
    return argv[i] if len(argv) > i else ""
