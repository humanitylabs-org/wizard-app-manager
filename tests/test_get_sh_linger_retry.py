"""Exercise the real shell function with command doubles, never host logind."""
import os
from pathlib import Path
import subprocess

import pytest

GET = Path(__file__).resolve().parents[1] / 'get.sh'


@pytest.mark.parametrize('failures,exit_code,attempts,delays', [
    (0, 0, 1, []), (2, 0, 3, ['2', '4']), (99, 7, 4, ['2', '4', '6']),
])
def test_enable_linger_retry(tmp_path, failures, exit_code, attempts, delays):
    source = GET.read_text()
    helper = source.split('enable_linger() {', 1)[1].split('\n}', 1)[0]
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    doubles = {
        'loginctl': '''n=0; [ ! -f "$COUNT" ] || n=$(<"$COUNT")
n=$((n+1)); printf '%s' "$n" > "$COUNT"
printf '%s\\n' "$*" >> "$CALLS"
[ "$n" -gt "$FAILURES" ] || exit 7
''',
        'sleep': 'printf "%s\\n" "$1" >> "$DELAYS"\n',
        # Never consult host account state or invoke a real privileged fallback.
        'id': 'echo 1001\n',
        'systemctl': 'exit 99\n',
        'install': 'exit 99\n',
    }
    for name, body in doubles.items():
        path = bindir / name
        path.write_text('#!/bin/bash\nset -eu\n' + body)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'COUNT': str(tmp_path / 'count'),
           'CALLS': str(tmp_path / 'calls'), 'DELAYS': str(tmp_path / 'delays'), 'FAILURES': str(failures)}
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\nenable_linger() {' + helper + '\n}\nenable_linger wizard\necho continued'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == exit_code
    assert (tmp_path / 'calls').read_text().splitlines() == ['enable-linger wizard'] * attempts
    assert ((tmp_path / 'delays').read_text().splitlines() if (tmp_path / 'delays').exists() else []) == delays
    assert ('continued' in result.stdout) == (exit_code == 0)
    if exit_code:
        assert 'after 4 attempts' in result.stderr


def test_fresh_root_path_uses_required_retry_and_other_paths_keep_policy():
    source = GET.read_text()
    assert '  enable_linger "$WUSER"\n' in source
    assert 'enable_linger "$u" >/dev/null 2>&1 || true' in source
    assert 'enable_linger root >/dev/null 2>&1 || true' in source
    assert source.count('loginctl enable-linger') == 1


@pytest.mark.parametrize('systemctl_rc,restorecon_rc,install_rc,user,exit_code', [
    (0, 0, 0, 'wizard', 0), (1, 0, 0, 'wizard', 7),
    (0, 1, 0, 'wizard', 7), (0, 0, 1, 'wizard', 7),
    (0, 0, 0, 'unusual.name', 7),
])
def test_root_falls_back_to_linger_flag_when_logind_never_answers(
        tmp_path, systemctl_rc, restorecon_rc, install_rc, user, exit_code):
    """0.3.46 Thales gate: logind timed out on all 4 attempts on a fresh VM, aborting the install."""
    helper = GET.read_text().split('enable_linger() {', 1)[1].split('\n}', 1)[0]
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'log'
    doubles = {
        'loginctl': 'exit 7\n',
        'sleep': ':\n',
        'id': '[ "$#" -eq 1 ] && [ "$1" = -u ] && { echo 0; exit 0; }; echo 1001\n',
        'install': f'printf "install %s\\n" "$*" >> "$LOG"; exit {install_rc}\n',
        'touch': 'printf "touch %s\\n" "$*" >> "$LOG"\n',
        'restorecon': f'printf "restorecon %s\\n" "$*" >> "$LOG"; exit {restorecon_rc}\n',
        'systemctl': f'printf "systemctl %s\\n" "$*" >> "$LOG"; exit {systemctl_rc}\n',
    }
    for name, body in doubles.items():
        path = bindir / name
        path.write_text('#!/bin/bash\nset -eu\n' + body)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'LOG': str(log), 'TEST_USER': user}
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\nenable_linger() {' + helper + '\n}\nenable_linger "$TEST_USER"\necho continued'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == exit_code
    calls = log.read_text().splitlines() if log.exists() else []
    if user != 'wizard':
        assert calls == []
    elif install_rc:
        assert calls == ['install -d -m 0755 /var/lib/systemd/linger']
    else:
        assert 'touch /var/lib/systemd/linger/wizard' in calls
        assert 'restorecon /var/lib/systemd/linger /var/lib/systemd/linger/wizard' in calls
        assert ('systemctl start user@1001.service' in calls) == (restorecon_rc == 0)
    if exit_code == 0:
        assert calls.index('restorecon /var/lib/systemd/linger /var/lib/systemd/linger/wizard') < calls.index('systemctl start user@1001.service')
        assert 'enabled linger for wizard directly' in result.stderr and 'continued' in result.stdout
    else:
        assert 'continued' not in result.stdout
