"""get.sh finds an existing Hermes whoever owns it (root, any named user, a sudo user), installs alongside as that
user, stops on several installs, keeps fresh boxes fresh, has a read-only --check, and never restarts the Hermes
gateway in the middle of an install a Hermes agent started.

The real get.sh runs against a fake box: a fake passwd (getent), fake homes, fake systemd unit dir and /proc, and
PATH stubs for id/ps/sudo/apt-get/docker/systemd-run. `sudo -iu <user> ...` (the hand-off to the owner) is
recorded, not run. Nothing on the host is touched.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GET_SH = REPO / "get.sh"
INSTALL_SH = REPO / "install.sh"
GW, RS = "gateway", "restart"  # Only stub executables are used by these sandbox tests.

STUBS = {
    # id [-u|-un|-g|-nG] [user]
    "id": r"""#!/bin/bash
flag=""; u=""
for a in "$@"; do case "$a" in -*) flag="$flag$a" ;; *) u="$a" ;; esac; done
[ -n "$u" ] || u="$FAKE_USER"
line="$(awk -F: -v u="$u" '$1==u' "$FAKE_PASSWD")"
[ -n "$line" ] || { echo "id: '$u': no such user" >&2; exit 1; }
uid="$(echo "$line" | cut -d: -f3)"
case "$flag" in
  -u) echo "$uid" ;; -un|-nu) echo "$u" ;; -g) echo "$uid" ;; -nG|-Gn) echo "$u ${FAKE_GROUPS:-}" ;; *) echo "uid=$uid($u)" ;;
esac
""",
    "getent": r"""#!/bin/bash
[ "$1" = passwd ] || exit 2
if [ -n "${2:-}" ]; then awk -F: -v u="$2" '$1==u' "$FAKE_PASSWD" | grep . ; else cat "$FAKE_PASSWD"; fi
""",
    "ps": "#!/bin/sh\ncat \"$FAKE_PS\" 2>/dev/null\n",
    "sudo": "#!/bin/sh\necho \"sudo $*\" >> \"$FAKE_LOG\"\nexit 0\n",
    "runuser": "#!/bin/sh\necho 'Hermes Agent v9.9 (stub)'\n",
    "stat": "#!/bin/sh\necho \"${FAKE_STAT_OWNER:-root}\"\n",
    "docker": "#!/bin/sh\ncase \"$1\" in version) echo 29.0.0 ;; esac\nexit 0\n",
    "systemd-run": "#!/bin/sh\necho \"systemd-run $*\" >> \"$FAKE_LOG\"\nexit 0\n",
}
STUBS["useradd"] = ("#!/bin/sh\necho \"useradd $*\" >> \"$FAKE_LOG\"\nfor u; do :; done\n"
                    "mkdir -p \"$(dirname \"$FAKE_PASSWD\")/home/$u\"\n"
                    "echo \"$u:x:1999:1999::$(dirname \"$FAKE_PASSWD\")/home/$u:/bin/bash\" >> \"$FAKE_PASSWD\"\n")
for name in ("apt-get", "loginctl", "systemctl", "usermod", "install", "chown", "curl"):
    STUBS[name] = f"#!/bin/sh\necho \"{name} $*\" >> \"$FAKE_LOG\"\nexit 0\n"

# The separated host helper is an explicitly recorded root phase, not an actual
# useradd/Docker install on the unit-test host.
STUBS['curl'] = r"""#!/bin/bash
echo "curl $*" >> "$FAKE_LOG"
out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; -*) ;; *) url="$1";; esac; shift; done
if [[ "$url" == */scripts/prepare-alongside.py ]] && [ -n "$out" ]; then
  printf '%s\n' 'import os,sys' 'with open(os.environ["FAKE_LOG"],"a") as f: f.write("SEPARATED owner="+sys.argv[1]+" home="+sys.argv[2]+" WIZARD_AGENT_SAFE="+os.environ.get("WIZARD_AGENT_SAFE","")+"\n")' > "$out"
