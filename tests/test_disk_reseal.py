"""wam disk reseal (wam/disk_reseal.py): seal the installer's root volume to the chip again, only at the box."""
import json
import os
import subprocess

import pytest

from wam import disk_reseal as dr


class Logind(dr.Box):
    def __init__(self, out, rc=0):
        super().__init__()
        self.out, self.rc = out, rc

    def run(self, argv, **kw):
        self.log.append(argv)
        return subprocess.CompletedProcess(argv, self.rc, self.out, "")


LOCAL = "Remote=no\nTTY={tty}\nType=tty\nClass=user\n"


@pytest.mark.parametrize("tty,cmdline,ok", [
    ("tty1", "", True),
    ("tty12", "", True),
    ("pts/3", "", False),
    ("ttyS0", "console=ttyS0", False),
    ("ttyS0", "quiet wizard.recovery_console=ttyS0", True),
    ("ttyS1", "wizard.recovery_console=ttyS0", False),
])
def test_local_console_only(monkeypatch, tmp_path, tty, cmdline, ok):
    monkeypatch.delenv("SSH_CONNECTION", raising=False); monkeypatch.delenv("SSH_TTY", raising=False)
    c = tmp_path / "cmdline"; c.write_text(cmdline + "\n")
    box = Logind(LOCAL.format(tty=tty))
    if ok:
        assert dr.local_console(c, box) == tty
    else:
        with pytest.raises(dr.Refused):
            dr.local_console(c, box)
    assert box.log[0][:3] == ["loginctl", "show-session", "self"]


def test_sudo_on_the_boxs_screen_is_accepted_although_fd0_is_a_pts(monkeypatch, tmp_path):
    """Debian's sudo runs the command on a new pseudo-terminal (use_pty): logind still knows it is tty1."""
    monkeypatch.delenv("SSH_CONNECTION", raising=False); monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(dr.os, "ttyname", lambda fd: "/dev/pts/4", raising=False)
    assert dr.local_console(tmp_path / "x", Logind(LOCAL.format(tty="tty1"))) == "tty1"


@pytest.mark.parametrize("out,rc", [
    ("Remote=yes\nTTY=\nType=tty\nClass=user\n", 0),        # SSH, even after sudo dropped SSH_*
    ("Remote=no\nTTY=\nType=unspecified\nClass=background\n", 0),   # an agent service or cron
    ("", 1),                                                   # no session at all
])
def test_remote_or_background_sessions_are_refused(monkeypatch, tmp_path, out, rc):
    monkeypatch.delenv("SSH_CONNECTION", raising=False); monkeypatch.delenv("SSH_TTY", raising=False)
    with pytest.raises(dr.Refused, match="own screen"):
        dr.local_console(tmp_path / "x", Logind(out, rc))


def test_ssh_refused_even_on_a_vt(monkeypatch, tmp_path):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    with pytest.raises(dr.Refused, match="not over SSH"):
        dr.local_console(tmp_path / "x", Logind(LOCAL.format(tty="tty1")))


class FakeBox(dr.Box):
    def __init__(self, root, answers=None, plugin=True):
        super().__init__(root=root)
        self.answers, self.plugin = answers or {}, plugin

    def token_plugin(self):
        return self.plugin

    def run(self, argv, **kw):
        self.log.append(argv)
        for prefix, rc in self.answers.items():
            if argv[:len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(argv, rc, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def sealed_box(monkeypatch, tmp_path):
    monkeypatch.setattr(dr.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dr, "local_console", lambda *a, **k: "tty1")
    from wam import disk
    monkeypatch.setattr(disk, "chip", lambda *a, **k: {"present": True})
    monkeypatch.setattr(dr, "boot_facts", lambda b: {"pcr7_sha256": "cd"})
    d = tmp_path / "etc/wizard/disk"; d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"encrypted": True, "chip": "tpm2", "stage": "sealed", "pcr7_sha256": "ab"}))
    (d / "volume").write_text("NAME=x\nSPEC=UUID=1\nDEV=/dev/disk/by-uuid/1\n")
    return d


