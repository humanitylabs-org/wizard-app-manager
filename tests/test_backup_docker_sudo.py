import subprocess
from wam.backup import dockercli


def _fake(ok):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if tuple(argv[:len(ok)]) != tuple(ok):
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, b"x", b"")
    return run, calls


def test_plain_docker_when_socket_is_reachable(monkeypatch):
    run, _ = _fake(["docker"])
    monkeypatch.setattr(dockercli, "_cached", None)
    monkeypatch.setattr(dockercli.subprocess, "run", run)
    assert dockercli.docker() == ["docker"]


def test_sudo_docker_before_group_membership_is_active(monkeypatch):
    run, calls = _fake(["sudo", "-n", "docker"])
    monkeypatch.setattr(dockercli, "_cached", None)
    monkeypatch.setattr(dockercli.subprocess, "run", run)
    assert dockercli.docker() == ["sudo", "-n", "docker"]
    assert dockercli.docker() == ["sudo", "-n", "docker"]
    assert len(calls) == 2  # probed once, then cached


def test_neither_works_keeps_plain_docker_so_the_real_error_shows(monkeypatch):
    run, _ = _fake(["nope"])
    monkeypatch.setattr(dockercli, "_cached", None)
    monkeypatch.setattr(dockercli.subprocess, "run", run)
    assert dockercli.docker() == ["docker"]


def test_verb():
    assert dockercli.verb(["sudo", "-n", "docker", "inspect", "x"]) == "inspect"
    assert dockercli.verb(["docker", "run"]) == "run"