fi
if [[ "$url" == */scripts/protected-agent-storage.py ]] && [ -n "$out" ]; then
  printf '%s\n' 'import os,sys' 'with open(os.environ["FAKE_LOG"],"a") as f: f.write("STORAGE_PREFLIGHT "+" ".join(sys.argv[1:])+"\n")' 'sys.exit(0 if os.environ.get("FAKE_STORAGE_READY","1")=="1" else 2)' > "$out"
fi
"""

STUBS['mktemp'] = r"""#!/bin/bash
if [ "${1:-}" = -d ] && [[ "${2:-}" == /run/wizard-* ]]; then
  echo "root-helper-safe-directory $*" >> "$FAKE_LOG"
  exec /usr/bin/mktemp -d "$(dirname "$FAKE_LOG")/root-helper.XXXXXXXX"
fi
exec /usr/bin/mktemp "$@"
"""

class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"; self.bin.mkdir()
        for name, text in STUBS.items():
            (self.bin / name).write_text(text); (self.bin / name).chmod(0o755)
        self.units = tmp / "etc-systemd"; self.units.mkdir()
        self.sudoers = tmp / "sudoers.d"; self.sudoers.mkdir()
        self.proc = tmp / "proc"; self.proc.mkdir()
        self.log = tmp / "log"; self.log.write_text("")
        self.ps = tmp / "ps"; self.ps.write_text("")
        self.users: dict[str, tuple[int, Path]] = {}
        self.add_user("root", 0)

    def add_user(self, name: str, uid: int) -> Path:
        home = self.tmp / "home" / name
        home.mkdir(parents=True, exist_ok=True)
        self.users[name] = (uid, home)
        (self.tmp / "passwd").write_text("".join(f"{n}:x:{u}:{u}::{h}:/bin/bash\n" for n, (u, h) in self.users.items()))
        return home

    def hermes(self, user: str, *, where: str = "local", home_dir: Path | None = None) -> Path:
        """A Hermes install for `user`: config.yaml plus a `hermes` command at a stock location."""
        h = self.users[user][1]
        hh = home_dir or h / ".hermes"
        hh.mkdir(parents=True, exist_ok=True)
        (hh / "config.yaml").write_text("model: x\n")
        exe = {"local": h / ".local/bin/hermes", "venv": hh / "hermes-agent/venv/bin/hermes",
               "launcher": hh / "hermes-agent/.hermes/bin/hermes"}[where]
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(f"#!/bin/sh\necho \"real-hermes $*\" >> \"{self.log}\"\n"); exe.chmod(0o755)
        return hh

    def wos(self, user: str, mode: str) -> None:
        d = self.users[user][1] / ".wizard-os"; d.mkdir(parents=True, exist_ok=True)
        (d / "apps.json").write_text("{}")
        if mode == "alongside":
            (d / "alongside.json").write_text("{}")

    def gateway_process(self, user: str, pid: int, hermes_home: str = "", container: bool = False) -> None:
        d = self.proc / str(pid); d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join([b"/usr/bin/python3", b"/opt/hermes/bin/hermes", GW.encode(), b"run"]) + b"\0")
        (d / "environ").write_bytes((f"HERMES_HOME={hermes_home}\0" if hermes_home else "").encode())
        (d / "cgroup").write_text("0::/system.slice/docker-abc.scope\n" if container else "0::/system.slice/x.service\n")
        (d / "stat").write_text(f"{pid} (python3) S 1 0 0\n")
        with self.ps.open("a") as f:
            f.write(f"{user} {pid} python3\n")

    def env(self, user: str, **extra) -> dict:
        uid, home = self.users[user]
        e = {"PATH": f"{self.bin}:{home}/.local/bin:/usr/bin:/bin", "HOME": str(home), "USER": user, "LANG": "C.UTF-8",
             "FAKE_USER": user, "FAKE_PASSWD": str(self.tmp / "passwd"), "FAKE_PS": str(self.ps),
             "FAKE_LOG": str(self.log), "WIZARD_SYSTEMD_DIR": str(self.units), "WIZARD_SUDOERS_DIR": str(self.sudoers),
             "WIZARD_PROC_ROOT": str(self.proc), "WIZARD_RUN_ROOT": str(self.tmp / "run-user"),
             "WIZARD_BUS_WAIT": "0", "WIZARD_PRIVATE_ACCESS": "none"}
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def run(self, user: str, *args: str, script: Path = GET_SH, **extra) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(script), *args], env=self.env(user, **extra), capture_output=True,
                              text=True, timeout=60, cwd=self.tmp)

    def separated(self) -> str:
        return next((l for l in self.log.read_text().splitlines() if l.startswith('SEPARATED ')), '')

    def handoff(self) -> str:
        """The `sudo -iu <user> env ...` line get.sh handed the install to (empty if none)."""
        return next((l for l in self.log.read_text().splitlines() if l.startswith("sudo -iu ")), "")


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _env_of(line: str) -> dict:
    return dict(w.split("=", 1) for w in line.split() if "=" in w and w.split("=", 1)[0].isupper())


# ------------------------------------------------------------------ detection, as root

def test_root_owned_hermes_installs_alongside_as_root_not_a_new_wizard_user(box, tmp_path):
    """The common VPS: Hermes installed as root (/root/.hermes). Old get.sh made a `wizard` user and a FRESH install."""
    box.hermes("root", where="venv")
    src = _local_wam(tmp_path)
    p = box.run("root", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "installing Wizard OS alongside it as root" in p.stdout
    assert not box.handoff(), "root's Hermes must not be handed to another user"
    assert "useradd" not in box.log.read_text()
    seen = _env_of((tmp_path / "install-env").read_text())
    assert seen["WIZARD_MODE"] == "alongside" and seen["ME"] == "root"
    assert seen["HERMES_HOME"] == str(box.users["root"][1] / ".hermes")
    assert seen["WIZARD_HERMES_BIN"].endswith("hermes-agent/venv/bin/hermes")   # found off PATH, passed on


def test_custom_named_user_hermes_is_installed_alongside_as_that_user(box):
    """Hermes owned by `alice` (not the sudo user, not `wizard`): root hands the install to alice, alongside."""
    box.add_user("alice", 1001)
    box.hermes("alice")
    p = box.run("root")
    assert p.returncode == 0, p.stdout + p.stderr
    line = box.separated()
    assert line.startswith("SEPARATED owner=alice "), line
    assert "home=" + str(box.users["alice"][1] / ".hermes") in line
    assert not box.handoff(), "new installs must never execute privileged code from the agent home"
    assert "useradd" not in box.log.read_text()
    assert (box.sudoers / "90-wizard-alice").exists() is False   # stub sudo said alice already has sudo


def test_sudo_user_hermes_still_installs_alongside_as_the_sudo_user(box):
    box.add_user("bob", 1000)
    box.hermes("bob")
    p = box.run("root", SUDO_USER="bob")
    assert p.returncode == 0, p.stdout + p.stderr
    assert box.separated().startswith("SEPARATED owner=bob "), box.log.read_text()
    assert not box.handoff()


def test_system_unit_with_custom_hermes_home_is_found(box, tmp_path):
    """`sudo hermes gateway install --system --run-as-user carol` with a custom HERMES_HOME outside ~/.hermes."""
    box.add_user("carol", 1002)
    hh = box.hermes("carol", home_dir=tmp_path / "srv" / "hermes", where="launcher")
    (box.units / f"hermes-{GW}.service").write_text(
        f"[Service]\nUser=carol\nEnvironment=\"HERMES_HOME={hh}\"\nExecStart=/x/hermes {GW} run\n")
    p = box.run("root", WIZARD_CHECK="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Hermes owner:      carol" in p.stdout and f"Hermes home:       {hh}" in p.stdout
    assert f"systemd-system:hermes-{GW}.service" in p.stdout


def test_running_gateway_process_owner_is_found(box, tmp_path):
    box.add_user("dave", 1003)
    hh = box.hermes("dave", home_dir=tmp_path / "data" / "dave-hermes")
    box.gateway_process("dave", 4242, str(hh))
    box.gateway_process("root", 4343, "/in/a/container", container=True)   # a containerised Hermes is not "this box's"
    p = box.run("root", WIZARD_CHECK="1")
    assert "Hermes owner:      dave" in p.stdout and "process:4242" in p.stdout, p.stdout
    assert "4343" not in p.stdout


def test_several_hermes_installs_stop_with_a_clear_message_and_a_picker(box):
    for name, uid in (("alice", 1001), ("bob", 1002)):
        box.add_user(name, uid); box.hermes(name)
    p = box.run("root")
    assert p.returncode == 2
    assert "more than one Hermes" in p.stderr and "WIZARD_HERMES_USER=<user>" in p.stderr
    assert "user alice" in p.stderr and "user bob" in p.stderr
    assert not box.handoff() and "apt-get" not in box.log.read_text(), "nothing may change before the stop"
    p = box.run("root", WIZARD_HERMES_USER="bob")
    assert p.returncode == 0 and box.separated().startswith("SEPARATED owner=bob "), p.stdout + p.stderr


def test_profiles_of_one_hermes_are_one_install(box):
    hh = box.hermes("root")
    (hh / "profiles" / "work").mkdir(parents=True)
    (hh / "profiles" / "work" / "config.yaml").write_text("x: 1\n")
    box.gateway_process("root", 5000, str(hh / "profiles" / "work"))
    p = box.run("root", WIZARD_CHECK="1")
    assert "SEVERAL" not in p.stdout and "Hermes owner:      root" in p.stdout, p.stdout


def test_no_hermes_root_still_makes_the_wizard_user_fresh(box):
    p = box.run("root")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "useradd -m -s /bin/bash wizard" in box.log.read_text()
    line = box.handoff()
    assert line.startswith("sudo -iu wizard ") and _env_of(line)["WIZARD_MODE"] == "fresh"
    assert not (box.sudoers / "90-wizard-wizard").exists()
    assert 'scripts/prepare-fresh-user.sh' in box.log.read_text()


@pytest.mark.parametrize("record", ["none", "alongside", "failed"])
def test_running_runtipi_is_not_a_fresh_receipt_for_named_owner(box, record):
    box.add_user("alice", 1001)
    box.hermes("alice")
    if record == "alongside":
        box.wos("alice", "alongside")
    elif record == "failed":
        state = box.users["alice"][1] / ".wizard-os"
        state.mkdir()
        (state / "release.json").write_text('{"mode":"alongside","status":"install-failed"}')
    (box.bin / "docker").write_text('#!/bin/sh\ncase "$1" in version) echo 29.9.1;; ps) echo runtipi;; esac\nexit 0\n')
    checked = box.run("root", "--check")
    assert "would install:     alongside as alice" in checked.stdout, checked.stdout
    result = box.run("root")
    assert result.returncode == 0, result.stdout + result.stderr
    if record == "none":
        assert box.separated().startswith("SEPARATED owner=alice "), box.log.read_text()
        assert not box.handoff()
    else:
        assert box.handoff().startswith("sudo -iu alice "), box.log.read_text()
        assert _env_of(box.handoff())["WIZARD_MODE"] == "alongside"
    assert "useradd" not in box.log.read_text()
    assert list(box.sudoers.iterdir()) == []


def test_fresh_box_with_a_root_hermes_stays_fresh_on_rerun(box):
    box.hermes("root")
    box.add_user("wizard", 1000)
    box.wos("wizard", "fresh")
    p = box.run("root")
    assert p.returncode == 0, p.stdout + p.stderr
    assert box.handoff().startswith("sudo -iu wizard ") and _env_of(box.handoff())["WIZARD_MODE"] == "fresh"


def test_rerun_after_a_failed_alongside_install_stays_alongside(box, tmp_path):
    """Demo, 0.3.0: the first alongside run failed after Runtipi came up but before `wam alongside` recorded the
    mode. The re-run saw a running runtipi and planned a fresh install as a new wizard user."""
    box.hermes("root", where="launcher")
    d = box.users["root"][1] / ".wizard-os"; d.mkdir(parents=True, exist_ok=True)
    (d / "release.json").write_text('{"release": "0.3.0", "mode": "alongside", "status": "install-failed"}')
    (box.bin / "docker").write_text('#!/bin/sh\ncase "$1" in version) echo 29.0.0 ;; ps) echo runtipi ;; esac\nexit 0\n')
    p = box.run("root", WIZARD_CHECK="1")
    assert "would install:     alongside as root" in p.stdout, p.stdout
    src = _local_wam(tmp_path)
    p = box.run("root", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "useradd" not in box.log.read_text() and not box.handoff()
    assert _env_of((tmp_path / "install-env").read_text())["WIZARD_MODE"] == "alongside"


def test_rerun_with_two_hermes_requires_explicit_selection(box):
    for name, uid in (("alice", 1001), ("bob", 1002)):
        box.add_user(name, uid); box.hermes(name)
    box.wos("bob", "alongside")
    p = box.run("root")
    assert p.returncode == 2, p.stdout + p.stderr
    assert not box.handoff()
    p = box.run("root", WIZARD_HERMES_USER="alice")
    assert p.returncode == 0, p.stdout + p.stderr
    assert box.separated().startswith("SEPARATED owner=alice ")


def test_wizard_mode_fresh_is_respected_as_root(box):
    box.hermes("root")
    box.run("root", WIZARD_MODE="fresh")
    assert box.handoff().startswith("sudo -iu wizard ") and _env_of(box.handoff())["WIZARD_MODE"] == "fresh"


# ------------------------------------------------------------------ as a non-root user

def test_user_whose_hermes_belongs_to_root_is_told_instead_of_a_second_agent(box):
    box.hermes("root")
    box.add_user("erin", 1004)
    p = box.run("erin")
    assert p.returncode == 2
    assert "belongs to root" in p.stderr and "sudo bash" in p.stderr
    assert not box.handoff()


def test_check_mode_changes_nothing_and_says_what_it_would_do(box):
    box.add_user("alice", 1001)
    box.hermes("alice")
    before = sorted(str(p) for p in box.tmp.rglob("*"))
    for args, extra in (((), {"WIZARD_CHECK": "1"}), (("--check",), {})):
        p = box.run("root", *args, **extra)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "would install:     alongside as alice" in p.stdout
        assert "Nothing was changed" in p.stdout
    log = box.log.read_text()
    assert not any(w in log for w in ("apt-get", "useradd", "usermod", "loginctl", "sudo -iu", "systemd-run"))
    after = sorted(str(p) for p in box.tmp.rglob("*") if p != box.log)
    assert [x for x in after if x not in before] == []


def test_check_mode_reports_none_and_the_fresh_plan(box):
    p = box.run("root", WIZARD_CHECK="1")
    assert "Hermes found:      none" in p.stdout and "fresh, as the wizard user" in p.stdout


# ------------------------------------------------------------------ agent-safe

def _local_wam(tmp: Path, install_body: str | None = None) -> Path:
    """A local App Manager repo whose install.sh records its environment (and optionally does more)."""
    src = tmp / "wam-src"; src.mkdir()
    body = install_body or ""
    (src / "install.sh").write_text(
        "#!/usr/bin/env bash\nset -e\n"
        f"echo \"ME=$(id -un) WIZARD_MODE=$WIZARD_MODE HERMES_HOME=${{HERMES_HOME:-}} WIZARD_HERMES_BIN=${{WIZARD_HERMES_BIN:-}} "
        f"WIZARD_AGENT_SAFE=${{WIZARD_AGENT_SAFE:-}}\" > {tmp}/install-env\n" + body)
    g = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=src, env=g, check=True)
    return src


@pytest.mark.parametrize("marker", [{"_HERMES_GATEWAY": "1"}, {"HERMES_SESSION_PLATFORM": "telegram"},
                                    {"WIZARD_AGENT_SAFE": "1"}])
def test_agent_detection_is_passed_to_the_install(box, tmp_path, marker):
    box.add_user("alice", 1001)
    box.hermes("alice")
    box.wos("alice", "alongside")
    box.run("root", **marker)
    assert _env_of(box.handoff())["WIZARD_AGENT_SAFE"] == "1"
    p = box.run("root", WIZARD_CHECK="1", **marker)
    assert "run by an agent:   yes" in p.stdout


def test_agent_detection_from_the_parent_process_tree(box, tmp_path):
    """No env markers: an ancestor process is `hermes gateway run`."""
    src = GET_SH.read_text()
    funcs = src[src.index("_pcmd() {"):src.index("# Fills HERMES_FOUND")]
    agent = src[src.index("in_hermes_agent() {"):src.index("docker_mode() {")]
    box.gateway_process("root", 77)
    script = (f"PROC={box.proc}\n{funcs}\n{agent}\n_ppid() {{ case \"$1\" in 77) echo 1 ;; *) echo 77 ;; esac; }}\n"
              "in_hermes_agent && echo agent || echo plain\n")
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert p.stdout.strip() == "agent", p.stderr
    script = script.replace("echo 77 ;;", "echo 1 ;;")
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert p.stdout.strip() == "plain", p.stderr


def test_older_install_sh_under_an_agent_never_restarts_hermes_mid_install(box, tmp_path):
    """A release whose install.sh still restarts synchronously: get.sh's shim turns that into a detached restart
    scheduled after the install, and every other hermes command still reaches the real Hermes."""
    box.add_user("alice", 1001)
    box.hermes("alice")
    old = f"hermes config get x\nhermes {GW} {RS}\necho install-finished >> \"$FAKE_LOG\"\n"
    src = _local_wam(tmp_path, old)
    p = box.run("alice", _HERMES_GATEWAY="1", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"))
    assert p.returncode == 0, p.stdout + p.stderr
    log = box.log.read_text().splitlines()
    assert "real-hermes config get x" in log
    assert f"real-hermes {GW} {RS}" not in log, "Hermes was restarted inside the agent's own install"
    sched = [l for l in log if l.startswith("systemd-run ")]
    assert sched and "--user" in sched[0] and "--on-active=20s" in sched[0] and f"{GW} {RS}" in sched[0]
    assert log.index("install-finished") < log.index(sched[0])
    assert "Hermes will restart in ~20s" in p.stdout.replace(RS, "restart")


def test_terminal_install_keeps_the_synchronous_restart(box, tmp_path):
    box.add_user("alice", 1001)
    box.hermes("alice")
    src = _local_wam(tmp_path, f"hermes {GW} {RS}\n")
    p = box.run("alice", WAM_REPO=str(src), WAM_REF="main", WAM_DIR=str(tmp_path / "wam"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert f"real-hermes {GW} {RS}" in box.log.read_text()
    assert "systemd-run" not in box.log.read_text()


def _install_alongside_block(tmp: Path, agent_safe: str, outer: str = "0", *, uid: int = 1001) -> tuple[subprocess.CompletedProcess, str]:
    """Run install.sh's alongside block (agent-safe gate .. bootstrap) with stub wam/wizard; return the call log."""
    src = INSTALL_SH.read_text()
    block = src[src.index("# Run by a Hermes agent"):src.index("# 3) Fill every empty")]
    tail = src[src.index("PENDING=()"):src.index("case \"$status\" in")]
    home = tmp / "h"; (home / ".local/bin").mkdir(parents=True); (home / ".wizard-os").mkdir()
    log = tmp / "calls"
    for name in ("wam", "wizard"):
        (home / ".local/bin" / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> {log}\n"
                                                "case \"$1\" in alongside) [ \"$2\" = --detect ] && echo '{\"api_origin\":\"http://127.0.0.1:9\"}';; esac\nexit 0\n")
        (home / ".local/bin" / name).chmod(0o755)
    stub = tmp / "sbin"; stub.mkdir()
    # This extracted block asks only for the current numeric uid. Never consult
    # the pytest process/host identity, or silently accept a new id invocation.
    assert uid in (0, 1001)
    (stub / "id").write_text(f'#!/bin/sh\n[ "$#" -eq 1 ] && [ "$1" = -u ] || exit 64\nprintf "%s\\n" "{uid}"\n')
    (stub / "id").chmod(0o755)
    (stub / "systemd-run").write_text(f"#!/bin/sh\necho \"systemd-run $*\" >> {log}\n"); (stub / "systemd-run").chmod(0o755)
    (stub / "curl").write_text("#!/bin/sh\nexit 0\n"); (stub / "curl").chmod(0o755)
    script = f"set -uo pipefail\nFAILED=(); DEGRADED=(); OUTCOME={home}/.wizard-os/outcome.json\n{block}\n{tail}\necho RESTART_LATER=$RESTART_LATER\n"
    env = {"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(home), "WIZARD_MODE": "alongside",
           "WIZARD_AGENT_SAFE": agent_safe, "WIZARD_HERMES_BIN": "/opt/h/hermes", "WIZARD_PRIVATE_ACCESS": "none",
           "WIZARD_OUTER_INSTALL": outer}
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    return p, log.read_text() if log.exists() else ""


