"""The base Docker preflight is exercised with no real privileged/network commands."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]


def run_preflight(tmp_path, release='ID=debian\n', existing=False, download_rc=0, install_rc=0):
    source = REPO / 'scripts/ensure-base-docker.sh'
    assert source.is_file(), 'fresh Debian installs need a Docker preflight before upstream Runtipi'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for cmd in ('mktemp', 'rm', 'sh'):
        (bin_dir / cmd).symlink_to(shutil.which(cmd))
    calls = tmp_path / 'calls'
    release_file = tmp_path / 'os-release'
    release_file.write_text(release)
    for name, body in {
        'sudo': 'printf "sudo %s\\n" "$*" >> "$CALLS"; exec "$@"',
        'curl': '''printf "curl %s\\n" "$*" >> "$CALLS"
[ "$DOWNLOAD_RC" = 0 ] || exit "$DOWNLOAD_RC"
[ "$1" = -fsSL ] && [ "$2" = https://get.docker.com ] && [ "$3" = -o ]
printf 'printf "official-docker-script\\n" >> "$CALLS"; exit "$INSTALL_RC"\n' > "$4"
''',
    }.items():
        p = bin_dir / name
        p.write_text('#!/bin/bash\nset -eu\n' + body + '\n')
        p.chmod(0o755)
    if existing:
        p = bin_dir / 'docker'
        p.write_text('#!/bin/sh\nexit 0\n')
        p.chmod(0o755)
    # Replace only the standard release path in the isolated test copy, not a product override.
    script = tmp_path / 'preflight.sh'
    script.write_text(source.read_text().replace('/etc/os-release', str(release_file)))
    env = {'PATH': str(bin_dir), 'TMPDIR': str(tmp_path), 'CALLS': str(calls),
           'DOWNLOAD_RC': str(download_rc), 'INSTALL_RC': str(install_rc)}
    p = subprocess.run(['/bin/bash', '-euo', 'pipefail', str(script)], env=env,
                       capture_output=True, text=True, timeout=10)
    return p, calls.read_text() if calls.exists() else '', list(tmp_path.glob('wizard-base-docker.*'))


def test_debian_cloud_uses_official_docker_before_upstream(tmp_path):
    p, log, leftovers = run_preflight(tmp_path)
    assert p.returncode == 0, p.stderr
    assert 'curl -fsSL https://get.docker.com -o ' in log
    assert 'sudo sh ' in log
    assert 'official-docker-script' in log
    assert not leftovers


@pytest.mark.parametrize('release', ['ID="debian"\n', 'ID=debian\nVARIANT_ID=genericcloud\n'])
def test_standard_release_id_is_one_value(tmp_path, release):
    p, log, leftovers = run_preflight(tmp_path, release)
    assert p.returncode == 0 and 'official-docker-script' in log
    assert not leftovers


@pytest.mark.parametrize('release,existing', [('ID=ubuntu\n', False), ('ID=debian\n', True), ('NAME=Unknown\n', False)])
def test_other_distros_and_existing_docker_are_unchanged(tmp_path, release, existing):
    p, log, leftovers = run_preflight(tmp_path, release, existing)
    assert p.returncode == 0
    assert not log and not leftovers


@pytest.mark.parametrize('download_rc,install_rc', [(22, 0), (0, 42)])
def test_failure_aborts_and_removes_private_download(tmp_path, download_rc, install_rc):
    p, log, leftovers = run_preflight(tmp_path, download_rc=download_rc, install_rc=install_rc)
    assert p.returncode == (download_rc or install_rc)
    assert not leftovers
    if download_rc:
        assert 'sudo ' not in log


def test_preflight_is_only_on_missing_base_and_precedes_upstream():
    source = (REPO / 'install.sh').read_text()
    missing = source.index('if [ ! -f "$RUNTIPI_ROOT/.env" ]')
    preflight = source.index('source "$HERE/scripts/ensure-base-docker.sh"')
    upstream = source.index('curl -fsSL https://setup.runtipi.io')
    assert missing < preflight < upstream
