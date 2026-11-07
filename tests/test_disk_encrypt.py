"""wam disk encrypt (wam/disk_encrypt.py): the safety rails that run before anything is touched, the mount
layout it writes, and the file check that gates every switch. The real move is proven in a throwaway VM
(qa/disk_encrypt_vm.py)."""
import io
import json
import os
import pwd
import subprocess

import pytest

from wam import disk_encrypt as de


def test_tab_block_replaces_only_its_own_lines():
    base = "UUID=1 / ext4 defaults 0 1\n# owner's line\n"
    one = de.tab_block(base, ["a b c"])
    assert one == base + f"{de.BEGIN}\na b c\n{de.END}\n"
    two = de.tab_block(one, ["x y z"])
    assert two.count(de.BEGIN) == 1 and "a b c" not in two and "x y z" in two
    assert de.tab_block(two, None) == base


def test_mount_lines_never_block_boot_and_never_prompt():
    crypt, fs = de.mount_lines("u-1", [{"name": "0-wizard-os", "path": "/home/o/.wizard-os"},
                                       {"name": "1-runtipi", "path": "/home/o/my runtipi"}])
    assert crypt == ["wizard_data UUID=u-1 none luks,discard,nofail,headless=true,tpm2-device=auto"]
    assert all("nofail" in ln for ln in fs)
    assert fs[0].startswith("/dev/mapper/wizard_data /srv/wizard-data ext4")
    assert fs[2] == ("/srv/wizard-data/1-runtipi /home/o/my\\040runtipi none "
                     "bind,nofail,x-systemd.requires-mounts-for=/srv/wizard-data 0 0")


def test_manifest_catches_content_mode_owner_and_link_changes(tmp_path):
    a = tmp_path / "a"; (a / "d").mkdir(parents=True)
    (a / "d" / "f").write_text("hello"); os.symlink("d/f", a / "l"); os.chmod(a / "d" / "f", 0o600)
    b = tmp_path / "b"
    subprocess.run(["cp", "-a", f"{a}/.", str(b)], check=True)
    assert de.compare(de.manifest(a), de.manifest(b)) == []
    (b / "d" / "f").write_text("hellO")
    assert de.compare(de.manifest(a), de.manifest(b)) == ["differs d/f"]
    (b / "d" / "f").write_text("hello"); os.chmod(b / "d" / "f", 0o644)
    assert de.compare(de.manifest(a), de.manifest(b)) == ["differs d/f"]
    os.chmod(b / "d" / "f", 0o600); os.unlink(b / "l"); os.symlink("d", b / "l")
    assert de.compare(de.manifest(a), de.manifest(b)) == ["differs l"]
    os.unlink(b / "l")
    assert de.compare(de.manifest(a), de.manifest(b)) == ["missing l"]


def test_recovery_key_confirmation_loops_until_first_group_and_clears(tmp_path):
    key = "-".join(["abcdefgh"] + ["ijklmnop"] * 7)
    out = io.StringIO()
    de.show_recovery_key(key, io.StringIO("nope\nijklmnop\nABCD EFGH\n"), out)
    text = out.getvalue()
    assert text.count(key) == 1
    assert text.count("does not match") == 2
    assert text.endswith("\033[2J\033[3J\033[H")


@pytest.mark.parametrize("tty,cmdline,ok", [
    ("/dev/tty1", "", True),
    ("/dev/tty12", "", True),
    ("/dev/pts/3", "", False),
    ("/dev/ttyS0", "console=ttyS0", False),
    ("/dev/ttyS0", "quiet wizard.recovery_console=ttyS0", True),
    ("/dev/ttyS1", "wizard.recovery_console=ttyS0", False),
])
def test_local_console_only(monkeypatch, tmp_path, tty, cmdline, ok):
    monkeypatch.delenv("SSH_CONNECTION", raising=False); monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(de.os, "ttyname", lambda fd: tty)
    c = tmp_path / "cmdline"; c.write_text(cmdline + "\n")
    if ok:
        assert de.local_console(c) == tty
    else:
        with pytest.raises(de.Refused):
            de.local_console(c)


def test_ssh_refused_even_on_a_vt(monkeypatch, tmp_path):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.setattr(de.os, "ttyname", lambda fd: "/dev/tty1")
    with pytest.raises(de.Refused, match="not over SSH"):
        de.local_console(tmp_path / "x")


