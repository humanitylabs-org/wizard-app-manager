"""Docker 20.10 supports context ls, but not context show (Debian 12)."""
import subprocess

import pytest

from wam.backup import dockercli


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(dockercli, "_cached", {})


def legacy_client(monkeypatch, *, output=b"\ndefault\n", error=None):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 20
        assert kwargs["check"] is True
        if argv == ["docker", "context", "show"]:
            raise subprocess.CalledProcessError(1, argv, stderr=b"Usage: docker context COMMAND")
        if argv == ["docker", "context", "ls", "--format", "{{if .Current}}{{.Name}}{{end}}"]:
            if error:
                raise error(argv)
            return subprocess.CompletedProcess(argv, 0, output, b"")
        if argv == ["sudo", "-n", "docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{.ID}}"]:
            return subprocess.CompletedProcess(argv, 0, b"local-daemon", b"")
        raise subprocess.CalledProcessError(1, argv, stderr=b"permission denied on docker.sock")

    monkeypatch.setattr(dockercli.subprocess, "run", run)
    return calls


def test_legacy_context_query_reaches_existing_authorized_local_sudo(monkeypatch):
    calls = legacy_client(monkeypatch)
    assert dockercli.docker({}) == ["sudo", "-n", "docker", "--host", "unix:///var/run/docker.sock"]
    assert [argv for argv, _ in calls][1] == [
        "docker", "context", "ls", "--format", "{{if .Current}}{{.Name}}{{end}}"]
    assert not any(argv[1:3] == ["context", "show"] for argv, _ in calls)


def test_legacy_current_row_can_precede_other_contexts(monkeypatch):
    legacy_client(monkeypatch, output=b"default\n\n")
    assert dockercli.docker({})[0:2] == ["sudo", "-n"]


@pytest.mark.parametrize("output", [b"\ncustom\n", b"", b"\n\n", b"default\ncustom", b"default\xff"])
def test_unknown_or_ambiguous_current_context_never_escalates(monkeypatch, output):
    calls = legacy_client(monkeypatch, output=output)
    assert dockercli.docker({}) == ["docker"]
    first = len(calls)
    assert dockercli.docker({}) == ["docker"]
    assert len(calls) > first  # unknown contexts/failures must not be cached
    assert not any(argv[0] == "sudo" for argv, _ in calls)


@pytest.mark.parametrize("error", [
    lambda argv: subprocess.CalledProcessError(1, argv),
    lambda argv: subprocess.TimeoutExpired(argv, 20),
    lambda argv: FileNotFoundError("docker unavailable"),
])
def test_context_query_error_never_assumes_default(monkeypatch, error):
    calls = legacy_client(monkeypatch, error=error)
    assert dockercli.docker({}) == ["docker"]
    assert not any(argv[0] == "sudo" for argv, _ in calls)


@pytest.mark.parametrize("host", ["tcp://remote.invalid:2376", "ssh://owner@remote.invalid", "unix:///run/other.sock"])
def test_explicit_context_and_host_conflict_is_not_escalated(monkeypatch, host):
    # Never guess a daemon when both settings are present. In Docker 20.10
    # DOCKER_HOST wins; ignoring it could authorize a different local daemon.
    # Direct success still wins first.
    calls = legacy_client(monkeypatch)
    assert dockercli.docker({"DOCKER_CONTEXT": "default", "DOCKER_HOST": host}) == ["docker"]
    assert len(calls) == 1
    assert not any(argv[0] == "sudo" for argv, _ in calls)
