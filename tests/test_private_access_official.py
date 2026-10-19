"""Opt-in, network-isolated official-binary contract check; NOT owner-login acceptance.

WIZARD_TEST_OFFICIAL_TAILSCALE=1 python -m pytest tests/test_private_access_official.py -v
Requires Docker, existing python:3.12-slim image, and static official Linux binaries.
Never mounts a host socket/state directory, uses no control-plane access or auth keys.
"""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.environ.get("WIZARD_TEST_OFFICIAL_TAILSCALE") != "1", reason="opt-in Docker contract check")
def test_real_needslogin_preserves_nondefault_preferences():
    script = Path(__file__).resolve().parents[1] / "scripts/setup-private-access.sh"
    command = r'''
set -eu
mkdir -p /state/bin
printf '#!/bin/sh\nexec /official/tailscale --socket=/state/socket "$@"\n' > /state/bin/tailscale
printf '#!/bin/sh\nexit 0\n' > /state/bin/systemctl
chmod +x /state/bin/*
export PATH=/state/bin:$PATH HOME=/state TMPDIR=/state WIZARD_TAILSCALE_WAIT=1
# A stopped daemon is a real CLI connection error, not JSON state Running.
if tailscale status --json >/state/down 2>&1; then exit 1; fi
/official/tailscaled --tun=userspace-networking --state=/state/state --socket=/state/socket >/state/daemon.log 2>&1 &
pid=$!
trap 'kill "$pid"; wait "$pid" || true' EXIT
for n in 1 2 3 4 5; do [ -S /state/socket ] && break; sleep 1; done
tailscale version
# The real unregistered daemon reports NeedsLogin, no AuthURL without networking.
tailscale status --json > /state/status.json
python -c 'import json; assert json.load(open("/state/status.json"))["BackendState"] == "NeedsLogin"'
bash /setup.sh
# Set real persisted preferences after initial up has established ControlURL.
tailscale set --accept-dns=false --accept-routes=true --hostname=fixture
tailscale debug prefs > /state/before.json
bash /setup.sh
tailscale debug prefs > /state/after.json
python -c 'import json; a=json.load(open("/state/before.json")); b=json.load(open("/state/after.json")); assert a == b, (a,b); assert b["CorpDNS"] is False and b["RouteAll"] is True and b["Hostname"] == "fixture"; print("PASS: all persisted preferences preserved in real NeedsLogin")'
'''
    result = subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--tmpfs", "/state:exec",
        "-v", "/usr/bin/tailscale:/official/tailscale:ro",
        "-v", "/usr/sbin/tailscaled:/official/tailscaled:ro",
        "-v", f"{script}:/setup.sh:ro", "python:3.12-slim", "bash", "-c", command,
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all persisted preferences preserved" in result.stdout
