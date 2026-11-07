"""`wam disk reseal`: seal the disk to this box's security chip again after a deliberate firmware or Secure Boot change.

wizard-os spec/DISK_ENCRYPTION.md (decision #112). For the chip-sealed root volume a wizard-os-iso install makes.
Run as root at the box's own screen: the owner types the recovery key into systemd-cryptenroll's own prompt (never
an argument, a file or an agent), a fresh chip slot sealed to PCR 7 replaces the old one, and the new slot is tested
token-only before the state is updated. Status: wam/disk.py.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

DISK_DIR = Path("/etc/wizard/disk")
STATE = DISK_DIR / "state.json"
CMDLINE = Path("/proc/cmdline")
TOKEN_PLUGIN = "/usr/lib/*/cryptsetup/libcryptsetup-token-systemd-tpm2.so"   # Debian: systemd-cryptsetup


class Refused(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"wam disk reseal: {msg}")


@dataclass
class Box:
    """Everything that touches the system goes through here, so tests can watch it."""
    root: Path = Path("/")
    log: list = field(default_factory=list)

    def run(self, argv: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        self.log.append(argv)
        r = subprocess.run(argv, text=True, capture_output=capture)
        if check and r.returncode != 0:
            raise Refused(f"{' '.join(argv[:3])} failed ({r.returncode}) {(r.stderr or '').strip()[-400:]}")
        return r

    def p(self, path: Path | str) -> Path:
        path = Path(path)
        return self.root / path.relative_to("/") if self.root != Path("/") else path

    def token_plugin(self) -> bool:
        return bool(glob.glob(str(self.p(TOKEN_PLUGIN))))


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _session(box: "Box | None" = None) -> dict:
    """The caller's logind session (sudo keeps it: its PAM stack has no pam_systemd). Empty when there is none."""
    run = (box or Box()).run
    r = run(["loginctl", "show-session", "self", "-p", "Remote", "-p", "TTY", "-p", "Type", "-p", "Class"], check=False)
    if r.returncode != 0:
        return {}
    return dict(l.split("=", 1) for l in (r.stdout or "").splitlines() if "=" in l)


def local_console(cmdline: Path = CMDLINE, box: "Box | None" = None) -> str:
    """The box's own screen: a local logind session on a virtual terminal, or on a serial line the installer was told
    to trust (wizard.recovery_console=ttySn, as wizard-os-iso's first-boot seal). Never SSH, an agent or cron.
    Asked of logind, not ttyname(0): sudo runs commands on a fresh pseudo-terminal (use_pty) and drops SSH_*."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        raise Refused("run this at the box's own screen, not over SSH")
    s = _session(box)
    if not s:
        raise Refused("run this at the box's own screen (no login session found)")
    if s.get("Remote") != "no" or s.get("Type") != "tty" or s.get("Class") not in ("user", "user-early"):
        raise Refused("run this at the box's own screen, not over SSH or from another program")
    tty = s.get("TTY", "")
    if re.fullmatch(r"tty([1-9]|[1-5][0-9]|6[0-3])", tty):
        return tty
    trusted = re.findall(r"(?:^|\s)wizard\.recovery_console=(ttyS[0-9]{1,2})(?=\s|$)", _read(cmdline))
    if re.fullmatch(r"ttyS[0-9]{1,2}", tty) and tty in trusted:
        return tty
    raise Refused(f"run this at the box's own screen ({tty or 'no terminal'} is not one)")


def boot_update_pending(box: Box) -> bool:
    """A shim on the EFI partition newer than this boot: PCR 7 changes only at the next start."""
    try:
        btime = next(int(l.split()[1]) for l in _read(box.p("/proc/stat")).splitlines() if l.startswith("btime "))
    except (StopIteration, ValueError, IndexError):
        return False
    for f in glob.glob(str(box.p("/boot/efi/EFI/*/shimx64.efi"))):
        try:
            if os.stat(f).st_mtime > btime:
                return True
        except OSError:
            pass
    return False


def load_state(box: Box) -> dict:
    try:
        d = json.loads(box.p(STATE).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(box: Box, st: dict) -> None:
    path = box.p(STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def boot_facts(box: Box) -> dict:
    """What disk.py compares later to say the chip may not match (the same fields as the installer's seal)."""
    from . import disk
    pcr = _read(box.p(disk.TPM / "pcr-sha256" / "7")).strip().lower()
    shim = box.run(["dpkg-query", "-W", "-f=${Version}", "shim-signed"], check=False)
    bios = _read(box.p(disk.BIOS_VERSION)).strip().replace('"', "").replace("\\", "")
    return {"pcr7_sha256": pcr, "shim_version": shim.stdout.strip() if shim.returncode == 0 else "",
            "bios_version": bios, "efivars_sha256": disk.efivar_hashes()}


def sealed_device(box: Box, st: dict) -> str:
    """The installer's chip-sealed root volume (wizard-os-iso writes DEV= to /etc/wizard/disk/volume)."""
    m = re.search(r"^DEV=(\S+)$", _read(box.p(DISK_DIR / "volume")), re.M)
    if st.get("chip") == "tpm2" and st.get("encrypted") and (st.get("scope") or "root") == "root" and m:
        return m.group(1)
    raise Refused("this box has no disk sealed to its security chip")


def reseal(box: Box | None = None) -> int:
    box = box or Box()
    if os.geteuid() != 0:
        raise Refused("needs root: run `sudo wam disk reseal` at this box")
    local_console(box=box)
    st = load_state(box)
    dev = sealed_device(box, st)
    if box.p(DISK_DIR / "install.key").exists():
        raise Refused("first-run disk setup has not finished: restart and finish it on this box's screen")
    from . import disk
    if not disk.chip().get("present"):
        raise Refused("no TPM 2.0 security chip is visible: turn it on in the firmware settings first")
    if not box.token_plugin():
        # Without it neither the test below nor the start-up unlock can use the chip: refuse before changing anything.
        raise Refused("the chip unlock library is missing: install systemd-cryptsetup, then try again")
    if boot_update_pending(box):
        raise Refused("a boot security update is installed but not started yet: restart, type the recovery key if "
                      "asked, then run this again")
    print("Sealing the disk to this box's security chip as it starts now.")
    print("Type the recovery key (the 8 groups of letters) and press Enter.")
    box.run(["systemd-cryptenroll", "--wipe-slot=tpm2", "--tpm2-device=auto", "--tpm2-pcrs=7", dev], capture=False)
    if box.run(["cryptsetup", "open", "--test-passphrase", "--token-only", dev], check=False).returncode != 0:
        st.update(stage="reseal-failed", reason="chip-test-failed")   # status: "run wam disk reseal"
        save_state(box, st)
        raise Refused("the new chip slot does not open the disk, so the next start asks for the recovery key "
                      "(it still works). Run `sudo wam disk reseal` here again")
    st.update(boot_facts(box), stage="sealed", reason="", sealed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              pcrs=[7], secure_boot=disk.secure_boot() is True)
    save_state(box, st)
    print("Done: the disk is sealed to this box's security chip again. Restart now to confirm. If it asks for the "
          "recovery key, type it and run `sudo wam disk reseal` here again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return reseal()
    except Refused as e:
        print(e.code, file=__import__("sys").stderr)
        return 2