@pytest.mark.parametrize('uid', (1001, 0), ids=('nonroot-user-manager', 'root-system-manager'))
def test_install_sh_defers_the_link_restart_under_an_agent(tmp_path, uid):
    p, calls = _install_alongside_block(tmp_path, "1", uid=uid)
    assert p.returncode == 0, p.stderr
    link = next(l for l in calls.splitlines() if l.startswith("wizard link"))
    assert "--" + RS not in link
    sched = next(l for l in calls.splitlines() if l.startswith("systemd-run"))
    assert ('--user' in sched.split()) is (uid != 0)
    assert "--on-active=20s" in sched and f"/opt/h/hermes' {GW} {RS}" in sched
    assert calls.index("wizard doctor") < calls.index("systemd-run"), "summary/doctor first, restart last"
    assert "RESTART_LATER=1" in p.stdout and "send any" in p.stdout


@pytest.mark.parametrize('uid', (1001, 0), ids=('nonroot-user-manager', 'root-system-manager'))
def test_install_sh_restarts_synchronously_from_a_terminal(tmp_path, uid):
    p, calls = _install_alongside_block(tmp_path, "0", uid=uid)
    assert p.returncode == 0, p.stderr
    assert "--" + RS in next(l for l in calls.splitlines() if l.startswith("wizard link"))
    assert "systemd-run" not in calls and "RESTART_LATER=\n" in p.stdout + "\n"


