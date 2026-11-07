"""Alongside must prepare uploads through the installer's already-authorized Docker route."""
import subprocess

import pytest

from wam import alongside
from wam.backup import dockercli


@pytest.fixture
def docker_route(monkeypatch):
    # No real daemon, sudo, group or owner data is touched by these tests.
    monkeypatch.setattr(dockercli, "_cached", {})
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(key, raising=False)

    def setup(*, direct=False, sudo=True, context="default"):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            elevated = argv[0] == "sudo"
            if "context" in argv:
                return subprocess.CompletedProcess(argv, 0, context.encode(), b"")
            denied = not (sudo if elevated else direct)
            if denied:
                error = "permission denied at unix:///var/run/docker.sock"
                if kwargs.get("check"):
                    raise subprocess.CalledProcessError(1, argv, stderr=error.encode())
                return subprocess.CompletedProcess(argv, 1, "", error)
            return subprocess.CompletedProcess(argv, 0, b"daemon-id\n", "")

        monkeypatch.setattr(subprocess, "run", run)
        return calls

    return setup


def test_media_uses_existing_passwordless_sudo_without_group_membership(tmp_path, docker_route):
    calls = docker_route()
    assert alongside.adopt_media(tmp_path, "image:pin", "1001:1001") is True
    command = calls[-1]
    assert command[:6] == ["sudo", "-n", "docker", "--host", "unix:///var/run/docker.sock", "run"]
    assert command[6:13] == ["--rm", "--pull", "missing", "--network", "none", "--user", "0:0"]
    assert f"{tmp_path}/media:/media" in command
    assert "chown -R 1001:1001 /media/Inbox /media/.wizard-uploads" in command[-1]
    assert "chmod -R 777" not in command[-1]


def test_media_keeps_working_direct_docker(tmp_path, docker_route):
    calls = docker_route(direct=True)
    assert alongside.adopt_media(tmp_path, "image:pin", "1001:1001") is True
    assert calls[-1][:2] == ["docker", "run"]
    assert not any(call[0] == "sudo" for call in calls)


@pytest.mark.parametrize("setting,value", [
    ("DOCKER_CONTEXT", "owners-context"),
    ("DOCKER_HOST", "tcp://remote.invalid:2376"),
    ("DOCKER_TLS_VERIFY", "1"),
    (None, None),  # a non-default current context even without an environment override
])
def test_media_does_not_escalate_a_remote_or_custom_context(tmp_path, monkeypatch, docker_route, setting, value):
    calls = docker_route(context="owners-context" if setting is None else "default")
    if setting:
        monkeypatch.setenv(setting, value)
    with pytest.raises(SystemExit, match="could not give the Agent Service its attachment folders: permission denied"):
        alongside.adopt_media(tmp_path, "image:pin", "1001:1001")
    assert not any(call[0] == "sudo" for call in calls)


def test_media_keeps_a_working_rootless_daemon(tmp_path, monkeypatch, docker_route):
    calls = docker_route(direct=True)
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1001/docker.sock")
    assert alongside.adopt_media(tmp_path, "image:pin", "0:1000") is True
    assert calls[-1][:2] == ["docker", "run"]
    assert not any(call[0] == "sudo" for call in calls)


def test_media_still_reports_failure_when_neither_route_is_authorized(tmp_path, docker_route):
    calls = docker_route(sudo=False)
    with pytest.raises(SystemExit, match="could not give the Agent Service its attachment folders: permission denied"):
        alongside.adopt_media(tmp_path, "image:pin", "1001:1001")
    assert calls[-1][:2] == ["docker", "run"]
    assert all(call[:2] == ["sudo", "-n"] for call in calls if call[0] == "sudo")


def test_invalid_media_arguments_never_probe_docker(tmp_path, docker_route):
    calls = docker_route()
    assert alongside.adopt_media(tmp_path, "bad image;rm", "1001:1001") is False
    assert alongside.adopt_media(tmp_path, "image:pin", "1001:1001; rm") is False
    assert calls == []
