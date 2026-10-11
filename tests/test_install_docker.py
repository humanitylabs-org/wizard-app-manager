"""Execute install.sh only in an allowlisted PATH with fake privileged/provider commands."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("docker_access", [True, False])
def test_existing_install_uses_consistent_docker_privileges(tmp_path, docker_access):
    home = tmp_path / "home"
    root = home / "runtipi"
    (root / "user-config").mkdir(parents=True)
    (root / ".env").write_text("NGINX_PORT=8480\n")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "user-config/tipi-compose.yml").write_text(
        "services:\n  runtipi-reverse-proxy:\n    ports: !override\n      - 127.0.0.1:8480:80\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bash", "cat", "mkdir", "cp", "chmod", "rm", "date", "dirname", "seq", "grep", "cut"):
        (bin_dir / name).symlink_to(shutil.which(name))
    calls = tmp_path / "calls"

    def tool(name, body):
        target = bin_dir / name
        target.write_text("#!/bin/bash\nset -eu\n" + body)
        target.chmod(0o755)

    tool("id", 'case "${1:-}" in -un) echo sandbox;; *) echo 1000;; esac\n')
    tool("sudo", 'printf "sudo %s\\n" "$*" >> "$CALLS"; export MOCK_SUDO=1; exec "$@"\n')
    tool("docker", '''printf "docker sudo=%s %s\\n" "${MOCK_SUDO:-0}" "$*" >> "$CALLS"
if [ "$1" = info ] && [ "$DOCKER_ACCESS" = 0 ]; then exit 1; fi
if [ "$1" != info ] && [ "$DOCKER_ACCESS" = 0 ] && [ "${MOCK_SUDO:-0}" != 1 ]; then exit 1; fi
if [ "$1" = image ]; then exit 1; fi
''')
    for name in ("systemctl", "loginctl", "curl"):
        tool(name, f'printf "{name} %s\\n" "$*" >> "$CALLS"\n')
    tool("tailscale", '''printf "tailscale %s\\n" "$*" >> "$CALLS"
[ "$1" = status ] && echo '{"BackendState":"Running"}'
''')
    tool("git", "exit 0\n")
    tool("python3", f'''if [ "${{1:-}}" = -m ] && [ "${{2:-}}" = wam ]; then
  printf "wam %s\\n" "$*" >> "$CALLS"
  exit 0
fi
exec "{sys.executable}" "$@"
''')
    env = {"PATH": str(bin_dir), "HOME": str(home), "USER": "sandbox", "TMPDIR": str(tmp_path),
           "CALLS": str(calls), "DOCKER_ACCESS": str(int(docker_access)), "RUNTIPI_ROOT": str(root),
           "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run(["bash", str(REPO / "install.sh")], env=env, capture_output=True, text=True, timeout=15)
    assert p.returncode == 0, p.stdout + p.stderr
    log = calls.read_text().splitlines()
    docker_calls = [line for line in log if line.startswith("docker ")]
    assert docker_calls[0] == "docker sudo=0 info"
    actual = [line.split(" ", 2)[2] for line in docker_calls[1:]]
    assert actual[0].startswith("exec runtipi-db psql ")
    assert actual[1:] == [
        "image inspect ghcr.io/humanitylabs-org/wizard-app-manager:latest",
        "pull ghcr.io/humanitylabs-org/wizard-app-manager:latest",
        "compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-deps runtipi",
        "rm -f wizard-store wizard-skin",
    ]
    privilege = "0" if docker_access else "1"
    assert all(line.startswith(f"docker sudo={privilege} ") for line in docker_calls[1:])
    assert "tailscale up" not in "\n".join(log)
    assert any("enable --now tailscaled" in line for line in log)
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"
    assert log.index("tailscale status --json") < log.index("docker sudo=0 info")
    compose = (root / "user-config/tipi-compose.yml").read_text()
    assert "ports: !override" in compose and "127.0.0.1:8480:80" in compose
    assert "image: ghcr.io/humanitylabs-org/wizard-app-manager:latest" in compose
    assert any(line.startswith("wam -m wam bootstrap") for line in log)
