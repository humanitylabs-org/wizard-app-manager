"""Fresh base host preparation uses local command doubles, never real package/network/ownership operations."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare-named-user.sh'


def test_fresh_admin_phase_keeps_base_loopback_only_and_preserves_database_uid(tmp_path):
    home = tmp_path / 'owner'; home.mkdir()
    bindir = tmp_path / 'bin'; bindir.mkdir()
    calls = tmp_path / 'calls'
    fixture = tmp_path / 'base-setup.sh'
    fixture.write_text('mkdir -p "$OWNER_HOME/runtipi/data"\nprintf "BASE_READY=1\\n" > "$OWNER_HOME/runtipi/.env"\n')
    commands = {
        'id': 'case "$*" in "-u") echo 0;; "-u alice"|"-g alice") echo 1234;; esac',
        'getent': 'printf "alice:x:1234:1234::%s:/bin/bash\\n" "$OWNER_HOME"',
        'install': 'mkdir -p "${@: -1}"',
        'curl': 'printf "curl %s\\n" "$*" >> "$CALLS"; cp "$SETUP_FIXTURE" "${@: -1}"',
        'chown': 'printf "chown %s\\n" "$*" >> "$CALLS"',
        'find': 'printf "find %s\\n" "$*" >> "$CALLS"',
        'usermod': 'printf "usermod %s\\n" "$*" >> "$CALLS"',
        'loginctl': 'printf "loginctl %s\\n" "$*" >> "$CALLS"',
        'sudo': 'echo forbidden-sudo >> "$CALLS"; exit 99',
    }
    for name, body in commands.items():
        executable = bindir / name
        executable.write_text('#!/bin/bash\nset -eu\n' + body + '\n'); executable.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'OWNER_HOME': str(home), 'CALLS': str(calls),
           'SETUP_FIXTURE': str(fixture), 'WIZARD_PRIVATE_ACCESS': 'none', 'TMPDIR': str(tmp_path)}
    result = subprocess.run(['bash', str(SCRIPT), 'alice'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    compose = (home / 'runtipi/user-config/tipi-compose.yml').read_text()
    assert '127.0.0.1:8480:80' in compose and '127.0.0.1:8443:443' in compose
    assert '-mindepth 1 -maxdepth 1 ! -name data' in calls.read_text()
    assert 'chown 1234:1234' in calls.read_text()
    assert 'forbidden-sudo' not in calls.read_text()
    assert (home / 'runtipi/.env').read_text() == 'BASE_READY=1\n'
