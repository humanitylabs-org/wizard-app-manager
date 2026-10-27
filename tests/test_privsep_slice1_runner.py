"""Validate the VM recipe without executing the side-effecting acceptance runner."""
import ast
import json
from pathlib import Path

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
