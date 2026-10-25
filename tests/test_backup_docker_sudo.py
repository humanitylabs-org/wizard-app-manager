import json
import os
import subprocess

import pytest

from wam.backup import dockercli, filesystem
from wam.backup.appdata import AppData
from wam.backup.box import Box, HermesRunner


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    monkeypatch.setattr(dockercli, "_cached", {} if hasattr(dockercli, "_lock") else None)


def fake(monkeypatch, *, plain=False, sudo=True, context="default"):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        assert kw["stdin"] == subprocess.DEVNULL
        assert kw["timeout"] == 20
        if argv[1:3] == ["context", "show"]:
            return subprocess.CompletedProcess(argv, 0, context.encode(), b"")
        if (argv[0] == "docker" and plain) or (argv[0] == "sudo" and sudo):
            return subprocess.CompletedProcess(argv, 0, b"daemon", b"")
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(dockercli.subprocess, "run", run)
    return calls


def test_plain_docker_when_socket_is_reachable(monkeypatch):
    calls = fake(monkeypatch, plain=True)
    assert dockercli.docker({}) == ["docker"]
    assert len(calls) == 1


def test_sudo_docker_before_group_membership_is_active(monkeypatch):
    calls = fake(monkeypatch)
    expected = ["sudo", "-n", "docker", "--host", "unix:///var/run/docker.sock"]
    assert dockercli.docker({}) == expected
    result = dockercli.docker({})
    result.append("mutation")
    assert dockercli.docker({}) == expected
    assert len(calls) == 3  # plain info, local context, sudo info; then cached


def test_probe_uses_sanitized_box_environment(monkeypatch):
    calls = fake(monkeypatch)
    env = {"PATH": "/box/bin", "DOCKER_HOST": "unix:///run/example.sock",
           "WIZARD_BACKUP_KEY": "private", "WIZARD_BACKUP_PASSPHRASE": "private",
           "WIZARD_S3_SECRET_ACCESS_KEY": "private"}
    prefix = dockercli.docker(env)
    assert prefix == ["sudo", "-n", "docker", "--host", env["DOCKER_HOST"]]
    for _, kw in calls:
        assert kw["env"] == {"PATH": "/box/bin", "DOCKER_HOST": env["DOCKER_HOST"]}
    assert env["WIZARD_BACKUP_KEY"] == "private"


@pytest.mark.parametrize("extra", [
    {"DOCKER_HOST": "tcp://remote.example:2375"},
    {"DOCKER_HOST": "ssh://owner@remote.example"},
    {"DOCKER_CONTEXT": "private"},
    {"DOCKER_TLS_VERIFY": "1"},
    {"DOCKER_CERT_PATH": "/certs"},
])
def test_never_sudo_a_remote_or_custom_context(monkeypatch, extra):
    calls = fake(monkeypatch)
    assert dockercli.docker(extra) == ["docker"]
    assert not any(argv[0] == "sudo" for argv, _ in calls)


def test_explicit_default_context_takes_precedence_over_docker_host(monkeypatch):
    fake(monkeypatch)
    assert dockercli.docker({"DOCKER_CONTEXT": "default", "DOCKER_HOST": "unix:///run/ignored.sock"})[-1] == "unix:///var/run/docker.sock"


def test_configured_nondefault_context_does_not_switch_daemons(monkeypatch):
    calls = fake(monkeypatch, context="other")
    assert dockercli.docker({}) == ["docker"]
    assert not any(argv[0] == "sudo" for argv, _ in calls)


def test_neither_works_is_not_cached(monkeypatch):
    calls = fake(monkeypatch, sudo=False)
    assert dockercli.docker({}) == ["docker"]
    before = len(calls)
    assert dockercli.docker({}) == ["docker"]
    assert len(calls) > before


def test_cache_is_environment_scoped_and_expires(monkeypatch):
    calls = fake(monkeypatch)
    clock = [10.0]
    monkeypatch.setattr(dockercli.time, "monotonic", lambda: clock[0])
    a = {"DOCKER_HOST": "unix:///run/a.sock", "HOME": "/owner-a"}
    b = {"DOCKER_HOST": "unix:///run/b.sock", "HOME": "/owner-b"}
    assert dockercli.docker(a)[-1] == a["DOCKER_HOST"]
    assert dockercli.docker(b)[-1] == b["DOCKER_HOST"]
    assert len(calls) == 4
    clock[0] += 31
    dockercli.docker(a)
    assert len(calls) == 6


