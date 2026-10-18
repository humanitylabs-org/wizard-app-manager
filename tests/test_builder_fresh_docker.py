"""Fresh-box doctor must use the installer's already-authorized local Docker route."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest
from wam import builder_bundle as bb


@pytest.mark.parametrize('context', [None, 'host', 'context', 'other-error', 'sudo-denied'])
def test_fresh_docker_probe_route(tmp_path, monkeypatch, context):
    data = tmp_path / 'app-data/wizardapps/hermes-agent/data'
    data.mkdir(parents=True)
    (data / 'config.yaml').write_text('terminal:\n  backend: local\n')
    monkeypatch.delenv('DOCKER_HOST', raising=False)
    monkeypatch.delenv('DOCKER_CONTEXT', raising=False)
    if context == 'host':
        monkeypatch.setenv('DOCKER_HOST', 'unix:///run/user/1000/docker.sock')
    if context == 'context':
        monkeypatch.setenv('DOCKER_CONTEXT', 'owner-context')
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == 'docker':
            error = 'connection refused' if context == 'other-error' else 'permission denied at unix:///var/run/docker.sock'
            raise subprocess.CalledProcessError(1, cmd, stderr=error)
        assert cmd[:3] == ['sudo', '-n', 'docker']
        assert kwargs['timeout'] <= 20
        if context == 'sudo-denied':
            raise subprocess.CalledProcessError(1, cmd, stderr='sudo: a password is required')
        return SimpleNamespace(returncode=0, stdout='engine-id\n' if 'ps' in cmd else '[]', stderr='')
    monkeypatch.setattr(bb.subprocess, 'run', run)
    result = bb.runtime_health(SimpleNamespace(runtipi_root=tmp_path))
    assert result[0][1] is (context is None)
    if context is None:
        assert calls[-1][:6] == ['sudo', '-n', 'docker', 'exec', '-u', '1000:1000']
        assert len(calls) == 3
    elif context in ('host', 'context', 'other-error'):
        assert len(calls) == 1
    else:
        assert len(calls) == 2
