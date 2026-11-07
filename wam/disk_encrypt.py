"""`wam disk encrypt`: move Wizard's data onto a new TPM-sealed LUKS2 volume on a box whose root stays as it is.

wizard-os spec/DISK_ENCRYPTION.md, "Linux alongside an existing OS, and boxes installed before this decision".
Opt-in, attended, as root at the box's own screen. Root is never re-encrypted and nothing is repartitioned:
the owner names an empty drive or partition. Order (each step recorded in /etc/wizard/disk/state.json):

  1. preflight      root, local console, TPM 2.0, the device is not in use and holds nothing (or the owner types
                    its name to erase it), it fits the data
  2. backup         `wam backup export` then `wam backup verify` (or --backup-file: verify an existing one)
  3. volume         LUKS2 with a random key that lives only in /run (tmpfs), never on a disk; sealed to the
                    chip (PCR 7) and tested token-only; one recovery key shown once on this screen and typed
                    back (first group); then the temporary slot is wiped
  4. move           stop Wizard, copy each data folder onto the volume, check every file (path, type, mode,
                    owner, size, sha256, link target), rename the old folder aside, leave an empty immutable
                    mount point (chattr +i) so nothing writes plaintext underneath while the volume is locked,
                    bind-mount the copy back; crypttab/fstab entries are nofail, headless=true; Wizard's units
                    only start when the volume is mounted; check every file again through the bind mount; start
  5. done           shim-signed held, automatic fwupd updates off, a locked-volume notice on the console
  6. confirm        `wam disk encrypt --confirm` deletes the old copies and trims; `--rollback` copies the
                    current data back (never the stale old copy), checks it and removes the mounts

`wam disk unlock` takes the recovery key at the box when the chip refuses. Status: wam/disk.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DISK_DIR = Path("/etc/wizard/disk")
STATE = DISK_DIR / "state.json"
MOUNT = Path("/srv/wizard-data")
MAPPER = "wizard_data"
RUN_DIR = Path("/run/wizard-disk-encrypt")
CRYPTTAB = Path("/etc/crypttab")
FSTAB = Path("/etc/fstab")
BEGIN, END = "# wizard-disk-encrypt begin (wam disk encrypt; do not edit)", "# wizard-disk-encrypt end"
WIZARD_USER_UNITS = ("wizard-app-manager-api.service", "wizard-host-dashboard.service",
                     "wizard-app-manager-sync.timer", "wizard-backup.timer")
LOCKED_UNIT = "wizard-data-locked.service"
LOCKED_LINE = "Wizard data is locked: enter the recovery key at this box (sudo wam disk unlock)"
CMDLINE = Path("/proc/cmdline")


class Refused(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"wam disk encrypt: {msg}")


# ---------------------------------------------------------------- running things

@dataclass
class Box:
    """Everything that touches the system goes through here, so tests can watch it."""
    root: Path = Path("/")
    dry: bool = False
    log: list = field(default_factory=list)

    def run(self, argv: list[str], *, input: str | None = None, check: bool = True, capture: bool = True,
            secret: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
        self.log.append(argv if not secret else [argv[0], "<secret>"])
        r = subprocess.run(argv, input=input, text=True, capture_output=capture, env=env)
        if check and r.returncode != 0:
            err = "" if secret else (r.stderr or "").strip()[-400:]
            raise Refused(f"{' '.join(argv[:3])} failed ({r.returncode}) {err}")
        return r

    def p(self, path: Path | str) -> Path:
        path = Path(path)
        return self.root / path.relative_to("/") if self.root != Path("/") else path


# ---------------------------------------------------------------- the console and the recovery key

def local_console(cmdline: Path = CMDLINE) -> str:
    """The box's own screen: a virtual terminal, or a serial line the installer was told to trust
    (wizard.recovery_console=ttySn, as wizard-os-iso's first-boot seal). Never an SSH session."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        raise Refused("run this at the box's own screen, not over SSH: it shows the recovery key")
    try:
        tty = os.ttyname(0)
    except OSError:
        raise Refused("run this at the box's own screen (no terminal)") from None
    if re.fullmatch(r"/dev/tty[1-9][0-9]?", tty):
        return tty
    trusted = re.findall(r"(?:^|\s)wizard\.recovery_console=(ttyS[0-9]{1,2})(?=\s|$)", _read(cmdline))
    if re.fullmatch(r"/dev/ttyS[0-9]{1,2}", tty) and tty.removeprefix("/dev/") in trusted:
        return tty
    raise Refused(f"run this at the box's own screen ({tty} is not one)")


def show_recovery_key(key: str, tty_in, tty_out) -> None:
    """Show the key once, ask for its first group back, then clear the screen and its scrollback."""
    first = key.split("-", 1)[0]
    tty_out.write("\033[2J\033[H\nYour Wizard data recovery key. Write it down or photograph it now and keep it\n"
                  "somewhere safe away from this box. It is shown once and is never stored on the box.\n"
                  "You need it if this box's security chip ever refuses to unlock Wizard's data.\n\n"
                  f"    {key}\n\n")
    while True:
        tty_out.write("Type the first 8 letters of the key to confirm you saved it: ")
        tty_out.flush()
        got = re.sub(r"[^a-z]", "", (tty_in.readline() or "").lower())
        if got == first:
            break
        tty_out.write("That does not match. Check what you wrote down and try again.\n")
    tty_out.write("\033[2J\033[3J\033[H")
    tty_out.flush()


# ---------------------------------------------------------------- what moves

def owner() -> pwd.struct_passwd:
    name = os.environ.get("SUDO_USER") or os.environ.get("WIZARD_OWNER")
    if not name or name == "root":
        raise Refused("run it with sudo from the owner's account (sudo wam disk encrypt …)")
    return pwd.getpwnam(name)


def data_paths(user: pwd.struct_passwd, extra: list[str] | None = None) -> list[Path]:
    """The Wizard home (vault, tokens, agent state) and the Runtipi root (app data, owner files, user config)."""
    home = Path(user.pw_dir)
    wiz = Path(os.environ.get("WIZARD_OS_HOME") or home / ".wizard-os")
    cfg = {}
    try:
        cfg = json.loads((wiz / "app-manager.json").read_text())
    except (OSError, ValueError):
        pass
    runtipi = Path(os.environ.get("RUNTIPI_ROOT") or cfg.get("runtipi_root") or home / "runtipi").expanduser()
    out = [p for p in (wiz, runtipi, *(Path(e) for e in extra or [])) if p.is_dir() and not p.is_symlink()]
    for a in out:
        for b in out:
            if a != b and b.is_relative_to(a):
                raise Refused(f"{b} is inside {a}; name only the outer folder")
    if not out:
        raise Refused("found no Wizard data to move")
    return out


def du(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        for dp, dns, fns in os.walk(p):
            for n in fns:
                try:
                    total += os.lstat(os.path.join(dp, n)).st_blocks * 512
                except OSError:
                    pass
    return total


def manifest(base: Path) -> dict[str, tuple]:
    """Every entry under base: type, mode, owner, size, sha256 (files), target (links). Times are not compared."""
    out: dict[str, tuple] = {}
    for dp, dns, fns in os.walk(base):
        dns.sort()
        for n in sorted(dns + fns):
            full = os.path.join(dp, n)
            st = os.lstat(full)
            rel = os.path.relpath(full, base)
            kind = stat.S_IFMT(st.st_mode)
            extra: object = None
            if stat.S_ISREG(st.st_mode):
                h = hashlib.sha256()
                with open(full, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                extra = (st.st_size, h.hexdigest())
            elif stat.S_ISLNK(st.st_mode):
                extra = os.readlink(full)
            out[rel] = (kind, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, extra)
    return out


def compare(a: dict, b: dict) -> list[str]:
    diff = [f"missing {k}" for k in a if k not in b] + [f"extra {k}" for k in b if k not in a]
    diff += [f"differs {k}" for k in a if k in b and a[k] != b[k]]
    return diff


# ---------------------------------------------------------------- the device

def check_device(box: Box, dev: str, erase_ok: bool, need: int) -> dict:
    p = Path(dev)
    if not p.is_absolute() or not p.exists() or not stat.S_ISBLK(os.stat(dev).st_mode):
        raise Refused(f"{dev} is not a drive or partition")
    info = json.loads(box.run(["lsblk", "-J", "-b", "-p", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS", dev]).stdout)
    node = info["blockdevices"][0]

    def busy(n):
        if any(m for m in (n.get("mountpoints") or []) if m):
            return True
        return any(busy(c) for c in n.get("children") or [])
    if busy(node) or node.get("children"):
        raise Refused(f"{dev} is in use (mounted, or has partitions or volumes on it); name an unused one")
    root_src = box.run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()
    parents = box.run(["lsblk", "-ln", "-s", "-p", "-o", "NAME", root_src], check=False).stdout.split()
    if node["name"] in parents or dev in parents:
        raise Refused(f"{dev} holds this box's system; root is never re-encrypted in place")
    sig = box.run(["blkid", "-p", "-o", "export", dev], check=False).stdout
    if sig.strip() and not erase_ok:
        raise Refused(f"{dev} is not empty ({_fstype(sig)}); add --erase {dev} to erase it")
    if int(node.get("size") or 0) < need:
        raise Refused(f"{dev} is too small: {int(node['size']) >> 30} GB, Wizard data needs {need >> 30} GB")
    return node


def _fstype(export: str) -> str:
    m = re.search(r"^TYPE=(.*)$", export, re.M)
    return f"holds {m.group(1)}" if m else "holds data"


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


# ---------------------------------------------------------------- state

def load_state(box: Box) -> dict:
    try:
        return json.loads(box.p(STATE).read_text())
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
    """What disk.py compares later to say the chip may not match (same fields as the installer's seal)."""
    from . import disk
    pcr = _read(box.p(disk.TPM / "pcr-sha256" / "7")).strip().lower()
    shim = box.run(["dpkg-query", "-W", "-f=${Version}", "shim-signed"], check=False)
    bios = _read(box.p("/sys/class/dmi/id/bios_version")).strip().replace('"', "").replace("\\", "")
    return {"pcr7_sha256": pcr, "shim_version": shim.stdout.strip() if shim.returncode == 0 else "",
            "bios_version": bios, "efivars_sha256": disk.efivar_hashes()}


# ---------------------------------------------------------------- Wizard's services

def user_ctl(box: Box, user: pwd.struct_passwd, *args: str) -> None:
    env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{user.pw_uid}")
    box.run(["runuser", "-u", user.pw_name, "--", "systemctl", "--user", *args], check=False, env=env)


def stop_wizard(box: Box, user: pwd.struct_passwd, runtipi: Path | None) -> None:
    user_ctl(box, user, "stop", *WIZARD_USER_UNITS)
    cli = runtipi / "runtipi-cli" if runtipi else None
    if cli and cli.is_file():
        box.run([str(cli), "stop"], check=False)


def start_wizard(box: Box, user: pwd.struct_passwd, runtipi: Path | None) -> None:
    cli = runtipi / "runtipi-cli" if runtipi else None
    if cli and cli.is_file():
        box.run([str(cli), "start"], check=False)
    user_ctl(box, user, "start", "wizard-app-manager-api.service", "wizard-host-dashboard.service")


def unit_dropins(box: Box, user: pwd.struct_passwd, remove: bool = False) -> None:
    """Wizard's own (user) units start only when the volume is mounted. The owner's other services are untouched."""
    base = Path(user.pw_dir) / ".config" / "systemd" / "user"
    for unit in WIZARD_USER_UNITS:
        d = box.p(base / f"{unit}.d")
        f = d / "wizard-data.conf"
        if remove:
            f.unlink(missing_ok=True)
            try:
                d.rmdir()
            except OSError:
                pass
            continue
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# wam disk encrypt: Wizard data lives on the encrypted volume\n[Unit]\n"
                     f"ConditionPathIsMountPoint={MOUNT}\n")
        for q in (base, base / f"{unit}.d"):
            try:
                os.chown(box.p(q), user.pw_uid, user.pw_gid)
            except OSError:
                pass
        os.chown(f, user.pw_uid, user.pw_gid)
    user_ctl(box, user, "daemon-reload")


# ---------------------------------------------------------------- crypttab / fstab blocks

def tab_block(text: str, lines: list[str] | None) -> str:
    """Replace (or remove, lines=None) the marked block, keeping everything else exactly."""
    out, skip = [], False
    for ln in text.splitlines():
        if ln == BEGIN:
            skip = True
            continue
        if ln == END:
            skip = False
            continue
        if not skip:
            out.append(ln)
    if lines:
        out += [BEGIN, *lines, END]
    return "\n".join(out) + "\n"


def mount_lines(uuid: str, moves: list[dict]) -> tuple[list[str], list[str]]:
    crypt = [f"{MAPPER} UUID={uuid} none luks,discard,nofail,headless=true,tpm2-device=auto"]
    fs = [f"/dev/mapper/{MAPPER} {MOUNT} ext4 defaults,nofail,x-systemd.device-timeout=60s 0 2"]
    fs += [f"{MOUNT}/{m['name']} {_esc(m['path'])} none bind,nofail,x-systemd.requires-mounts-for={MOUNT} 0 0"
           for m in moves]
    return crypt, fs


def _esc(p: str) -> str:
    return p.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def locked_notice(box: Box, remove: bool = False) -> None:
    unit = box.p(Path("/etc/systemd/system") / LOCKED_UNIT)
    if remove:
        box.run(["systemctl", "disable", LOCKED_UNIT], check=False)
        unit.unlink(missing_ok=True)
        box.p("/run/issue.d/wizard-data-locked.issue").unlink(missing_ok=True)
        return
    unit.write_text(f"""[Unit]
Description=Say on the console when Wizard data is locked (wam disk encrypt)
After=local-fs.target cryptsetup.target
ConditionPathIsMountPoint=!{MOUNT}

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'mkdir -p /run/issue.d /run/motd.d && printf "%s\\\\n\\\\n" "{LOCKED_LINE}" | tee /run/issue.d/wizard-data-locked.issue > /run/motd.d/wizard-data-locked'

[Install]
WantedBy=multi-user.target
""")
    box.run(["systemctl", "daemon-reload"], check=False)
    box.run(["systemctl", "enable", LOCKED_UNIT], check=False)


def hold_boot_updates(box: Box) -> list[str]:
    done = []
    if box.run(["dpkg-query", "-W", "shim-signed"], check=False).returncode == 0:
        box.run(["apt-mark", "hold", "shim-signed"], check=False)
        done.append("shim-signed held")
    if box.run(["systemctl", "cat", "fwupd-refresh.timer"], check=False).returncode == 0:
        box.run(["systemctl", "disable", "--now", "fwupd-refresh.timer"], check=False)
        done.append("automatic fwupd updates off")
    return done


# ---------------------------------------------------------------- the steps

def backup(box: Box, user: pwd.struct_passwd, given: str | None) -> str:
    env = dict(os.environ, HOME=user.pw_dir)
    wam = os.environ.get("WIZARD_DISK_WAM") or (sys.argv[0] if Path(sys.argv[0]).name == "wam" else None) \
        or shutil.which("wam") or "wam"
    if given:
        path = given
    else:
        out_dir = Path(user.pw_dir) / "wizard-backups"
        path = str(out_dir / f"before-disk-encrypt-{time.strftime('%Y%m%d-%H%M%S')}.wizard-backup")
        box.run(["runuser", "-u", user.pw_name, "--", "mkdir", "-p", str(out_dir)], env=env)
        box.run(["runuser", "-u", user.pw_name, "--", wam, "backup", "export", "-o", path], env=env, capture=False)
    box.run(["runuser", "-u", user.pw_name, "--", wam, "backup", "verify", path], env=env, capture=False)
    return path


def cryptsetup_attach() -> str:
    """systemd's own unlocker: the exact code path crypttab uses at boot."""
    for c in (shutil.which("systemd-cryptsetup"), "/usr/lib/systemd/systemd-cryptsetup", "/lib/systemd/systemd-cryptsetup"):
        if c and os.path.isfile(c):
            return c
    raise Refused("systemd-cryptsetup is missing (Debian: apt install systemd-cryptsetup, or systemd before 256)")


def make_volume(box: Box, dev: str, tty_in, tty_out) -> str:
    """LUKS2 with a key only in RAM, sealed to the chip and tested through systemd's boot unlocker, one recovery
    key, then the temporary slot is wiped and the chip alone opens it (tested again)."""
    attach = cryptsetup_attach()
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = RUN_DIR / "temporary.key"
    tmp = secrets.token_hex(32)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, tmp.encode())
    os.close(fd)
    env = dict(os.environ, PASSWORD=tmp)   # systemd-cryptenroll's documented unlock input (works on 252 and 257)
    chip_open = [attach, "attach", MAPPER, dev, "-", "tpm2-device=auto,headless=true"]
    try:
        box.run(["wipefs", "-a", dev])
        box.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--label", "wizard-data",
                 "--key-file", str(key), dev])
        uuid = box.run(["cryptsetup", "luksUUID", dev]).stdout.strip()
        box.run(["systemd-cryptenroll", "--tpm2-device=auto", "--tpm2-pcrs=7", dev], env=env, secret=True)
        box.run(chip_open)
        box.run([attach, "detach", MAPPER])
        r = box.run(["systemd-cryptenroll", "--recovery-key", dev], env=env, secret=True)
        m = re.search(r"\b[a-z]{8}(?:-[a-z]{8}){7}\b", (r.stdout or "") + "\n" + (r.stderr or ""))
        if not m:
            raise Refused("could not read the new recovery key; nothing was moved (run it again)")
        show_recovery_key(m.group(0), tty_in, tty_out)
        m = r = None
        box.run(["systemd-cryptenroll", "--wipe-slot=password", dev], env=env, secret=True)
        box.run(chip_open)
        box.run(["mkfs.ext4", "-q", "-L", "wizard-data", f"/dev/mapper/{MAPPER}"])
        return uuid
    finally:
        env.pop("PASSWORD", None)
        tmp = ""
        try:
            with open(key, "r+b") as f:
                f.write(b"\0" * 64)
        except OSError:
            pass
        key.unlink(missing_ok=True)


def copy_tree(box: Box, src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    st = os.lstat(src)
    os.chown(dst, st.st_uid, st.st_gid)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    box.run(["cp", "-a", "--preserve=all", "--sparse=always", f"{src}/.", str(dst)])


def encrypt(dev: str, *, erase: str | None, backup_file: str | None, extra: list[str], box: Box | None = None,
            tty_in=None, tty_out=None) -> int:
    box = box or Box()
    if os.geteuid() != 0:
        raise Refused("needs root: sudo wam disk encrypt …")
    tty = local_console()
    st = load_state(box)
    if st.get("scope") == "wizard-data" and st.get("stage") not in (None, "rolled-back"):
        raise Refused(f"already done here (stage {st.get('stage')}); see `wam disk status`")
    if st.get("encrypted") and st.get("scope", "root") == "root":
        raise Refused("this box's whole disk is already encrypted")
    from . import disk
    if not disk.chip()["present"]:
        raise Refused("no TPM 2.0 security chip found (it may be turned off in the firmware settings)")
    if erase and erase != dev:
        raise Refused(f"--erase must name the same device ({dev})")
    user = owner()
    paths = data_paths(user, extra)
    need = int(du(paths) * 1.25) + (1 << 30)
    check_device(box, dev, erase == dev, need)
    print("Moving onto an encrypted volume on", dev, ":", ", ".join(map(str, paths)))
    runtipi = next((p for p in paths if (p / "runtipi-cli").exists()), None)

    print("1/5 Taking a backup and checking it …")
    bk = backup(box, user, backup_file)
    st = {"schema": 1, "scope": "wizard-data", "encrypted": True, "chip": "tpm2", "stage": "backup-verified",
          "device": dev, "mount": str(MOUNT), "backup": bk, "started": _now()}
    save_state(box, st)

    tty_in = tty_in or open(tty, "r")
    tty_out = tty_out or open(tty, "w")
    print("2/5 Making the encrypted volume and sealing it to this box's security chip …")
    uuid = make_volume(box, dev, tty_in, tty_out)
    (box.p(DISK_DIR) / "recovery-confirmed").write_text(_now() + "\n")
    st.update(stage="volume-ready", uuid=uuid, pcrs=[7], recovery_confirmed=True, **boot_facts(box))
    save_state(box, st)
    MOUNT.mkdir(mode=0o755, parents=True, exist_ok=True)
    box.run(["chattr", "+i", str(MOUNT)])   # nothing lands on root here while the volume is locked
    box.run(["mount", f"/dev/mapper/{MAPPER}", str(MOUNT)])

    print("3/5 Stopping Wizard and copying its data …")
    stop_wizard(box, user, runtipi)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moves = []
    for i, src in enumerate(paths):
        name = f"{i}-{src.name.lstrip('.') or 'data'}"
        dst = MOUNT / name
        copy_tree(box, src, dst)
        diff = compare(manifest(src), manifest(dst))
        if diff:
            raise Refused(f"the copy of {src} does not match ({len(diff)} differences, first: {diff[0]}); "
                          "Wizard's data is untouched at its old place")
        moves.append({"path": str(src), "name": name, "aside": f"{src}.plain-before-encrypt-{stamp}",
                      "uid": os.lstat(src).st_uid, "gid": os.lstat(src).st_gid,
                      "mode": stat.S_IMODE(os.lstat(src).st_mode)})
    st.update(stage="copied", moves=moves)
    save_state(box, st)

    print("4/5 Switching Wizard to the encrypted copy …")
    for m in moves:
        os.rename(m["path"], m["aside"])
        os.mkdir(m["path"], m["mode"])
        os.chown(m["path"], m["uid"], m["gid"])
        box.run(["chattr", "+i", m["path"]])
    crypt, fs = mount_lines(uuid, moves)
    _write_tab(box, CRYPTTAB, crypt)
    _write_tab(box, FSTAB, fs)
    box.run(["systemctl", "daemon-reload"], check=False)
    for m in moves:
        box.run(["mount", "--bind", str(MOUNT / m["name"]), m["path"]])
    for m in moves:
        diff = compare(manifest(Path(m["aside"])), manifest(Path(m["path"])))
        if diff:
            raise Refused(f"{m['path']} does not match after the switch ({diff[0]}); "
                          "run `sudo wam disk encrypt --rollback` at this box")
    unit_dropins(box, user)
    locked_notice(box)
    held = hold_boot_updates(box)
    st.update(stage="sealed", moved=_now(), held=held)
    save_state(box, st)

    print("5/5 Starting Wizard …")
    start_wizard(box, user, runtipi)
    print("\nDone: Wizard data is encrypted and unlocks with this box's security chip.")
    print("The old unencrypted copies are kept until you confirm:")
    for m in moves:
        print("   ", m["aside"])
    print("Check Wizard works (a restart is a good test), then run `sudo wam disk encrypt --confirm` at this box.")
    print("To undo: `sudo wam disk encrypt --rollback`.")
    return 0


def confirm(box: Box | None = None) -> int:
    box = box or Box()
    if os.geteuid() != 0:
        raise Refused("needs root")
    local_console()
    st = load_state(box)
    if st.get("scope") != "wizard-data" or st.get("stage") != "sealed":
        raise Refused("nothing to confirm")
    if not os.path.ismount(MOUNT):
        raise Refused("the encrypted volume is not unlocked right now; unlock it first (sudo wam disk unlock)")
    for m in st.get("moves", []):
        if not os.path.ismount(m["path"]):
            raise Refused(f"{m['path']} is not on the encrypted volume right now")
        aside = Path(m["aside"])
        if aside.exists():
            box.run(["rm", "-rf", "--one-file-system", str(aside)])
    box.run(["fstrim", "-a"], check=False)
    st.update(confirmed=_now(), old_copies_deleted=True)
    save_state(box, st)
    print("Old unencrypted copies deleted and free space trimmed.")
    return 0


def rollback(box: Box | None = None) -> int:
    box = box or Box()
    if os.geteuid() != 0:
        raise Refused("needs root")
    local_console()
    st = load_state(box)
    if st.get("scope") != "wizard-data" or st.get("stage") not in ("copied", "sealed"):
        raise Refused("nothing to roll back")
    user = owner()
    moves = st.get("moves", [])
    if st["stage"] == "sealed" and not os.path.ismount(MOUNT):
        raise Refused("unlock the encrypted volume first (sudo wam disk unlock): rollback copies its current data")
    runtipi = next((Path(m["path"]) for m in moves if (MOUNT / m["name"] / "runtipi-cli").exists()), None)
    stop_wizard(box, user, runtipi)
    for m in moves:
        path, src = Path(m["path"]), MOUNT / m["name"]
        if os.path.ismount(path):
            box.run(["umount", str(path)])
        if path.exists() and not any(path.iterdir()):
            box.run(["chattr", "-i", str(path)], check=False)
            path.rmdir()
        elif path.exists():
            continue   # stage "copied": the old folder was never switched
        back = Path(f"{path}.rollback-{time.strftime('%Y%m%d-%H%M%S')}")
        copy_tree(box, src, back)
        diff = compare(manifest(src), manifest(back))
        if diff:
            raise Refused(f"the copy back of {path} does not match ({diff[0]}); the encrypted volume is untouched")
        os.rename(back, path)
    _write_tab(box, FSTAB, None)
    _write_tab(box, CRYPTTAB, None)
    unit_dropins(box, user, remove=True)
    locked_notice(box, remove=True)
    box.run(["systemctl", "daemon-reload"], check=False)
    if os.path.ismount(MOUNT):
        box.run(["umount", str(MOUNT)])
    box.run(["chattr", "-i", str(MOUNT)], check=False)
    box.run(["cryptsetup", "close", MAPPER], check=False)
    st.update(stage="rolled-back", encrypted=False, rolled_back=_now())
    save_state(box, st)
    (box.p(DISK_DIR) / "recovery-confirmed").unlink(missing_ok=True)
    start_wizard(box, user, runtipi)
    print("Rolled back: Wizard data is back on the system disk, unencrypted. The encrypted volume was left as it is.")
    for m in moves:
        if Path(m["aside"]).exists():
            print("   older copy from before encrypting (stale, safe to delete):", m["aside"])
    return 0


def unlock(box: Box | None = None) -> int:
    """The chip refused: unlock with the recovery key typed at the box, then mount and start Wizard."""
    box = box or Box()
    if os.geteuid() != 0:
        raise Refused("needs root")
    local_console()
    st = load_state(box)
    if st.get("scope") != "wizard-data" or not st.get("uuid"):
        raise Refused("no encrypted Wizard data volume on this box")
    if not Path(f"/dev/mapper/{MAPPER}").exists():
        print("Type the recovery key (the 8 groups of letters) and press Enter.")
        box.run(["cryptsetup", "open", f"/dev/disk/by-uuid/{st['uuid']}", MAPPER], capture=False)
    box.run(["systemctl", "start", "systemd-fsck@dev-mapper-wizard_data.service"], check=False)
    box.run(["mount", str(MOUNT)], check=False)
    for m in st.get("moves", []):
        if not os.path.ismount(m["path"]):
            box.run(["mount", m["path"]], check=False)
    box.p("/run/issue.d/wizard-data-locked.issue").unlink(missing_ok=True)
    box.p("/run/motd.d/wizard-data-locked").unlink(missing_ok=True)
    user = owner()
    start_wizard(box, user, next((Path(m["path"]) for m in st.get("moves", [])
                                  if (Path(m["path"]) / "runtipi-cli").exists()), None))
    print("Unlocked. If the security chip refused because of a deliberate change (firmware update, Secure Boot"
          " setting), run `sudo wam disk reseal` here so the next start unlocks by itself.")
    return 0


def sealed_device(box: Box, st: dict) -> str:
    """The LUKS device this box seals to its chip: the installer's root volume or the moved Wizard data."""
    if st.get("scope") == "wizard-data" and st.get("uuid"):
        return f"/dev/disk/by-uuid/{st['uuid']}"
    vol = _read(box.p(DISK_DIR / "volume"))
    m = re.search(r"^DEV=(\S+)$", vol, re.M)
    if st.get("chip") == "tpm2" and st.get("encrypted") and m:
        return m.group(1)
    raise Refused("this box has no disk sealed to its security chip")


def reseal(box: Box | None = None) -> int:
    """After a deliberate firmware or Secure Boot change: seal a fresh chip slot to how this box starts now.
    The recovery key is typed into cryptsetup's own prompt at the box (never an argument, file or agent)."""
    box = box or Box()
    if os.geteuid() != 0:
        raise Refused("needs root")
    local_console()
    st = load_state(box)
    dev = sealed_device(box, st)
    if box.p(DISK_DIR / "install.key").exists():
        raise Refused("first-run disk setup has not finished: restart and finish it on this box's screen")
    from . import disk
    if not disk.chip().get("present"):
        raise Refused("no TPM 2.0 security chip is visible: turn it on in the firmware settings first")
    print("Sealing the disk to this box's security chip as it starts now.")
    print("Type the recovery key (the 8 groups of letters) and press Enter.")
    box.run(["systemd-cryptenroll", "--wipe-slot=tpm2", "--tpm2-device=auto", "--tpm2-pcrs=7", dev], capture=False)
    if box.run(["cryptsetup", "open", "--test-passphrase", "--token-only", dev], check=False).returncode != 0:
        raise Refused("the new chip slot does not open the disk; the recovery key still does. Nothing else changed")
    st.update(boot_facts(box), stage="sealed-wizard-data" if st.get("scope") == "wizard-data" else "sealed",
              reason="", sealed_at=_now(), pcrs=[7], secure_boot=disk.secure_boot() is True)
    save_state(box, st)
    print("Done: the disk is sealed to this box's security chip again. The next start unlocks by itself.")
    return 0


def _write_tab(box: Box, path: Path, lines: list[str] | None) -> None:
    p = box.p(path)
    text = _read(p)
    new = tab_block(text, lines)
    if new != text:
        tmp = p.with_suffix(".wizard-tmp")
        tmp.write_text(new)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wam disk encrypt", description=__doc__.split("\n\n")[0])
    ap.add_argument("--device", help="the empty drive or partition for the encrypted volume, e.g. /dev/nvme1n1")
    ap.add_argument("--erase", metavar="DEVICE", help="erase DEVICE even though it is not empty (type it again)")
    ap.add_argument("--backup-file", help="use and verify this existing `wam backup export` file")
    ap.add_argument("--path", action="append", default=[], help="another Wizard data folder to move")
    ap.add_argument("--confirm", action="store_true", help="delete the old unencrypted copies")
    ap.add_argument("--rollback", action="store_true", help="copy the data back and remove the encrypted mounts")
    a = ap.parse_args(argv)
    if a.confirm:
        return confirm()
    if a.rollback:
        return rollback()
    if not a.device:
        ap.error("--device is required")
    return encrypt(a.device, erase=a.erase, backup_file=a.backup_file, extra=a.path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