class FakeBox(de.Box):
    def __init__(self, answers):
        super().__init__()
        self.answers = answers

    def run(self, argv, **kw):
        self.log.append(argv)
        for prefix, (rc, out) in self.answers.items():
            if argv[:len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(argv, rc, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def blockdev(monkeypatch):
    monkeypatch.setattr(de.os, "stat", lambda p: os.stat_result((0o60660,) + (0,) * 9))
    monkeypatch.setattr(de.Path, "exists", lambda self: True)


def _box(tree, sig="", root_parents="/dev/sda2\n/dev/sda\n"):
    return FakeBox({("lsblk", "-J"): (0, json.dumps({"blockdevices": [tree]})),
                    ("findmnt",): (0, "/dev/sda2\n"),
                    ("lsblk", "-ln"): (0, root_parents),
                    ("blkid",): (0 if sig else 2, sig)})


def test_device_checks(blockdev):
    free = {"name": "/dev/sdb", "type": "disk", "size": 64 << 30, "mountpoints": [None]}
    assert de.check_device(_box(free), "/dev/sdb", False, 10 << 30)["name"] == "/dev/sdb"
    with pytest.raises(de.Refused, match="not empty"):
        de.check_device(_box(free, sig="TYPE=ext4\n"), "/dev/sdb", False, 1)
    assert de.check_device(_box(free, sig="TYPE=ext4\n"), "/dev/sdb", True, 1)
    with pytest.raises(de.Refused, match="too small"):
        de.check_device(_box(free), "/dev/sdb", False, 100 << 30)
    with pytest.raises(de.Refused, match="in use"):
        de.check_device(_box(dict(free, mountpoints=["/mnt"])), "/dev/sdb", True, 1)
    with pytest.raises(de.Refused, match="in use"):
        de.check_device(_box(dict(free, children=[{"name": "/dev/sdb1", "mountpoints": [None]}])),
                        "/dev/sdb", True, 1)
    with pytest.raises(de.Refused, match="never re-encrypted"):
        de.check_device(_box(dict(free, name="/dev/sda")), "/dev/sda", True, 1)


def test_data_paths_refuse_nesting(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".wizard-os").mkdir(parents=True); (home / "runtipi" / "media").mkdir(parents=True)
    user = pwd.struct_passwd(("o", "x", 1000, 1000, "", str(home), "/bin/sh"))
    monkeypatch.delenv("WIZARD_OS_HOME", raising=False); monkeypatch.delenv("RUNTIPI_ROOT", raising=False)
    assert de.data_paths(user) == [home / ".wizard-os", home / "runtipi"]
    with pytest.raises(de.Refused, match="inside"):
        de.data_paths(user, [str(home / "runtipi" / "media")])


def test_owner_must_come_from_sudo(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False); monkeypatch.delenv("WIZARD_OWNER", raising=False)
    with pytest.raises(de.Refused, match="sudo"):
        de.owner()


def test_encrypt_refuses_before_touching_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(de.os, "geteuid", lambda: 1000)
    box = FakeBox({})
    with pytest.raises(de.Refused, match="needs root"):
        de.encrypt("/dev/sdb", erase=None, backup_file=None, extra=[], box=box)
    monkeypatch.setattr(de.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SSH_CONNECTION", "x")
    with pytest.raises(de.Refused, match="SSH"):
        de.encrypt("/dev/sdb", erase=None, backup_file=None, extra=[], box=box)
    assert box.log == []


def test_cli_routes_disk_encrypt(monkeypatch):
    from wam import cli
    seen = []
    monkeypatch.setattr(de, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["disk", "encrypt", "--device", "/dev/sdb"]) == 0
    assert seen == [["--device", "/dev/sdb"]]


def test_reseal_refuses_without_a_sealed_disk_or_with_setup_unfinished(monkeypatch, tmp_path):
    monkeypatch.setattr(de.os, "geteuid", lambda: 0)
    monkeypatch.setattr(de, "local_console", lambda *a: "/dev/tty1")
    box = FakeBox({}); box.root = tmp_path
    with pytest.raises(de.Refused, match="no disk sealed"):
        de.reseal(box)
    d = tmp_path / "etc/wizard/disk"; d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"encrypted": True, "chip": "tpm2", "stage": "sealed"}))
    (d / "volume").write_text("NAME=x\nSPEC=UUID=1\nDEV=/dev/disk/by-uuid/1\n")
    (d / "install.key").write_text("k")
    with pytest.raises(de.Refused, match="first-run"):
        de.reseal(box)
    assert box.log == []
    (d / "install.key").unlink()
    from wam import disk
    monkeypatch.setattr(disk, "chip", lambda *a, **k: {"present": True})
    monkeypatch.setattr(de, "boot_facts", lambda b: {"pcr7_sha256": "ab"})
    assert de.reseal(box) == 0
    assert box.log[0][:4] == ["systemd-cryptenroll", "--wipe-slot=tpm2", "--tpm2-device=auto", "--tpm2-pcrs=7"]
    assert box.log[0][-1] == "/dev/disk/by-uuid/1" and "--unlock-key-file" not in " ".join(box.log[0])
    st = json.loads((d / "state.json").read_text())
    assert st["stage"] == "sealed" and st["pcr7_sha256"] == "ab"


def test_cli_routes_reseal(monkeypatch):
    from wam import cli
    monkeypatch.setattr(de, "reseal", lambda: 7)
    assert cli.main(["disk", "reseal"]) == 7
