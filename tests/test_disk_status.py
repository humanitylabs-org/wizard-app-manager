"""wam disk status (wam/disk.py): the plain Settings line for every state in wizard-os spec/DISK_ENCRYPTION.md."""
import json
import sys

import pytest

from wam import disk

SB_ON = b"\x06\x00\x00\x00\x01"
SB_OFF = b"\x06\x00\x00\x00\x00"
PCR = "ab" * 32


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fake box: /etc/wizard/disk, the chip, efivars, install-info and the commands disk.py runs."""
    d = tmp_path / "disk"; d.mkdir()
    tpm = tmp_path / "tpm"; (tpm / "pcr-sha256").mkdir(parents=True)
    efi = tmp_path / "efivars"; efi.mkdir()
    info = tmp_path / "install-info"
    monkeypatch.setattr(disk, "DISK_DIR", d)
    monkeypatch.setattr(disk, "TPM", tpm)
    monkeypatch.setattr(disk, "EFIVARS", efi)
    monkeypatch.setattr(disk, "INSTALL_INFO", info)
    monkeypatch.setattr(disk, "MAC_HOST_FILE", tmp_path / "no-mac-host.json")
    monkeypatch.setattr(disk, "BIOS_VERSION", tmp_path / "bios_version")
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []
    fake = {"luks": False, "virt": "none", "meta": None, "shim": "15.8-1", "candidate": "15.8-1", "fdesetup": ""}

    def run(cmd, timeout=10):
        calls.append(cmd)
        name = cmd[0] if cmd[0] != "sudo" else cmd[2]
        if name == "findmnt":
            return 0, "/dev/mapper/wizard--vg-root\n"
        if name == "lsblk":
            tree = {"name": "/dev/mapper/wizard--vg-root", "type": "lvm", "fstype": "ext4"}
            if fake["luks"]:
                tree["children"] = [{"name": "/dev/mapper/vda3_crypt", "type": "crypt", "fstype": "LVM2_member",
                                     "children": [{"name": "/dev/vda3", "type": "part", "fstype": "crypto_LUKS",
                                                   "uuid": "u-1"}]}]
            else:
                tree = {"name": "/dev/vda1", "type": "part", "fstype": "ext4"}
            return 0, json.dumps({"blockdevices": [tree]})
        if name == "cryptsetup":
            return (0, json.dumps(fake["meta"])) if fake["meta"] else (1, "")
        if name == "systemd-detect-virt":
            return (0 if fake["virt"] != "none" else 1), fake["virt"] + "\n"
        if name == "dpkg-query":
            return 0, fake["shim"]
        if name == "apt-cache":
            return 0, f"shim-signed:\n  Installed: {fake['shim']}\n  Candidate: {fake['candidate']}\n"
        if name == "fdesetup":
            return 0, fake["fdesetup"]
        return 127, ""
    monkeypatch.setattr(disk, "_run", run)

    class Box:
        pass
    b = Box()
    b.d, b.tpm, b.efi, b.info, b.fake, b.calls, b.root = d, tpm, efi, info, fake, calls, tmp_path

    def chip_present(pcr=PCR):
        (tpm / "tpm_version_major").write_text("2\n")
        (tpm / "pcr-sha256" / "7").write_text(pcr.upper() + "\n")

    def secure_boot(on=True):
        (efi / f"SecureBoot-{disk.GLOBAL_GUID}").write_bytes(SB_ON if on else SB_OFF)

    def sealed(**extra):
        st = {"schema": 1, "encrypted": True, "chip": "tpm2", "stage": "sealed", "reason": "",
              "recovery_confirmed": True, "pcrs": [7], "pcr7_sha256": PCR, "shim_version": "15.8-1",
              "bios_version": "", "efivars_sha256": disk.efivar_hashes(efi)}
        st.update(extra)
        (d / "state.json").write_text(json.dumps(st))
        (d / "recovery-confirmed").write_text("2026-11-07T00:00:00Z\n")
    b.chip_present, b.secure_boot, b.sealed = chip_present, secure_boot, sealed
    info.write_text("installer=wizard-os-iso 0.2\n")
    return b


META = {"keyslots": {"1": {"type": "luks2"}, "2": {"type": "luks2"}},
        "tokens": {"0": {"type": "systemd-tpm2", "tpm2-pcrs": [7], "keyslots": ["1"]},
                   "1": {"type": "systemd-recovery", "keyslots": ["2"]}}}


def test_sealed_dedicated_box(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed()
    r = disk.status()
    assert r["state"] == "sealed" and r["level"] == "ok"
    assert r["line"] == "Disk encrypted, unlocks with this box's security chip"
    assert r["encrypted"] and r["chip"]["sealed"] and r["recovery_key"]["confirmed_saved"]


def test_header_read_when_root_is_allowed(box, monkeypatch):
    box.fake["luks"] = True; box.fake["meta"] = META; box.chip_present(); box.secure_boot(); box.sealed()
    monkeypatch.setattr(disk.os, "geteuid", lambda: 0)
    r = disk.status()
    assert r["header_read"] and r["chip"]["pcrs"] == [7] and r["recovery_key"]["enrolled"]


def test_settings_route_never_uses_sudo(box, monkeypatch):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed()
    monkeypatch.setattr(disk.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(disk.shutil, "which", lambda n: "/usr/bin/" + n)
    disk.status(allow_sudo=False)
    assert not any(c[0] == "sudo" for c in box.calls)


def test_temporary_key_overrides_everything(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed()
    (box.d / "install.key").write_text("x")
    r = disk.status()
    assert r["state"] == "not-protected-yet" and r["level"] == "error"
    assert r["line"].startswith("Not protected yet: the disk key is still stored on this box")


def test_secure_boot_off_note(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(False); box.sealed()
    r = disk.status()
    assert r["state"] == "sealed"
    assert r["line"] == ("Disk encrypted, unlocks with this box's security chip. "
                         "Secure Boot is off: turn it on for stronger protection.")


def test_chip_no_longer_matches(box):
    box.fake["luks"] = True; box.chip_present(pcr="cd" * 32); box.secure_boot(); box.sealed()
    r = disk.status()
    assert r["state"] == "chip-mismatch"
    assert "run `wam disk reseal` at this box" in r["line"]


def test_boot_update_since_seal_and_waiting(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed()
    (box.efi / "dbx-d719b2cb-3d3a-4596-a3bc-dad00e67656f").write_bytes(b"new revocations")
    box.fake["candidate"] = "16.0-1"
    r = disk.status()
    assert "dbx" in r["boot_updates"]["changed_since_seal"]
    assert disk.UPDATE_NOTE in r["line"] and disk.WAITING_NOTE in r["line"]


def test_shim_changed_since_seal(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed(shim_version="15.7-1")
    r = disk.status()
    assert r["boot_updates"]["changed_since_seal"] == ["shim"] and disk.UPDATE_NOTE in r["line"]


def test_no_chip_install(box):
    (box.d / "state.json").write_text(json.dumps({"encrypted": False, "chip": "none", "stage": "plain",
                                                   "reason": "no-security-chip"}))
    r = disk.status()
    assert r["state"] == "no-chip" and not r["encrypted"]
    assert r["line"] == "Not encrypted: no security chip found (it may be turned off in the firmware settings)"


def test_passphrase_only(box):
    box.fake["luks"] = True
    r = disk.status()
    assert r["state"] == "passphrase"


def test_vps(box):
    box.info.unlink(); box.fake["virt"] = "kvm"
    r = disk.status()
    assert r["state"] == "vps"
    assert r["line"] == "Not encrypted by Wizard: relies on your provider's disk encryption, if it has any"


def test_plain_linux_alongside(box, tmp_path, monkeypatch):
    box.info.unlink()
    from wam import alongside
    monkeypatch.setattr(alongside, "load", lambda home: {"mode": "alongside"})
    r = disk.status(tmp_path)
    assert r["platform"] == "alongside" and r["state"] == "plain" and r["line"] == "Not encrypted"


def test_alongside_wizard_data_volume(box, tmp_path, monkeypatch):
    box.info.unlink(); box.fake["luks"] = True; box.chip_present(); box.secure_boot()
    box.sealed(scope="wizard-data", mount="/srv/wizard-data")
    from wam import alongside
    monkeypatch.setattr(alongside, "load", lambda home: {"mode": "alongside"})
    r = disk.status(tmp_path)
    assert r["state"] == "sealed-wizard-data"
    assert r["line"] == "Wizard data encrypted, unlocks with this box's security chip"
    assert ["findmnt", "-no", "SOURCE", "--target", "/srv/wizard-data"] in box.calls


@pytest.mark.parametrize("out,state,line", [
    ("FileVault is On.\n", "mac-on", "Protected by FileVault on this Mac"),
    ("FileVault is Off.\n", "mac-off", "Not encrypted: FileVault is off on this Mac"),
    ("", "mac-unknown", "Couldn't check FileVault on this Mac"),
])
def test_mac(box, monkeypatch, out, state, line):
    monkeypatch.setattr(sys, "platform", "darwin"); box.fake["fdesetup"] = out
    r = disk.status()
    assert (r["state"], r["line"]) == (state, line)


def test_mac_guest_reads_host_file(box, monkeypatch):
    host = box.root / "host.json"; host.write_text(json.dumps({"filevault": "off"}))
    monkeypatch.setattr(disk, "MAC_HOST_FILE", host)
    r = disk.status()
    assert r["platform"] == "mac-guest" and r["state"] == "mac-off"


def test_public_shape_has_no_devices_or_hashes(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed()
    pub = json.dumps(disk.public(disk.status()))
    assert "/dev/" not in pub and PCR not in pub and "efivars" not in pub
    assert json.loads(pub)["line"] == "Disk encrypted, unlocks with this box's security chip"


def test_status_never_writes(box):
    box.fake["luks"] = True; box.chip_present(); box.secure_boot(); box.sealed()
    before = sorted((p.name, p.stat().st_mtime_ns) for p in box.d.iterdir())
    disk.status(); disk.describe(disk.status())
    assert sorted((p.name, p.stat().st_mtime_ns) for p in box.d.iterdir()) == before
    assert not any(c[0] in ("cryptsetup",) and "open" in c for c in box.calls)


def test_gateway_admits_the_disk_route_only_as_get_settings_route():
    from wam import store_install as si
    assert si.gateway_path_ok("/_disk") and "_disk" in si.GATEWAY_CONTROL
    assert not si.gateway_path_ok("/_disk/anything")
