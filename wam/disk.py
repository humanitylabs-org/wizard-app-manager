"""Disk encryption status: is the owner's data encrypted, and does this box's security chip unlock it?

wizard-os spec/DISK_ENCRYPTION.md (decision #112: chip-only, TPM2 sealed to PCR 7, one recovery key).
`wam disk status [--json]` and Settings → Network & security (store-install/_disk) read this. It is read-only:
it never reads key material, never runs cryptsetup with a key and never writes anything.

Sources, all public:
  /etc/wizard/disk/state.json        written by the installer's first-boot seal (wizard-os-iso) and `wam disk reseal`
  /etc/wizard/disk/install.key       the temporary install key: its presence alone means "not protected yet"
  /etc/wizard/disk/recovery-confirmed the owner typed back the start of the recovery key
  lsblk / findmnt                    whether the volume holding Wizard data sits on a LUKS (crypt) device
  /sys/class/tpm, /sys/firmware      the chip, its current PCR 7, Secure Boot and the Secure Boot variables
  cryptsetup luksDump (root only)    which key slots and tokens exist (metadata, never keys)
  fdesetup status (macOS) or the Mac host's /run/wizard-host/disk.json (a Wizard guest on a Mac)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DISK_DIR = Path("/etc/wizard/disk")
INSTALL_INFO = Path("/etc/wizard/install-info")
MAC_HOST_FILE = Path("/run/wizard-host/disk.json")
MAC_GUEST_MARK = Path("/var/lib/wizard/mac-files")   # try-wizard's guest mounts the Mac's files here
BIOS_VERSION = Path("/sys/class/dmi/id/bios_version")
TPM = Path("/sys/class/tpm/tpm0")
EFIVARS = Path("/sys/firmware/efi/efivars")
GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
IMAGE_SECURITY_GUID = "d719b2cb-3d3a-4596-a3bc-dad00e67656f"
SHIM_LOCK_GUID = "605dab50-e046-4300-abb6-3dd810dd8b23"
# name recorded in state.json -> the efivars file it hashes (the same files wizard-os-iso's seal hashes).
# SbatLevel itself is boot-services only; after boot the OS sees shim's runtime copy, SbatLevelRT.
SB_VARS = ("PK", "KEK", "db", "dbx", "SbatLevel")
SB_FILES = {"PK": f"PK-{GLOBAL_GUID}", "KEK": f"KEK-{GLOBAL_GUID}", "db": f"db-{IMAGE_SECURITY_GUID}",
            "dbx": f"dbx-{IMAGE_SECURITY_GUID}", "SbatLevel": f"SbatLevelRT-{SHIM_LOCK_GUID}"}

UPDATE_NOTE = "The next restart may ask for the recovery key."
WAITING_NOTE = "A boot security update is waiting: install it at this box."
SB_OFF_NOTE = "Secure Boot is off: turn it on for stronger protection."

LINES = {
    "not-protected-yet": ("Not protected yet: the disk key is still stored on this box. Finish setup on this box's "
                          "screen (if it keeps failing, check the security chip is turned on in the firmware settings)",
                          "error"),
    "chip-mismatch": ("This box's security chip no longer matches: run `wam disk reseal` at this box", "warn"),
    "sealed": ("Disk encrypted, unlocks with this box's security chip", "ok"),
    "sealed-wizard-data": ("Wizard data encrypted, unlocks with this box's security chip", "ok"),
    "passphrase": ("Disk encrypted, asks for a passphrase at every start", "ok"),
    "no-chip": ("Not encrypted: no security chip found (it may be turned off in the firmware settings)", "warn"),
    "plain": ("Not encrypted", "warn"),
    "vps": ("Not encrypted by Wizard: relies on your provider's disk encryption", "warn"),
    "mac-on": ("Protected by FileVault on this Mac", "ok"),
    "mac-off": ("Not encrypted: FileVault is off on this Mac", "warn"),
    "mac-unknown": ("Couldn't check FileVault on this Mac", "warn"),
    "unknown": ("Couldn't check disk encryption on this box", "warn"),
}


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _read(p: Path) -> str | None:
    try:
        return p.read_text()
    except OSError:
        return None


def _json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def secure_boot(efivars: Path | None = None) -> bool | None:
    efivars = efivars or EFIVARS
    try:
        b = (efivars / f"SecureBoot-{GLOBAL_GUID}").read_bytes()
        return len(b) >= 5 and b[4] == 1
    except OSError:
        return None if not efivars.is_dir() else False


def efivar_hashes(efivars: Path | None = None) -> dict[str, str]:
    efivars = efivars or EFIVARS
    out = {}
    for name in SB_VARS:
        try:
            out[name] = hashlib.sha256((efivars / SB_FILES[name]).read_bytes()).hexdigest()
        except OSError:
            out[name] = ""
    return out


def chip(tpm: Path | None = None) -> dict:
    tpm = tpm or TPM
    major = (_read(tpm / "tpm_version_major") or "").strip()
    pcr7 = (_read(tpm / "pcr-sha256" / "7") or "").strip().lower()
    return {"present": major == "2", "version": major or None, "pcr7_sha256": pcr7 or None}


def crypt_parents(path: str) -> list[dict]:
    """The LUKS devices under the filesystem holding `path` (lsblk inverse tree), empty when it is plain."""
    rc, src = _run(["findmnt", "-no", "SOURCE", "--target", path])
    src = src.strip().splitlines()[0] if rc == 0 and src.strip() else ""
    src = src.split("[", 1)[0]   # bind mounts and btrfs subvolumes: /dev/mapper/x[/@home]
    if not src.startswith("/dev/"):
        return []
    rc, out = _run(["lsblk", "-J", "-s", "-p", "-o", "NAME,TYPE,FSTYPE,UUID", src])
    if rc != 0:
        return []
    found: list[dict] = []

    def walk(nodes):
        for n in nodes or []:
            if n.get("fstype") == "crypto_LUKS":
                found.append({"device": n.get("name"), "uuid": n.get("uuid")})
            walk(n.get("children"))
    try:
        walk(json.loads(out).get("blockdevices"))
    except ValueError:
        return []
    return found


def luks_meta(device: str, allow_sudo: bool = True) -> dict | None:
    """Key slot and token types from the LUKS2 header (metadata only). Needs root (or `sudo -n`); None otherwise."""
    cmd = ["cryptsetup", "luksDump", "--dump-json-metadata", device]
    if os.geteuid() != 0:
        if not allow_sudo or not shutil.which("sudo"):
            return None
        cmd = ["sudo", "-n", *cmd]
    rc, out = _run(cmd)
    if rc != 0 or "{" not in out:
        return None
    try:
        d = json.loads(out[out.index("{"):])
    except ValueError:
        return None
    tokens = [{"type": t.get("type"), "pcrs": t.get("tpm2-pcrs"), "keyslots": t.get("keyslots")}
              for t in (d.get("tokens") or {}).values() if isinstance(t, dict)]
    return {"keyslots": len(d.get("keyslots") or {}), "tokens": tokens,
            "tpm2_pcrs": next((t["pcrs"] for t in tokens if t["type"] == "systemd-tpm2"), None),
            "recovery": any(t["type"] == "systemd-recovery" for t in tokens)}


def boot_updates(state: dict) -> dict:
    """Boot security changes since the seal (shim, firmware, Secure Boot variables) and a held shim update waiting."""
    changed = []
    rc, shim = _run(["dpkg-query", "-W", "-f=${Version}", "shim-signed"])
    shim = shim.strip() if rc == 0 else ""
    if state.get("shim_version") is not None and shim != state.get("shim_version"):
        changed.append("shim")
    bios = (_read(BIOS_VERSION) or "").strip().replace('"', "").replace("\\", "")
    sealed_bios = str(state.get("bios_version") or "").strip().replace('"', "").replace("\\", "")
    if state.get("bios_version") is not None and bios and bios != sealed_bios:
        changed.append("firmware")
    sealed_vars = state.get("efivars_sha256") or {}
    now = efivar_hashes()
    for name in SB_VARS:
        if sealed_vars.get(name) is not None and now.get(name, "") != sealed_vars.get(name):
            changed.append(name)
    waiting = False
    rc, pol = _run(["apt-cache", "policy", "shim-signed"])
    if rc == 0:
        inst = cand = ""
        for line in pol.splitlines():
            s = line.strip()
            if s.startswith("Installed:"):
                inst = s.split(":", 1)[1].strip()
            elif s.startswith("Candidate:"):
                cand = s.split(":", 1)[1].strip()
        waiting = bool(inst and cand and inst != "(none)" and cand != "(none)" and inst != cand)
    return {"changed_since_seal": changed, "update_waiting": waiting}


def _kind(wizard_home: Path | None) -> str:
    if sys.platform == "darwin":
        return "mac"
    if MAC_HOST_FILE.is_file():
        return "mac-guest"
    if "installer=wizard-os-iso" in (_read(INSTALL_INFO) or ""):
        return "dedicated"
    try:
        from . import alongside
        if wizard_home is not None and alongside.load(wizard_home):
            return "alongside"
    except Exception:  # noqa: BLE001 - status must answer even if alongside state is unreadable
        pass
    rc, virt = _run(["systemd-detect-virt", "--vm"])
    if virt.strip() == "apple" or MAC_GUEST_MARK.exists():
        return "mac-guest"   # try-wizard's Linux VM on a Mac: the Mac's FileVault protects it, not this guest
    if rc == 0 and virt.strip() and virt.strip() != "none":
        return "vps"
    return "linux"


def _mac(kind: str) -> dict:
    if kind == "mac":
        rc, out = _run(["fdesetup", "status"])
        fv = "on" if "FileVault is On" in out else "off" if "FileVault is Off" in out else "unknown"
    else:
        fv = str(_json(MAC_HOST_FILE).get("filevault") or "unknown")
        fv = fv if fv in ("on", "off") else "unknown"
    state = {"on": "mac-on", "off": "mac-off"}.get(fv, "mac-unknown")
    return {"platform": kind, "filevault": fv, "encrypted": fv == "on" if fv != "unknown" else None, "state": state}


def status(wizard_home: Path | None = None, data_path: str | None = None, allow_sudo: bool = True) -> dict:
    """One read-only answer. `state` keys into LINES; `line` is the plain sentence Settings shows."""
    kind = _kind(wizard_home)
    if kind in ("mac", "mac-guest"):
        res = _mac(kind)
        res["line"], res["level"] = LINES[res["state"]]
        res["notes"] = []
        return res

    if kind == "dedicated" and not os.access(DISK_DIR, os.R_OK | os.X_OK):
        # The installer makes it 0755; if this check cannot read it, say so rather than guess "Not protected yet" away.
        line, level = LINES["unknown"]
        return {"platform": kind, "state": "unknown", "line": line, "level": level, "notes": [], "encrypted": None,
                "header_read": False}
    disk = _json(DISK_DIR / "state.json")
    tpm = chip()
    sb = secure_boot()
    temp_key = (DISK_DIR / "install.key").exists()
    confirmed = (DISK_DIR / "recovery-confirmed").exists()
    scope = disk.get("scope") or ("wizard-data" if kind == "alongside" else "root")
    path = data_path or (disk.get("mount") if scope == "wizard-data" else None) or \
        (str(wizard_home) if wizard_home else "/")
    parents = crypt_parents(path)
    meta = luks_meta(parents[0]["device"], allow_sudo) if parents else None
    sealed_meta = meta["tpm2_pcrs"] is not None if meta else None
    sealed = sealed_meta if sealed_meta is not None else disk.get("stage") == "sealed"
    recovery = meta["recovery"] if meta else bool(disk.get("recovery_confirmed") or confirmed)
    pcr_now, pcr_sealed = tpm["pcr7_sha256"], (disk.get("pcr7_sha256") or "").lower() or None
    mismatch = bool(sealed and pcr_now and pcr_sealed and pcr_now != pcr_sealed)

    notes: list[str] = []
    if temp_key:
        state = "not-protected-yet"
    elif parents and (disk.get("stage") == "reseal-failed" or (sealed and mismatch)):
        state = "chip-mismatch"   # reseal-failed: the old chip slot is gone and the new one did not test
    elif parents and sealed:
        state = "sealed-wizard-data" if scope == "wizard-data" else "sealed"
    elif parents:
        state = "passphrase"
    elif kind == "vps":
        state = "vps"
    elif disk.get("reason") == "no-security-chip" or not tpm["present"]:
        state = "no-chip"
    elif kind in ("dedicated", "alongside", "linux"):
        state = "plain"
    else:
        state = "unknown"

    updates = {"changed_since_seal": [], "update_waiting": False}
    if state in ("sealed", "sealed-wizard-data", "chip-mismatch"):
        if sb is False:
            notes.append(SB_OFF_NOTE)
        updates = boot_updates(disk)
        if updates["changed_since_seal"] and state != "chip-mismatch":
            notes.append(UPDATE_NOTE)
        if updates["update_waiting"]:
            notes.append(WAITING_NOTE)

    line, level = LINES[state]
    if notes:
        line = line.rstrip(".") + ". " + " ".join(notes)
    return {
        "platform": kind, "state": state, "line": line, "level": level, "notes": notes,
        "encrypted": bool(parents), "scope": scope if parents else None,
        "chip": {"present": tpm["present"], "sealed": bool(sealed) if parents else False,
                 "pcrs": (meta or {}).get("tpm2_pcrs") or disk.get("pcrs"), "matches": not mismatch if sealed else None},
        "recovery_key": {"enrolled": bool(recovery) if parents else False, "confirmed_saved": confirmed},
        "temporary_key_present": temp_key,
        "secure_boot": sb,
        "boot_updates": updates,
        "seal_stage": disk.get("stage"), "seal_reason": disk.get("reason") or None,
        "header_read": meta is not None,
    }


def describe(res: dict) -> str:
    out = [res["line"]]
    if res.get("platform") in ("mac", "mac-guest"):
        out.append(f"  FileVault: {res.get('filevault')}")
        return "\n".join(out)
    chip_ = res.get("chip") or {}
    yn = lambda v: "yes" if v else "no" if v is not None else "unknown"  # noqa: E731
    out += [f"  encrypted: {yn(res.get('encrypted'))}" + (f" ({res['scope']})" if res.get("scope") else ""),
            f"  sealed to the security chip: {yn(chip_.get('sealed'))}"
            + (f" (PCR {','.join(map(str, chip_['pcrs']))})" if chip_.get("pcrs") else ""),
            f"  recovery key enrolled: {yn(res['recovery_key']['enrolled'])}, "
            f"saved by the owner: {yn(res['recovery_key']['confirmed_saved'])}",
            f"  temporary install key still present: {yn(res.get('temporary_key_present'))}",
            f"  Secure Boot: {'on' if res.get('secure_boot') else 'off' if res.get('secure_boot') is False else 'unknown'}"]
    if not res.get("header_read") and res.get("encrypted"):
        out.append("  (key slots not read: run as root for the LUKS header check)")
    return "\n".join(out)


def public(res: dict) -> dict:
    """What the Settings route returns: the line and the yes/no facts, nothing about devices or hashes."""
    keep = ("platform", "state", "line", "level", "encrypted", "secure_boot", "temporary_key_present", "filevault")
    out = {k: res.get(k) for k in keep if k in res}
    if "chip" in res:
        out["chip"] = {"present": res["chip"]["present"], "sealed": res["chip"]["sealed"]}
        out["recovery_key"] = dict(res["recovery_key"])
    return out