def test_verb():
    assert dockercli.verb(["sudo", "-n", "docker", "--host", "unix:///run/a.sock", "inspect", "x"]) == "inspect"
    assert dockercli.verb(["sudo", "-n", "docker", "inspect", "x"]) == "inspect"
    assert dockercli.verb(["docker", "run"]) == "run"


def test_real_subprocess_fallback_streams_and_filters_secrets(tmp_path, monkeypatch):
    """No daemon/VM: real exec paths simulate socket denial and noninteractive sudo.

    Exercise AppData's streamed helper, the long-lived root filesystem helper,
    and HermesRunner, not just a mocked selector. The sudo double intentionally
    drops DOCKER_HOST like sudo env_reset; --host must carry the exact socket.
    """
    import shutil
    import sys
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the exact root filesystem helper")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "commands.jsonl"
    docker = bindir / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ['PROBE_LOG'], 'a') as f:
    f.write(json.dumps({'argv':args, 'sudo':os.environ.get('VIA_SUDO'),
                        'secrets':[k for k in os.environ if k.startswith('WIZARD_S3_') or k in ('WIZARD_BACKUP_KEY','WIZARD_BACKUP_PASSPHRASE')]})+'\\n')
if not os.environ.get('VIA_SUDO'):
    print('permission denied on docker.sock', file=sys.stderr); sys.exit(1)
assert args[:2] == ['--host', 'unix:///run/backup-probe.sock']
args = args[2:]
if args[0] == 'info': print('fake-daemon'); sys.exit(0)
if args[0] == 'inspect': print('example/helper:test'); sys.exit(0)
if '--entrypoint' in args:
    i = args.index('--entrypoint'); entry = args[i+1]
    if entry == 'node': os.execv(os.environ['PROBE_NODE'], [os.environ['PROBE_NODE'], *args[i+3:]])
    if entry == 'sh': os.execv('/bin/sh', ['/bin/sh', *args[i+3:]])
sys.stdout.buffer.write(sys.stdin.buffer.read())
''')
    sudo = bindir / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + '''import os, sys
assert sys.argv[1:3] == ['-n','docker']
os.environ.pop('DOCKER_HOST', None)
os.environ['VIA_SUDO']='1'
os.execv(os.path.join(os.path.dirname(__file__), 'docker'), sys.argv[2:])
''')
    docker.chmod(0o755)
    sudo.chmod(0o755)
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
           "DOCKER_HOST": "unix:///run/backup-probe.sock", "PROBE_LOG": str(log), "PROBE_NODE": node,
           "WIZARD_BACKUP_KEY": "private", "WIZARD_BACKUP_PASSPHRASE": "private",
           "WIZARD_S3_SECRET_ACCESS_KEY": "private"}
    home, rt = tmp_path / "wizard", tmp_path / "runtipi"
    home.mkdir(); rt.mkdir()
    box = Box(home, rt, env=env)
    data = AppData(box)
    assert data.image() == "example/helper:test"
    out = tmp_path / "streamed"
    data._helper([], {}, f'cat > "{out}"', lambda stream: stream.write(b"owner bytes"))
    assert out.read_bytes() == b"owner bytes"
    root = rt / "app-data"
    root.mkdir()
    (root / "marker").write_text("preserved")
    access = filesystem.Access(box)
    try:
        assert "marker" in access.call("listdir", root)
    finally:
        access.close()
    assert HermesRunner(box)._run([*dockercli.docker(box.child_env), "echo"], stdin="hermes bytes") == "hermes bytes"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0]["sudo"] is None  # real denied unprivileged process
    assert all(not row["secrets"] for row in rows)
    assert all(row["sudo"] == "1" for row in rows[1:])
    assert any("node" in row["argv"] for row in rows)
    assert any("sh" in row["argv"] for row in rows)
