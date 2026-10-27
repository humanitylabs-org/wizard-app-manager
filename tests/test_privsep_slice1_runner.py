"""Validate the VM recipe without executing the side-effecting acceptance runner."""
import ast
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

RUNNER = Path(__file__).resolve().parents[1] / 'qa/privsep-slice1/run.py'


def test_both_clean_guests_get_the_same_credential_free_hub_mirror():
    tree = ast.parse(RUNNER.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'guest_cloud_config')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(RUNNER), 'exec'), namespace)
    public = 'ssh-ed25519 disposable-test-public-key'
    config = yaml.safe_load(namespace['guest_cloud_config'](public))
    files = {entry['path']: entry for entry in config['write_files']}
    mirror = files['/etc/docker/daemon.json']
    assert json.loads(mirror['content']) == {'registry-mirrors': ['https://mirror.gcr.io']}
    assert mirror['permissions'] == '0644'
    assert config['ssh_pwauth'] is False
    assert config['users'][0]['ssh_authorized_keys'] == [public]
    guest = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'guest_vm')
    calls = [n for n in ast.walk(guest) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Name) and n.func.id == 'guest_cloud_config' for n in calls)
    assert 'label' not in {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
    source = RUNNER.read_text()
    assert "guest_vm('baseline', baseline," in source
    assert "guest_vm('candidate', candidate," in source  # immutable commit, not a moving branch
    assert source.index('cloud-init status --wait') < source.index("guest('install',")
    assert 'docker login' not in source


@pytest.mark.parametrize('message,failures,expected_attempts,expected_code', [
    ('docker.io/library/rabbitmq:4-alpine: 429 Too Many Requests', 2, 3, 0),
    ('docker.io/library/rabbitmq:4-alpine: 429 Too Many Requests', 9, 3, 7),
    ('toomanyrequests: You have reached your unauthenticated pull rate limit', 1, 2, 0),
    ('permission denied', 9, 1, 7),
    ('GET /marketplace/all -> 404', 9, 1, 7),
    ('', 0, 1, 0),
])
def test_registry_retry_is_bounded_and_never_masks_unrelated_failures(
        tmp_path, message, failures, expected_attempts, expected_code):
    tree = ast.parse(RUNNER.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'guest_install_command')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(RUNNER), 'exec'), namespace)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    sleep = bindir / 'sleep'
    sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$WAITS"\n')
    sleep.chmod(0o755)
    install = tmp_path / 'install.sh'
    install.write_text('''#!/bin/bash
count=0
[ ! -f "$COUNT" ] || read -r count < "$COUNT"
count=$((count + 1))
printf '%s\\n' "$count" > "$COUNT"
if [ "$count" -le "$FAILURES" ]; then printf '%s\\n' "$MESSAGE"; exit 7; fi
''')
    count = tmp_path / 'count'
    waits = tmp_path / 'waits'
    # Real install is a pipeline: retain its final stage's exit, not curl's.
    command = namespace['guest_install_command'](f'printf script | bash "{install}"')
    env = {**os.environ, 'PATH': f'{bindir}:' + os.environ['PATH'], 'TMPDIR': str(tmp_path),
           'COUNT': str(count), 'WAITS': str(waits), 'MESSAGE': message, 'FAILURES': str(failures)}
    result = subprocess.run(['bash', '-se'], input='set -o pipefail\n' + command,
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == expected_code, result.stdout + result.stderr
    assert int(count.read_text()) == expected_attempts
    assert (waits.read_text().splitlines() if waits.exists() else []) == ['60', '120'][:expected_attempts - 1]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ['bin', 'install.sh', 'count'] + (['waits'] if expected_attempts > 1 else []))
