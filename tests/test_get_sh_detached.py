"""`get.sh start` / `get.sh status`: the detached install an agent runs (no terminal, a command time limit).

The real get.sh runs on the fake box from test_get_sh_any_user (fake passwd, PATH stubs). install.sh is a local stub
repo that prints what the real one would (steps, Tailscale owner-action markers, the restart notice) and can sleep or
fail. Nothing on the host is touched; the detached runner is a real background process in the test's temp dir.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from test_get_sh_any_user import GW, RS, Box, _local_wam

LINK = "https://login.tailscale.com/a/1a2b3c"
WIZ = "https://box.tail1.ts.net:8443/"


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    # systemd-run really starts the unit's command (detached), like the real one.
    (b.bin / "systemd-run").write_text(
        "#!/bin/bash\necho \"systemd-run $*\" >> \"$FAKE_LOG\"\n"
        "while [ $# -gt 0 ] && [ \"$1\" != /bin/bash ]; do shift; done\n"
        "[ $# -gt 0 ] || exit 0\nsetsid nohup \"$@\" </dev/null >/dev/null 2>&1 &\nexit 0\n")
    return b


def _wam(tmp: Path, body: str) -> dict:
    src = _local_wam(tmp, body)
    return {"WAM_REPO": str(src), "WAM_REF": "main", "WAM_DIR": str(tmp / "wam")}


def _status_json(home: Path) -> dict:
    return json.loads((home / ".wizard-os/install-status.json").read_text())


def _wait(home: Path, states: set[str], timeout: float = 30) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = _status_json(home)
            if st.get("state") in states:
                return st
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"status never reached {states}: {(home / '.wizard-os/install-status.json').read_text()}")


def _wait_runner_gone(box: Box, user: str, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        p = box.run(user, "status")
        if "stopped" not in p.stdout and "running" not in p.stdout.splitlines()[0]:
            return
        time.sleep(0.2)


GOOD = (
    "echo '==> Installing the App Manager'\n"
    f"echo '[wizard-os] owner-action: Open this link to connect your Wizard to your Tailscale: {LINK}'\n"
    "while [ ! -f \"$HOME/.signed-in\" ]; do sleep 0.2; done\n"
    "echo '[wizard-os] owner-action-done'\n"
    "echo '==> Default apps'\n"
    f"mkdir -p \"$HOME/.wizard-os\"; printf '{{\"links\":[{{\"url\":\"{WIZ}\"}}]}}' > \"$HOME/.wizard-os/links.json\"\n"
)


def test_start_returns_at_once_and_status_walks_running_owner_action_done(box, tmp_path):
    box.add_user("alice", 1001)
    box.hermes("alice")
    home = box.users["alice"][1]
    t0 = time.time()
    p = box.run("alice", "start", **_wam(tmp_path, GOOD))
    assert p.returncode == 0, p.stdout + p.stderr
    assert time.time() - t0 < 15, "start must hand off and return, not run the install"
    assert "started in the background" in p.stdout
    assert "curl -fsSL https://aiwizards.com/os.sh | bash -s status" in p.stdout.splitlines()

    st = _wait(home, {"needs_owner"})
    assert st["tailscale_login_url"] == LINK
    s = box.run("alice", "status").stdout.splitlines()
    assert s[0] == "Wizard OS install: needs_owner"
    assert "Open this link to connect your Wizard to your Tailscale" in s[2] and s[3] == LINK

    (home / ".signed-in").touch()
    st = _wait(home, {"done", "failed"})
    assert st["state"] == "done", (home / ".wizard-os/install.log").read_text()
    assert st["tailscale_login_url"] is None and st["wizard_url"] == WIZ and st["exit_code"] == 0
    s = box.run("alice", "status").stdout.splitlines()
    assert s[0] == "Wizard OS install: done" and s[-1] == WIZ
    log = (home / ".wizard-os/install.log").read_text()
    assert "Installing the App Manager" in log and "\x1b[" not in log
    assert not (home / ".wizard-os/install-env").exists(), "the runner's settings file is deleted once read"


def test_second_start_while_running_does_not_start_another(box, tmp_path):
    box.add_user("alice", 1001)
    box.hermes("alice")
    home = box.users["alice"][1]
    body = "echo \"run $$\" >> \"$HOME/runs\"\nwhile [ ! -f \"$HOME/.go\" ]; do sleep 0.2; done\n"
    env = _wam(tmp_path, body)
    assert box.run("alice", "start", **env).returncode == 0
    _wait(home, {"running"})
    for _ in range(50):
        if (home / "runs").exists():
            break
        time.sleep(0.2)
    p = box.run("alice", "start", **env)
    assert p.returncode == 0 and "already running; not starting a second one" in p.stdout
    (home / ".go").touch()
    _wait(home, {"done"})
    assert len((home / "runs").read_text().splitlines()) == 1


def test_failed_install_status_shows_the_last_20_log_lines(box, tmp_path):
    box.add_user("alice", 1001)
    box.hermes("alice")
    home = box.users["alice"][1]
    body = "for i in $(seq 1 40); do echo \"line $i\"; done\necho 'boom: docker is not running' >&2\nexit 7\n"
    assert box.run("alice", "start", **_wam(tmp_path, body)).returncode == 0
    st = _wait(home, {"done", "failed"})
    assert st["state"] == "failed" and st["exit_code"] == 7
    out = box.run("alice", "status").stdout.splitlines()
    assert out[0] == "Wizard OS install: failed"
    i = next(n for n, l in enumerate(out) if l.startswith("Last 20 lines"))
    tail = out[i + 1:]
    assert len(tail) == 20 and tail[-1] == "boom: docker is not running" and tail[0] == "line 22" and "line 21" not in tail


def test_status_before_any_start_says_how_to_start(box):
    box.add_user("alice", 1001)
    out = box.run("alice", "status").stdout
    assert "not started" in out and "curl -fsSL https://aiwizards.com/os.sh | bash -s start" in out


def test_status_spots_a_runner_that_died(box, tmp_path):
    box.add_user("alice", 1001)
    d = box.users["alice"][1] / ".wizard-os"; d.mkdir(parents=True)
    (d / "install-status.json").write_text(json.dumps({"state": "running", "step": "Default apps"}))
    (d / "install.log").write_text("last words\n")
    out = box.run("alice", "status").stdout
    assert out.startswith("Wizard OS install: stopped") and "last words" in out


def test_detached_agent_install_restarts_hermes_only_after_done_is_written(box, tmp_path):
    """An older install.sh under the agent asks for a synchronous restart; the detached run defers it, and the
    scheduled restart waits for the runner (which exits only after the final status is written)."""
    box.add_user("alice", 1001)
    box.hermes("alice")
    home = box.users["alice"][1]
    body = f"hermes {GW} {RS}\necho install-finished >> \"$FAKE_LOG\"\n"
    assert box.run("alice", "start", _HERMES_GATEWAY="1", **_wam(tmp_path, body)).returncode == 0
    st = _wait(home, {"done", "failed"})
    assert st["state"] == "done", (home / ".wizard-os/install.log").read_text()
    log = box.log.read_text().splitlines()
    assert f"real-hermes {GW} {RS}" not in log, "Hermes was restarted inside the install"
    sched = [l for l in log if l.startswith("systemd-run ") and f"{GW} {RS}" in l]
    assert sched, log
    runner = st["pid"]
    assert f"[ -d /proc/{runner} ] || break" in sched[0], "the restart must wait for the runner to exit"
    assert st["restart_pending"] is True and "Hermes restarts once" in st["message"]


def test_start_never_prompts_and_forwards_settings_without_printing_them(box, tmp_path):
    box.add_user("alice", 1001)
    box.hermes("alice")
    home = box.users["alice"][1]
    body = "echo \"seen=$GITHUB_TOKEN mode=$WIZARD_MODE detached=$WIZARD_DETACHED\" > \"$HOME/seen\"\n"
    p = box.run("alice", "start", GITHUB_TOKEN="ghp_secret'quote", **_wam(tmp_path, body))
    assert "ghp_secret" not in p.stdout + p.stderr
    _wait(home, {"done"})
    assert (home / "seen").read_text().strip() == "seen=ghp_secret'quote mode=alongside detached=1"
    assert "ghp_secret" not in (home / ".wizard-os/install.log").read_text()
    assert "ghp_secret" not in (home / ".wizard-os/install-status.json").read_text()


def test_root_start_uses_a_system_scope_transient_unit(box, tmp_path):
    box.hermes("root", where="venv")
    home = box.users["root"][1]
    assert box.run("root", "start", **_wam(tmp_path, "true\n")).returncode == 0
    _wait(home, {"done", "failed"})
    unit = next(l for l in box.log.read_text().splitlines() if "__runner" in l)
    assert unit.startswith("systemd-run --quiet --collect --unit wizard-os-install-") and "--user" not in unit


def test_plain_pipe_install_still_runs_in_the_foreground(box, tmp_path):
    box.add_user("alice", 1001)
    box.hermes("alice")
    p = box.run("alice", **_wam(tmp_path, "echo foreground-install\n"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "foreground-install" in p.stdout
    assert not (box.users["alice"][1] / ".wizard-os/install-status.json").exists()
