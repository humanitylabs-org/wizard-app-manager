"""Exercise the actual host setup script with isolated command doubles; never touch the host's tailnet."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-private-access.sh"


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Only allowlisted utilities: no real host tailscale can leak into the fresh-install test.
    for utility in ("bash", "sh", "cat", "mkdir", "mktemp", "rm", "cp", "chmod"):
        (bin_dir / utility).symlink_to(shutil.which(utility))
    (bin_dir / "python3").symlink_to(sys.executable)
    log = tmp_path / "calls"
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.write_text("Running")

    def tool(name, body):
        p = bin_dir / name
        p.write_text("#!/bin/bash\nset -eu\n" + body)
        p.chmod(0o755)

    tool("id", 'echo 1000\n')
    tool("sudo", 'printf "sudo %s\\n" "$*" >> "$CALLS"; exec "$@"\n')
    tool("systemctl", 'printf "systemctl %s\\n" "$*" >> "$CALLS"; exit "${SERVICE_RC:-0}"\n')
    tailscale_body = '''printf "tailscale %s\\n" "$*" >> "$CALLS"
case "$1" in
status) printf '{"BackendState":"%s"}\\n' "$(cat "$STATE_FILE")" ;;
up)
  echo "login link from mock"
  if [ "${UP_STATE:-NeedsLogin}" = "Running" ]; then echo Running > "$STATE_FILE"; exit 0; fi
  exit 1
  ;;
esac
'''
    tool("tailscale", tailscale_body)
    # Fresh installation: download a test script that records the install and provides the mock binary.
    tool("curl", '''printf "curl %s\\n" "$*" >> "$CALLS"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then cp "$INSTALL_FIXTURE" "$2"; break; fi
  shift
done
''')
    fixture = tmp_path / "install-fixture.sh"
    saved = tmp_path / "tailscale-fixture"
    saved.write_text((bin_dir / "tailscale").read_text())
    fixture.write_text('echo "package installed" >> "$CALLS"\ncp "$TAILSCALE_FIXTURE" "$TEST_BIN/tailscale"\nchmod +x "$TEST_BIN/tailscale"\n')
    env = {"PATH": str(bin_dir), "HOME": str(home), "CALLS": str(log),
           "STATE_FILE": str(state), "INSTALL_FIXTURE": str(fixture), "TAILSCALE_FIXTURE": str(saved),
           "TEST_BIN": str(bin_dir), "TMPDIR": str(tmp_path)}
    return bin_dir, log, home, state, env


def run(sandbox, **overrides):
    *_, env = sandbox
    return subprocess.run(["bash", str(SCRIPT)], env={**env, **overrides}, capture_output=True, text=True, timeout=10)


def test_existing_connection_is_not_reconfigured(sandbox):
    _, log, home, _, _ = sandbox
    p = run(sandbox)
    assert p.returncode == 0, p.stderr
    calls = log.read_text()
    assert "tailscale up" not in calls
    assert "package installed" not in calls
    assert "enable --now tailscaled" in calls
    assert json.loads((home / ".wizard-os/private-access.json").read_text()) == {
        "provider": "tailscale", "runtime": "host", "state": "Running", "installed": True}


def test_fresh_install_can_leave_login_pending(sandbox):
    bin_dir, log, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    (bin_dir / "tailscale").unlink()
    p = run(sandbox)
    assert p.returncode == 0, p.stderr
    assert "package installed" in log.read_text()
    assert "tailscale up --timeout=" in log.read_text()
    assert "not connected yet" in p.stdout
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "NeedsLogin"


def test_login_completes(sandbox):
    _, _, home, state, _ = sandbox
    state.write_text("NeedsLogin")
    p = run(sandbox, UP_STATE="Running")
    assert p.returncode == 0, p.stderr
    assert json.loads((home / ".wizard-os/private-access.json").read_text())["state"] == "Running"


@pytest.mark.parametrize("provider", ["cloudflare", "none"])
def test_alternative_does_not_touch_tailscale(sandbox, provider):
    _, log, home, _, _ = sandbox
    p = run(sandbox, WIZARD_PRIVATE_ACCESS=provider)
    assert p.returncode == 0
    assert not log.exists()
    assert not (home / ".wizard-os/private-access.json").exists()


def test_stopped_existing_node_is_not_reset(sandbox):
    _, log, _, state, _ = sandbox
    state.write_text("Stopped")
    assert run(sandbox).returncode == 0
    assert "tailscale up" not in log.read_text()


def test_daemon_failure_is_not_reported_as_installed(sandbox):
    _, _, home, _, _ = sandbox
    assert run(sandbox, SERVICE_RC="1").returncode != 0
    assert not (home / ".wizard-os/private-access.json").exists()


def test_invalid_provider_fails_without_mutations(sandbox):
    _, log, _, _, _ = sandbox
    assert run(sandbox, WIZARD_PRIVATE_ACCESS="invalid").returncode != 0
    assert not log.exists()
