"""The administrator phase runs with allowlisted doubles, never host services or Docker."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare-named-user.sh'


def test_admin_prepares_existing_base_without_granting_sudo_or_touching_data(tmp_path):
    home = tmp_path / 'owner'; (home / 'runtipi/data').mkdir(parents=True)
    (home / 'runtipi/.env').write_text('OWNER_SENTINEL=keep\n')
    sentinel = home / 'runtipi/data/sentinel'; sentinel.write_text('keep')
    bindir = tmp_path / 'bin'; bindir.mkdir()
    log = tmp_path / 'calls'
    commands = {
        'id': 'case "$*" in "-u") echo 0;; "-u alice"|"-g alice") echo 1234;; esac',
        'getent': 'printf "alice:x:1234:1234::%s:/bin/bash\\n" "$OWNER_HOME"',
        'usermod': 'printf "usermod %s\\n" "$*" >> "$CALLS"',
        'loginctl': 'printf "loginctl %s\\n" "$*" >> "$CALLS"',
        'sudo': 'echo forbidden-sudo >> "$CALLS"; exit 99',
        'curl': 'echo forbidden-download >> "$CALLS"; exit 99',
    }
    for name, body in commands.items():
        p = bindir / name; p.write_text('#!/bin/bash\nset -eu\n' + body + '\n'); p.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'OWNER_HOME': str(home), 'CALLS': str(log),
           'WIZARD_PRIVATE_ACCESS': 'none'}
    result = subprocess.run(['bash', str(SCRIPT), 'alice'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ['usermod -aG docker alice', 'loginctl enable-linger alice']
    assert (home / 'runtipi/.env').read_text() == 'OWNER_SENTINEL=keep\n'
    assert sentinel.read_text() == 'keep'
    assert 'no passwordless sudo was granted' in result.stdout


def test_admin_preparation_rejects_nonroot_before_any_other_command(tmp_path):
    bindir = tmp_path / 'bin'; bindir.mkdir()
    p = bindir / 'id'; p.write_text('#!/bin/sh\necho 1234\n'); p.chmod(0o755)
    result = subprocess.run(['bash', str(SCRIPT), 'alice'], env={'PATH': f'{bindir}:/usr/bin:/bin'},
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'requires an administrator' in result.stderr
