"""Named owner WITHOUT passwordless sudo, after the one-time administrator approval (live demo run, 0.3.6).

The approval adds the owner to the docker group, but the owner's systemd user manager (and every transient unit
`start` launches) keeps its old groups until the next login/reboot. The detached runner then failed `docker info`,
fell into the administrator gate, printed the admin command into the journal and exited 2, while `start` still
said "started in the background" and `status` later said "not started".
"""
from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

import pytest

from test_get_sh_any_user import Box, GET_SH, _local_wam

GROUP_GETENT = r"""#!/bin/bash
if [ "$1" = group ]; then [ "${2:-docker}" = docker ] && echo "docker:x:988:${FAKE_DOCKER_MEMBERS:-}"; exit 0; fi
[ "$1" = passwd ] || exit 2
if [ -n "${2:-}" ]; then awk -F: -v u="$2" '$1==u' "$FAKE_PASSWD" | grep . ; else cat "$FAKE_PASSWD"; fi
"""
# docker only works in a process that has picked up the group (here: started through `sg docker`).
DOCKER = '#!/bin/sh\ncase "$1" in info) [ -n "$FAKE_IN_DOCKER_GROUP" ] || exit 1 ;; version) echo 29.0.0 ;; esac\nexit 0\n'
SG = ('#!/bin/bash\necho "sg $*" >> "$FAKE_LOG"\n[ "$1" = docker ] || exit 1\nshift; [ "$1" = -c ] && shift\n'
      'export FAKE_IN_DOCKER_GROUP=1\nexec /bin/bash -c "$1"\n')


def _stub(box: Box, name: str, text: str) -> None:
    (box.bin / name).write_text(text)
    (box.bin / name).chmod(0o755)


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    _stub(b, "getent", GROUP_GETENT)
    _stub(b, "docker", DOCKER)
    _stub(b, "sg", SG)
    # systemd-run really starts the unit's command (detached), like the real one (as in test_get_sh_detached).
    _stub(b, "systemd-run", "#!/bin/bash\necho \"systemd-run $*\" >> \"$FAKE_LOG\"\n"
          "while [ $# -gt 0 ] && [ \"$1\" != /bin/bash ]; do shift; done\n"
          "[ $# -gt 0 ] || exit 0\nsetsid nohup \"$@\" </dev/null >/dev/null 2>&1 &\nexit 0\n")
    return b


def _wait_final(home: Path, timeout: float = 30) -> str:
    import json
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = json.loads((home / ".wizard-os/install-status.json").read_text())
            if st.get("state") in ("done", "failed"):
                return st["state"]
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError("runner never finished")


def test_runner_picks_up_new_docker_group_instead_of_failing(box, tmp_path):
    box.add_user("dana", 1000)
    box.hermes("dana")
    home = box.users["dana"][1]
    src = _local_wam(tmp_path, 'docker info >/dev/null 2>&1 || { echo "ERROR: docker permission denied"; exit 1; }\n'
                               'echo docker-ok\n')
    p = box.run("dana", "start", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"),
                FAKE_DOCKER_MEMBERS="dana")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _wait_final(home) == "done", (home / ".wizard-os/install.log").read_text()
    assert "docker-ok" in (home / ".wizard-os/install.log").read_text()
    assert "sg docker -c" in box.log.read_text()


def test_runner_not_in_docker_group_is_not_reexeced(box, tmp_path):
    box.add_user("dana", 1000)
    box.hermes("dana")
    home = box.users["dana"][1]
    src = _local_wam(tmp_path, 'docker info >/dev/null 2>&1 || { echo "no docker"; exit 1; }\n')
    assert box.run("dana", "start", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam")).returncode == 0
    assert _wait_final(home) == "failed"
    assert "sg " not in box.log.read_text()


def test_start_reports_a_runner_that_exits_at_once(box, tmp_path):
    """The live failure: `start` hands off, but its runner exits before writing any status."""
    box.add_user("dana", 1000)
    box.hermes("dana")
    _stub(box, "sudo", '#!/bin/sh\necho "sudo $*" >> "$FAKE_LOG"\nexit 0\n')
    # The unit "starts" but its runner never comes up (exits at once, writes nothing).
    _stub(box, "systemd-run", '#!/bin/sh\necho "systemd-run $*" >> "$FAKE_LOG"\nexit 0\n')
    _stub(box, "setsid", '#!/bin/sh\nexit 0\n')
    src = _local_wam(tmp_path, "echo should-not-run\n")
    p = box.run("dana", "start", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "did not start" in p.stderr
    assert "started in the background" not in p.stdout


def test_runner_that_loses_sudo_reports_the_admin_command_instead_of_vanishing(box, tmp_path):
    """Same live failure, now handled: the runner hits the gate, but records needs_admin and the one command."""
    box.add_user("dana", 1000)
    box.hermes("dana")
    # sudo -n works for `start` itself but not once the runner's settings file exists (= inside the runner).
    _stub(box, "sudo", '#!/bin/sh\necho "sudo $*" >> "$FAKE_LOG"\n[ -f "$HOME/.wizard-os/install-status.json" ] && exit 1\nexit 0\n')
    src = _local_wam(tmp_path, "echo should-not-run\n")
    env = dict(WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"))
    p = box.run("dana", "start", **env)
    assert p.returncode == 0, p.stdout + p.stderr
    end = time.time() + 30
    out = []
    while time.time() < end:
        out = box.run("dana", "status", **env).stdout.splitlines()
        if out and out[0] == "Wizard OS install: needs_admin":
            break
        time.sleep(0.2)
    assert out[0] == "Wizard OS install: needs_admin", out
    assert "An administrator of this server must run this once" in out[1]
    assert "sudo env WIZARD_HERMES_USER=dana" in out[2]


def _named_host_ready_script(bus: Path) -> str:
    src = GET_SH.read_text()
    funcs = ""
    for name in ("docker_group_member", "named_host_ready"):
        start = src.index(f"{name}() {{")
        funcs += src[start:src.index("\n}\n", start) + 3]
    return funcs.replace('"/run/user/$(id -u)/bus"', f'"{bus}"') + "\nnamed_host_ready && echo READY || echo NOT-READY\n"


def test_named_host_ready_accepts_an_owner_whose_session_predates_the_docker_group(box, tmp_path):
    box.add_user("dana", 1000)
    home = box.users["dana"][1]
    (home / "runtipi").mkdir()
    (home / "runtipi/.env").write_text("X=1\n")
    bus = tmp_path / "bus"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(bus))
    try:
        script = _named_host_ready_script(bus)
        run = lambda **e: subprocess.run(["bash", "-c", script], env=box.env("dana", WIZARD_PRIVATE_ACCESS="none", **e),
                                         capture_output=True, text=True, timeout=30).stdout.strip()
        assert run(FAKE_DOCKER_MEMBERS="dana") == "READY"
        assert run(FAKE_DOCKER_MEMBERS="other") == "NOT-READY"
    finally:
        s.close()