def test_reseal_refuses_without_a_sealed_disk_or_with_setup_unfinished(monkeypatch, tmp_path):
    monkeypatch.setattr(dr.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dr, "local_console", lambda *a, **k: "tty1")
    box = FakeBox(tmp_path)
    with pytest.raises(dr.Refused, match="no disk sealed"):
        dr.reseal(box)
    d = tmp_path / "etc/wizard/disk"; d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"encrypted": True, "chip": "tpm2", "stage": "sealed"}))
    (d / "volume").write_text("DEV=/dev/disk/by-uuid/1\n")
    (d / "install.key").write_text("k")
    with pytest.raises(dr.Refused, match="first-run"):
        dr.reseal(box)
    assert box.log == []


def test_reseal_only_for_the_installer_root_volume(sealed_box, tmp_path):
    (sealed_box / "state.json").write_text(json.dumps({"encrypted": True, "chip": "tpm2", "scope": "wizard-data"}))
    box = FakeBox(tmp_path)
    with pytest.raises(dr.Refused, match="no disk sealed"):
        dr.reseal(box)
    assert box.log == []


def test_reseal_seals_a_fresh_chip_slot_and_records_the_new_boot(sealed_box, tmp_path):
    box = FakeBox(tmp_path)
    assert dr.reseal(box) == 0
    assert box.log[0][:4] == ["systemd-cryptenroll", "--wipe-slot=tpm2", "--tpm2-device=auto", "--tpm2-pcrs=7"]
    assert box.log[0][-1] == "/dev/disk/by-uuid/1" and "--unlock-key-file" not in " ".join(box.log[0])
    assert box.log[1] == ["cryptsetup", "open", "--test-passphrase", "--token-only", "/dev/disk/by-uuid/1"]
    st = json.loads((sealed_box / "state.json").read_text())
    assert st["stage"] == "sealed" and st["pcr7_sha256"] == "cd"


def test_reseal_refuses_before_any_change_without_the_chip_unlock_library(sealed_box, tmp_path):
    box = FakeBox(tmp_path, plugin=False)
    with pytest.raises(dr.Refused, match="systemd-cryptsetup"):
        dr.reseal(box)
    assert box.log == []


def test_failed_test_records_it_so_status_says_reseal_even_with_pcr7_unchanged(sealed_box, tmp_path, monkeypatch):
    box = FakeBox(tmp_path, {("cryptsetup", "open"): 1})
    with pytest.raises(dr.Refused, match="asks for the recovery key") as e:
        dr.reseal(box)
    assert "Nothing else changed" not in str(e.value.code)
    st = json.loads((sealed_box / "state.json").read_text())
    assert st["stage"] == "reseal-failed" and st["pcr7_sha256"] == "ab"
    from wam import disk
    monkeypatch.setattr(disk, "DISK_DIR", sealed_box)
    monkeypatch.setattr(disk, "chip", lambda *a, **k: {"present": True, "version": "2", "pcr7_sha256": "ab"})
    monkeypatch.setattr(disk, "_kind", lambda home: "dedicated")
    monkeypatch.setattr(disk, "crypt_parents", lambda path: [{"device": "/dev/vda3", "uuid": "1"}])
    monkeypatch.setattr(disk, "luks_meta", lambda dev, allow_sudo=True: None)
    monkeypatch.setattr(disk, "boot_updates", lambda st: {"changed_since_seal": [], "update_waiting": False})
    r = disk.status(allow_sudo=False)
    assert r["state"] == "chip-mismatch" and "wam disk reseal" in r["line"]


def test_reseal_waits_for_a_restart_after_a_shim_update(sealed_box, tmp_path):
    (tmp_path / "proc").mkdir(); (tmp_path / "proc/stat").write_text("cpu 1 2 3\nbtime 1000\n")
    shim = tmp_path / "boot/efi/EFI/debian/shimx64.efi"; shim.parent.mkdir(parents=True); shim.write_bytes(b"shim")
    os.utime(shim, (2000, 2000))
    box = FakeBox(tmp_path)
    with pytest.raises(dr.Refused, match="restart"):
        dr.reseal(box)
    assert box.log == []
    os.utime(shim, (500, 500))
    assert dr.reseal(box) == 0


def test_cli_routes_reseal_and_nothing_else(monkeypatch):
    from wam import cli
    monkeypatch.setattr(dr, "main", lambda argv=None: 7)
    assert cli.main(["disk", "reseal"]) == 7
    with pytest.raises(SystemExit):
        cli.main(["disk", "encrypt", "--device", "/dev/sdb"])
