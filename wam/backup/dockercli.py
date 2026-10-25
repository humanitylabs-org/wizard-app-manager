"""Reach the same local Docker daemon before the owner's group login takes effect.

Probe with the backup child environment, never the engine's credentials. Sudo is
noninteractive and pins a local socket explicitly: sudo's HOME/config must not
silently select a different daemon. Custom contexts/TLS stay unprivileged.
"""
from __future__ import annotations

import os
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

        # A root context or TLS config could point at another machine. Do not
        # escalate remote connections or forward credentials with sudo -E.
        if (env.get("DOCKER_CONTEXT", "") not in ("", "default")
                or any(env.get(k) for k in ("DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_TLS"))):
            return ["docker"]
        host = "" if env.get("DOCKER_CONTEXT") else env.get("DOCKER_HOST", "")
        if not host:
            try:
                context = run(["docker", "context", "show"]).stdout
                if isinstance(context, bytes):
                    context = context.decode()
                if context.strip() != "default":
                    return ["docker"]
            except (OSError, subprocess.SubprocessError):
                return ["docker"]
            host = "unix:///var/run/docker.sock"
        if host.startswith("unix:///"):
            argv = ["sudo", "-n", "docker", "--host", host]
            try:
                run(argv + ["info", "--format", "{{.ID}}"])
                return remember(argv)
            except (OSError, subprocess.SubprocessError):
                pass
    return ["docker"]  # Preserve the caller's actual error; never prompt.


def verb(argv: list[str]) -> str:
    """The docker subcommand in argv, including the pinned-socket sudo form."""
    i = argv.index("docker") if "docker" in argv else 0
    i += 1
    if argv[i:i + 1] == ["--host"]:
        i += 2
    return argv[i] if len(argv) > i else ""