def test_admin_preparation_refuses_rootless_before_host_changes(box):
    box.add_user("alice", 1001)
    box.hermes("alice")
    p = box.run("root", DOCKER_HOST="unix:///run/user/1001/docker.sock")
    assert p.returncode == 2
    assert "uses rootless Docker" in p.stderr
    assert "apt-get" not in box.log.read_text() and not box.handoff()
    assert list(box.sudoers.iterdir()) == []


@pytest.mark.parametrize("entry", [(), ("start",)])
def test_passwordful_user_gets_exact_one_time_admin_command_without_writes(box, entry):
    box.add_user("alice", 1001)
    hh = box.hermes("alice", home_dir=box.tmp / "custom-hermes-home")
    (box.bin / "sudo").write_text('#!/bin/sh\necho "sudo $*" >> "$FAKE_LOG"\nexit 1\n')
    p = box.run("alice", *entry, HERMES_HOME=str(hh), FAKE_STAT_OWNER="alice")
    assert p.returncode == 2, p.stdout + p.stderr
    assert "sudo env WIZARD_HERMES_USER=alice" in p.stderr
    assert f"HERMES_HOME={hh}" in p.stderr
    assert "WIZARD_AGENT_SAFE=1 bash -s start" in p.stderr
    assert "No passwordless sudo is required or granted" in p.stderr
    assert not (box.users["alice"][1] / ".wizard-os").exists()
    assert list(box.sudoers.iterdir()) == []
    assert "apt-get" not in box.log.read_text() and "systemd-run" not in box.log.read_text()


def test_storage_maintenance_refusal_precedes_any_host_preparation(box):
    box.add_user('alice', 1001)
    box.hermes('alice')
    p = box.run('root', FAKE_STORAGE_READY='0')
    assert p.returncode == 2, p.stdout + p.stderr
    calls = box.log.read_text()
    assert 'STORAGE_PREFLIGHT check alice ' in calls
    assert 'apt-get' not in calls and 'usermod' not in calls and not box.separated()
    assert not box.handoff() and list(box.sudoers.iterdir()) == []

