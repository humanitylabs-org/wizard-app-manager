"""Fresh Ubuntu automatic updates must not make server preparation fail fast.

Execute the real preparation function with allowlisted command doubles. Never
invoke the host package manager, Docker, network, or systemd.
"""
import os
from pathlib import Path
import re
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'get.sh'


@pytest.mark.parametrize('apt_exit', [0, 100])
def test_server_preparation_waits_for_ubuntu_automatic_updates(tmp_path, apt_exit):
    source = SCRIPT.read_text()
    body = source.split('prepare_server() {', 1)[1].split('\n}\n', 1)[0]
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    calls = tmp_path / 'calls'
    doubles = {
        'apt-get': '''printf 'apt-get %s\\n' "$*" >> "$CALLS"
# A busy frontend lock fails immediately unless apt is asked to wait.
[ "$1" = -o ] && [ "$2" = DPkg::Lock::Timeout=600 ] || exit 99
exit "$APT_EXIT"''',
        'docker': 'printf "28.0.0\\n"',
        'systemctl': 'printf "systemctl %s\\n" "$*" >> "$CALLS"',
        'curl': 'exit 98',
    }
    for name, stub in doubles.items():
        command = bindir / name
        command.write_text('#!/bin/sh\nset -eu\n' + stub + '\n')
        command.chmod(0o755)
    result = subprocess.run(
        ['bash', '-c', 'set -e\nsay() { printf "%s\\n" "$*"; }\n'
         + 'prepare_server() {' + body + '\n}\nprepare_server\n'],
        env={**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin',
             'CALLS': str(calls), 'APT_EXIT': str(apt_exit)},
        capture_output=True, text=True,
    )
    assert result.returncode == apt_exit, result.stdout + result.stderr
    assert "Waiting for Ubuntu's automatic updates to finish" in result.stdout
    recorded = calls.read_text().splitlines()
    assert recorded[0] == 'apt-get -o DPkg::Lock::Timeout=600 update -qq'
    if apt_exit == 0:
        assert recorded[1] == ('apt-get -o DPkg::Lock::Timeout=600 install -y -qq '
                               'ca-certificates curl git python3 python3-yaml gh sudo')
        assert recorded[2] == 'systemctl enable --now docker'
    else:
        assert len(recorded) == 1  # Real apt failures still stop preparation.


def test_every_executed_bootstrap_apt_call_has_a_bounded_lock_wait():
    # Excludes the diagnostic echo that tells an owner how to install Python.
    calls = [line for line in SCRIPT.read_text().splitlines()
             if re.search(r'(^\s*|\|\| sudo )apt-get ', line)]
    assert len(calls) == 3
    assert all('apt-get -o DPkg::Lock::Timeout=600 ' in line for line in calls)
