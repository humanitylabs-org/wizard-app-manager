"""How this process reaches Docker. A fresh box adds the owner to the docker group during install, but that only
takes effect at the next login, so the owner's current shell (and the Settings panel's service) gets "permission
denied" on the socket. Same rule as install.sh and the rest of wam: plain `docker` when it works, else
`sudo -n docker` when that works, else plain `docker` so the real error surfaces."""
from __future__ import annotations

import subprocess

_cached: list[str] | None = None


def docker() -> list[str]:
    global _cached
    if _cached is None:
        _cached = ["docker"]
        for argv in (["docker"], ["sudo", "-n", "docker"]):
            try:
                subprocess.run(argv + ["info", "--format", "{{.ID}}"], capture_output=True, timeout=20, check=True,
                               stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                continue
            _cached = argv
            break
    return list(_cached)


def verb(argv: list[str]) -> str:
    """The docker subcommand in argv, for messages."""
    i = argv.index("docker") if "docker" in argv else 0
    return argv[i + 1] if len(argv) > i + 1 else ""
