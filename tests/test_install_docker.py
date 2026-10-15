"""Execute install.sh only in an allowlisted PATH with fake privileged/provider commands."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def sandbox(tmp_path, docker_access=True, before=None, **extra_env):
    home = tmp_path / "home"
    root = home / "runtipi"
    (root / "user-config").mkdir(parents=True)
    (root / ".env").write_text("NGINX_PORT=8480\n")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "user-config/tipi-compose.yml").write_text(
        "services:\n  runtipi-reverse-proxy:\n    ports: !override\n      - 127.0.0.1:8480:80\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bash", "sh", "cat", "mkdir", "cp", "chmod", "rm", "date", "dirname", "seq", "grep", "cut", "sed",
                 "mv"):
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
    for name in ("systemctl", "loginctl"):
        tool(name, f'printf "{name} %s\\n" "$*" >> "$CALLS"\n')
    # HEALTH=down makes every health check fail (the App Manager never comes up).
    tool("curl", 'printf "curl %s\\n" "$*" >> "$CALLS"\n[ "${HEALTH:-up}" = up ]\n')
    tool("tailscale", '''printf "tailscale %s\\n" "$*" >> "$CALLS"
[ "$1" = status ] && echo '{"BackendState":"Running"}'
''')
    tool("git", "exit 0\n")
    tool("sleep", "exit 0\n")
    # The stand-in `wam`: logs the call; `bootstrap` prints $BOOTSTRAP_OUT (default: every app installed).
    tool("python3", '''if [ "${1:-}" = -m ] && [ "${2:-}" = wam ]; then
  printf "wam %s\\n" "$*" >> "$CALLS"
  if [ "${3:-}" = bootstrap ]; then
    if [ -n "${BOOTSTRAP_OUT:-}" ]; then printf '%s\\n' "$BOOTSTRAP_OUT"; else echo '{"installed": [], "failed": {}}'; fi
  fi
  exit 0
fi
exec "PYEXE" "$@"
'''.replace("PYEXE", sys.executable))
    env = {"PATH": str(bin_dir), "HOME": str(home), "USER": "sandbox", "TMPDIR": str(tmp_path),
           "CALLS": str(calls), "DOCKER_ACCESS": str(int(docker_access)), "RUNTIPI_ROOT": str(root),
           "PYTHONDONTWRITEBYTECODE": "1", **extra_env}
    if before:
        before(home)
    p = subprocess.run(["bash", str(REPO / "install.sh")], env=env, capture_output=True, text=True, timeout=60)
    outcome_file = home / ".wizard-os/install-outcome.json"
    outcome = json.loads(outcome_file.read_text()) if outcome_file.is_file() else None
    return p, home, root, calls, outcome


@pytest.mark.parametrize("docker_access", [True, False])
def test_existing_install_uses_consistent_docker_privileges(tmp_path, docker_access):
    p, home, root, calls, outcome = sandbox(tmp_path, docker_access)
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
    # wizard-os#28: a clean run says so, and records it
    assert outcome is not None
    assert (outcome["status"], outcome["failed"], outcome["degraded"]) == ("ok", [], [])
    assert "Wizard OS is installed (box-only" in p.stdout


# ---- wizard-os#28: the installer never reports success over a failed required step

def test_a_failed_link_fails_the_install_and_leaves_hermes_alone(tmp_path):
    # Alongside mode with a Hermes home that has no config: the real `wizard link` refuses, so the install fails.
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / "SOUL.md").write_text("sentinel\n")
    p, home, root, calls, outcome = sandbox(tmp_path, WIZARD_MODE="alongside", HERMES_HOME=str(hermes))
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert any("'wizard link' failed" in f for f in outcome["failed"])
    assert "Wizard OS did NOT finish installing" in p.stderr
    assert "Wizard OS is installed" not in p.stdout
    assert sorted(x.name for x in hermes.iterdir()) == ["SOUL.md"]   # nothing written into the owner's Hermes


def test_failed_default_apps_are_reported_as_degraded(tmp_path):
    boot = json.dumps({"installed": ["files"], "failed": {"transcriber": "image pull denied"}})
    p, home, root, calls, outcome = sandbox(tmp_path, BOOTSTRAP_OUT=boot)
    assert p.returncode == 0, p.stdout + p.stderr        # default: unattended image builds keep going
    assert outcome["status"] == "degraded"
    assert outcome["degraded"] == ["some default apps did not install: transcriber: image pull denied"]
    assert "not everything is working" in p.stdout and "transcriber: image pull denied" in p.stdout
    assert "Wizard OS is installed (box-only" not in p.stdout
    p, *_ = sandbox(tmp_path / "strict", BOOTSTRAP_OUT=boot, WIZARD_STRICT="1")
    assert p.returncode == 3


def test_an_app_manager_that_never_answers_fails_the_install(tmp_path):
    p, home, root, calls, outcome = sandbox(tmp_path, HEALTH="down", WIZARD_HEALTH_TRIES="2")
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert any("health check" in f for f in outcome["failed"])
    assert not any(line.startswith("wam -m wam bootstrap") for line in calls.read_text().splitlines())


def owner_hermes(tmp_path):
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / "config.yaml").write_text("model:\n  default: some/model\n")
    (hermes / ".env").write_text("OPENROUTER_API_KEY=x\n")
    return hermes, {p.name: p.read_bytes() for p in hermes.iterdir()}


def test_a_failed_install_never_links_the_owners_hermes(tmp_path):
    # PR19 review P2: App Manager down -> FAILED; alongside must not link Hermes to a gateway that isn't up.
    hermes, before = owner_hermes(tmp_path)
    p, home, root, calls, outcome = sandbox(tmp_path, HEALTH="down", WIZARD_HEALTH_TRIES="2",
                                            WIZARD_MODE="alongside", HERMES_HOME=str(hermes))
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert "your Hermes" in p.stdout and "was not touched" in p.stdout
    log = calls.read_text().splitlines()
    assert not any(line.startswith("wam -m wam alongside") for line in log)
    assert {x.name: x.read_bytes() for x in hermes.iterdir()} == before
    assert not (home / ".wizard-os/linked.json").exists()


def test_a_failed_shell_or_agent_service_fails_the_install(tmp_path):
    # PR19 review P2: the shell / agent-service spots are the only way to use Wizard OS.
    # read the spot defaults, so the test follows wam/defaults.json (shell + agent-service are separate apps now)
    spots = json.loads((REPO / "wam/defaults.json").read_text())["slots"]
    shell, service = spots["shell"]["app"], spots["agent-service"]["app"]
    boot = json.dumps({"installed": ["files"], "failed": {shell: "image pull denied", "browser": "timeout"}})
    p, home, root, calls, outcome = sandbox(tmp_path, BOOTSTRAP_OUT=boot)
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["status"] == "failed"
    assert outcome["failed"] == ["the Wizard shell / Agent Service did not install, so Wizard OS can't be used: "
                                 f"{shell}: image pull denied"]
    assert outcome["degraded"] == ["some default apps did not install: browser: timeout"]
    assert "Wizard OS did NOT finish installing" in p.stderr
    boot = json.dumps({"installed": [shell], "failed": {service: "health check timed out"}})
    p, home, root, calls, outcome = sandbox(tmp_path / "service", BOOTSTRAP_OUT=boot)
    assert p.returncode == 1, p.stdout + p.stderr
    assert outcome["failed"] == ["the Wizard shell / Agent Service did not install, so Wizard OS can't be used: "
                                 f"{service}: health check timed out"]


def test_the_outcome_path_is_printed_only_when_it_was_written(tmp_path):
    # PR19 review P3: the outcome write is best-effort; don't point the owner at a file that isn't there.
    def block(home):
        (home / ".wizard-os/install-outcome.json").mkdir(parents=True)   # a directory: the write fails
    p, home, root, calls, outcome = sandbox(tmp_path, before=block, HEALTH="down", WIZARD_HEALTH_TRIES="2")
    assert p.returncode == 1, p.stdout + p.stderr
    assert "Wizard OS did NOT finish installing" in p.stderr
    assert "Recorded in" not in p.stderr
    p, *_ = sandbox(tmp_path / "ok", HEALTH="down", WIZARD_HEALTH_TRIES="2")
    assert "Recorded in" in p.stderr
