#!/usr/bin/env python3
"""Throwaway-VM proof of `wam disk encrypt` (wizard-os spec/DISK_ENCRYPTION.md, existing / alongside boxes).

Linux x86-64 host with KVM, swtpm, Debian's 4M OVMF (Secure Boot code + Microsoft-key vars), cloud-localds and a
Debian genericcloud qcow2. Boots a copy of the image with a software TPM 2.0 and an empty second drive, plants a
small fake Wizard (Wizard home with a canary, a Runtipi root with app data, a 64 MB owner file, a symlink, a user
unit like Wizard's), then drives the box's serial console as the owner would at the screen:

  move      `sudo wam disk encrypt --device /dev/vdb`: recovery key shown once, a wrong answer refused, the right
            first group accepted; data now on the TPM-sealed volume, bind-mounted back, old copy aside, files equal
  a         cold restart: unlocks with no prompt, Wizard's unit runs, `wam disk status` says sealed
  confirm   `--confirm` deletes the old copies
  c         Secure Boot off (other firmware vars): the chip withholds the key, the box still boots, Wizard's unit
            does not start, the empty mount point refuses writes, the console notice shows; `wam disk unlock`
            with the recovery key brings it back; `wam disk reseal` at the box, restart: no key needed; Secure
            Boot on again: withheld, unlock + reseal, restart: no key needed
  b         another chip: locked as in c; powered off, the encrypted drive's image does not contain the canary
  rollback  `--rollback`: data back on root (files equal), mounts gone; restart: Wizard runs, status Not encrypted

Usage: disk_encrypt_vm.py BASE.qcow2 [--out DIR] [--verify-only]
Everything it starts is stopped before it exits. The console password and the recovery key are random, in
memory only, and scrubbed from every saved file.
"""
import argparse
import atexit
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OVMF = os.environ.get("WIZARD_OVMF_DIR", "/usr/share/OVMF")
CODE_SB, VARS_SB = f"{OVMF}/OVMF_CODE_4M.secboot.fd", f"{OVMF}/OVMF_VARS_4M.ms.fd"
VARS_NOSB = f"{OVMF}/OVMF_VARS_4M.fd"   # same Secure Boot-capable code, no keys enrolled: Secure Boot off
RECOVERY_RE = re.compile(r"\b([cbdefghijklnrtuv]{8}(?:-[cbdefghijklnrtuv]{8}){7})\b")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|[\x00-\x08\x0e-\x1f]")
PROMPTS = ("Please enter passphrase", "Enter passphrase for")

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("--out", default=os.path.join(HERE, "results", "disk-encrypt-" + time.strftime("%Y%m%d-%H%M%S")))
ap.add_argument("--verify-only", action="store_true")
args = ap.parse_args()

if args.verify_only:
    probs = [f"missing tool {t}" for t in ("qemu-system-x86_64", "qemu-img", "swtpm", "cloud-localds", "ssh",
                                           "ssh-keygen", "scp") if not shutil.which(t)]
    probs += [f"missing firmware {f}" for f in (CODE_SB, VARS_SB, VARS_NOSB) if not os.path.isfile(f)]
    probs += [] if os.access("/dev/kvm", os.R_OK | os.W_OK) else ["no read/write access to /dev/kvm"]
    probs += [] if os.path.isfile(args.base) else [f"missing base image {args.base}"]
    probs += [f"missing {f}" for f in ("wam/disk_encrypt.py", "wam/disk.py", "wam/__main__.py")
              if not os.path.isfile(os.path.join(REPO, f))]
    st = os.statvfs(os.path.dirname(os.path.abspath(args.out)) or ".")
    free = st.f_bavail * st.f_frsize / 2**30
    probs += [] if free >= 20 else [f"only {free:.0f} GB free (need 20)"]
    print("verify-only:", "OK" if not probs else "PROBLEMS", f"(free {free:.0f} GB)")
    for p in probs:
        print(" -", p)
    sys.exit(1 if probs else 0)

R = os.path.abspath(args.out)
os.makedirs(R, exist_ok=False)
PW = secrets.token_urlsafe(18)
SECRETS = [PW]
CANARY = "wizard-canary-" + secrets.token_hex(16)
report = {"checks": {}, "verdicts": {}}
PROCS = []


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    for s in SECRETS:
        line = line.replace(s, "<redacted>")
    print(line, flush=True)
    with open(os.path.join(R, "run.log"), "a") as f:
        f.write(line + "\n")


def save():
    with open(os.path.join(R, "report.json"), "w") as f:
        json.dump(report, f, indent=2)


def verdict(name, ok, why=""):
    report["verdicts"][name] = {"pass": bool(ok), "why": why}
    log("VERDICT", name, "PASS" if ok else "FAIL", why)
    save()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
KEY = os.path.join(R, "id_ed25519")
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", KEY], check=True)
SSH = ["ssh", "-i", KEY, "-p", str(PORT), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "owner@127.0.0.1"]


def ssh(cmd, timeout=900):
    try:
        r = subprocess.run(SSH + [cmd], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


class Console:
    def __init__(self, path):
        self.text, self.mark, self.lock, self.sock = "", 0, threading.Lock(), None
        for _ in range(150):
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(path)
                self.sock = s
                break
            except OSError:
                time.sleep(0.2)
        if not self.sock:
            raise RuntimeError("no serial console")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                c = self.sock.recv(65536)
            except OSError:
                return
            if not c:
                return
            with self.lock:
                self.text = (self.text + ANSI.sub(" ", c.decode("latin-1")))[-400000:]

    def since(self):
        with self.lock:
            return self.text[self.mark:]

    def reset(self):
        with self.lock:
            self.mark = len(self.text)

    def expect(self, pats, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            seen = self.since()
            for p in pats:
                m = p.search(seen) if hasattr(p, "search") else (p if p in seen else None)
                if m:
                    return m
            time.sleep(0.5)
        return None

    def send(self, s):
        self.sock.sendall(s.encode())


def swtpm(state):
    os.makedirs(state, exist_ok=True)
    sock = os.path.join(state, "swtpm.sock")
    if os.path.exists(sock):
        os.remove(sock)
    p = subprocess.Popen(["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={state}", "--ctrl",
                          f"type=unixio,path={sock}", "--terminate"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    PROCS.append(p)
    for _ in range(100):
        if os.path.exists(sock):
            return sock
        time.sleep(0.1)
    raise RuntimeError("swtpm did not start")


SYS, DATA, SEED = os.path.join(R, "sys.qcow2"), os.path.join(R, "data.qcow2"), os.path.join(R, "seed.img")
VARS_ON, VARS_OFF = os.path.join(R, "vars-sb.fd"), os.path.join(R, "vars-nosb.fd")
TPM_A, TPM_B = os.path.join(R, "tpm-a"), os.path.join(R, "tpm-b")
VM = {"p": None, "con": None, "n": 0}


def boot(vars_path, tpm_dir):
    VM["n"] += 1
    name = f"boot{VM['n']}"
    ser = os.path.join(R, f"ser-{name}.sock")
    cmd = ["qemu-system-x86_64", "-machine", "q35,smm=on", "-accel", "kvm", "-cpu", "host", "-smp", "2", "-m", "2048",
           "-name", f"wizard-disk-qa-{name}", "-global", "driver=cfi.pflash01,property=secure,value=on",
           "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={CODE_SB}",
           "-drive", f"if=pflash,format=raw,unit=1,file={vars_path}",
           "-drive", f"file={SYS},if=virtio,format=qcow2,discard=unmap",
           "-drive", f"file={DATA},if=virtio,format=qcow2,discard=unmap",
           "-drive", f"file={SEED},if=virtio,format=raw,readonly=on",
           "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{PORT}-:22", "-device", "virtio-net-pci,netdev=n0",
           "-display", "none", "-chardev", f"socket,id=s0,path={ser},server=on,wait=off,logfile={R}/serial-{name}.log",
           "-serial", "chardev:s0"]
    if tpm_dir:
        cmd += ["-chardev", f"socket,id=chrtpm,path={swtpm(tpm_dir)}", "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
                "-device", "tpm-tis,tpmdev=tpm0"]
    log("boot", name, os.path.basename(vars_path), os.path.basename(tpm_dir or "no-tpm"))
    VM["p"] = subprocess.Popen(cmd, stdout=open(os.path.join(R, f"qemu-{name}.out"), "w"), stderr=subprocess.STDOUT,
                               start_new_session=True)
    PROCS.append(VM["p"])
    VM["con"] = Console(ser)
    return VM["con"]


def wait_ssh(budget=600):
    t0 = time.time()
    while time.time() - t0 < budget and VM["p"].poll() is None:
        if ssh("true", 30)[0] == 0:
            return True
        time.sleep(5)
    return False


def poweroff():
    ssh("sudo systemctl poweroff", 30)
    try:
        VM["p"].wait(180)
    except subprocess.TimeoutExpired:
        VM["p"].kill()
        VM["p"].wait()
    for p in PROCS:
        if p.poll() is None and "swtpm" in " ".join(getattr(p, "args", [])):
            p.terminate()


def cleanup():
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
    for root, _, files in os.walk(R):   # scrub the console password and the recovery key from everything kept
        for n in files:
            path = os.path.join(root, n)
            if n.endswith((".qcow2", ".fd", ".img", ".raw")) or "tpm-" in root:
                continue
            try:
                data = open(path, "rb").read()
            except OSError:
                continue
            new = data
            for s in SECRETS:
                if s:
                    new = new.replace(s.encode(), b"<redacted>")
            if new != data:
                open(path, "wb").write(new)
    for n in ("sys.qcow2", "data.qcow2", "data.raw"):
        try:
            os.remove(os.path.join(R, n))
        except OSError:
            pass
    shutil.rmtree(TPM_A, ignore_errors=True)
    shutil.rmtree(TPM_B, ignore_errors=True)


atexit.register(cleanup)


def login(con):
    con.reset()
    con.send("\r")
    if not con.expect([re.compile(r"login:\s*$", re.M)], 120):
        raise RuntimeError("no login prompt on the serial console")
    con.send("owner\r")
    con.expect(["Password:"], 30)
    con.send(PW + "\r")
    if not con.expect([re.compile(r"owner@\S+:~\$\s*$", re.M)], 60):
        raise RuntimeError("serial login failed")
    con.send("export PS1='SH$ '\r")
    con.expect([re.compile(r"^SH\$ ", re.M)], 20)


def at_box(con, cmd, pats, timeout=900):
    con.reset()
    con.send(cmd + "\r")
    return con.expect(pats, timeout)


WAM = "cd /opt/wam && sudo env WIZARD_DISK_WAM=/opt/fakewam PYTHONPATH=/opt/wam python3 -m wam disk"


def status():
    rc, out = ssh("cd /opt/wam && sudo env PYTHONPATH=/opt/wam python3 -m wam disk status --json")
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return {"raw": out[-400:]}


def unit_active():
    return ssh("systemctl --user is-active wizard-app-manager-api.service")[1].strip()


def canary_ok():
    rc, out = ssh("cat ~/.wizard-os/vault.json ~/runtipi/app-data/demo/data/state.txt; "
                  "sha256sum -c --quiet ~/media.sha256 && echo MEDIA-OK; readlink ~/runtipi/latest")
    return CANARY in out and out.count(CANARY) == 2 and "MEDIA-OK" in out and "app-data/demo" in out


def main():
    log("out", R)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", os.path.abspath(args.base), SYS, "12G"],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", DATA, "6G"], check=True, stdout=subprocess.DEVNULL)
    shutil.copy(VARS_SB, VARS_ON)
    shutil.copy(VARS_NOSB, VARS_OFF)
    pub = open(KEY + ".pub").read().strip()
    ud = os.path.join(R, "user-data")
    with open(ud, "w") as f:
        f.write(f"""#cloud-config
users:
  - name: owner
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    ssh_authorized_keys: [{json.dumps(pub)}]
chpasswd:
  expire: false
  users: [{{name: owner, password: {json.dumps(PW)}, type: text}}]
ssh_pwauth: false
""")
    subprocess.run(["cloud-localds", SEED, ud], check=True)
    os.remove(ud)
    tarball = os.path.join(R, "wam.tar")
    with tarfile.open(tarball, "w") as t:
        t.add(os.path.join(REPO, "wam"), arcname="wam/wam", filter=lambda ti: None if "__pycache__" in ti.name else ti)

    # ---- prepare the box: the generic kernel (TPM drivers), cryptsetup, tpm2, a trusted serial console
    boot(VARS_ON, TPM_A)
    if not wait_ssh(900):
        return verdict("setup", False, "no SSH after first boot")
    rc, out = ssh("sudo DEBIAN_FRONTEND=noninteractive apt-get -qq update && sudo DEBIAN_FRONTEND=noninteractive "
                  "apt-get -qq install -y linux-image-amd64 cryptsetup-bin tpm2-tools libtss2-tcti-device0t64 systemd-cryptsetup python3-yaml e2fsprogs "
                  ">/dev/null && sudo DEBIAN_FRONTEND=noninteractive apt-get -qq purge -y 'linux-image-*-cloud-amd64' "
                  "linux-image-cloud-amd64 >/dev/null; echo 'GRUB_CMDLINE_LINUX_DEFAULT=\"$GRUB_CMDLINE_LINUX_DEFAULT "
                  "wizard.recovery_console=ttyS0\"' | sudo tee /etc/default/grub.d/99-wizard-qa.cfg >/dev/null && "
                  "sudo update-grub 2>&1 | tail -1", 1800)
    log("prepare", rc, out[-300:])
    poweroff()
    boot(VARS_ON, TPM_A)
    if not wait_ssh(600):
        return verdict("setup", False, "no SSH after the kernel switch")
    rc, info = ssh("uname -r; cat /sys/class/tpm/tpm0/tpm_version_major; od -An -tx1 "
                   "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c; systemctl --version | head -1")
    report["checks"]["box"] = info
    log("box", info.replace("\n", " | "))
    if "\n2\n" not in f"\n{info}\n" or not re.search(r"\b01\s*$", info.splitlines()[2] if len(info.splitlines()) > 2 else ""):
        return verdict("setup", False, "no TPM 2.0 or Secure Boot off: " + info)
    subprocess.run(["scp", "-q", "-i", KEY, "-P", str(PORT), "-o", "StrictHostKeyChecking=no", "-o",
                    "UserKnownHostsFile=/dev/null", tarball, "owner@127.0.0.1:/tmp/wam.tar"], check=True)
    rc, out = ssh(f"""set -e
sudo mkdir -p /opt/wam && sudo tar -xf /tmp/wam.tar -C /opt/wam --strip-components=1
printf '#!/bin/sh\\n# qa stand-in for `wam backup`: export = tar of the data, verify = the tar reads back\\n'\\
'case "$2" in export) tar -C "$HOME" -cf "$4" .wizard-os runtipi;; verify) tar -tf "$3" >/dev/null && echo verified;; esac\\n' | sudo tee /opt/fakewam >/dev/null
sudo chmod 755 /opt/fakewam
mkdir -p ~/.wizard-os ~/runtipi/app-data/demo/data ~/runtipi/media ~/.config/systemd/user
printf '{{"secret": "{CANARY}"}}\\n' > ~/.wizard-os/vault.json && chmod 600 ~/.wizard-os/vault.json
echo '{CANARY}' > ~/runtipi/app-data/demo/data/state.txt
head -c 64M /dev/urandom > ~/runtipi/media/photo.bin && (cd ~ && sha256sum runtipi/media/photo.bin > media.sha256)
ln -s app-data/demo ~/runtipi/latest
printf '[Unit]\\nDescription=stand-in for Wizard\\n[Service]\\nWorkingDirectory=%%h/.wizard-os\\nExecStart=/bin/sh -c "while sleep 30; do date > %%h/.wizard-os/alive; done"\\n[Install]\\nWantedBy=default.target\\n' > ~/.config/systemd/user/wizard-app-manager-api.service
systemctl --user daemon-reload && systemctl --user enable --now wizard-app-manager-api.service
sudo loginctl enable-linger owner
/opt/fakewam backup export -o ~/pre.tar && echo PLANTED""")
    log("plant", rc, out[-200:])
    if "PLANTED" not in out or not canary_ok():
        return verdict("setup", False, "could not plant the fake Wizard: " + out[-300:])
    verdict("setup", True, info.replace("\n", " | "))

    # ---- move: at the box's screen (the trusted serial console)
    con = VM["con"]
    login(con)
    over_ssh = ssh(f"{WAM} encrypt --device /dev/vdb --backup-file /home/owner/pre.tar </dev/null")[1]
    report["checks"]["refused_over_ssh"] = over_ssh[-200:]
    m = at_box(con, f"{WAM} encrypt --device /dev/vdb --backup-file /home/owner/pre.tar",
               ["first 8 letters", "wam disk encrypt:"], 900)
    shown = con.since()
    km = RECOVERY_RE.search(shown)
    if not m or not km:
        verdict("move", False, "no recovery key screen: " + shown[-600:].replace(PW, "<pw>"))
        return
    rec = km.group(1)
    SECRETS.append(rec)
    con.reset()
    con.send("zzzzzzzz\r")
    wrong = bool(con.expect(["does not match"], 60))
    con.reset()
    con.send(rec.split("-")[0] + "\r")
    done = con.expect(["Done: Wizard data is encrypted", "wam disk encrypt:"], 1200)
    tail = con.since()[-800:]
    ok_done = bool(done) and "Done: Wizard data is encrypted" in tail
    rc, dump = ssh("sudo cryptsetup luksDump --dump-json-metadata /dev/vdb")
    try:
        meta = json.loads(dump[dump.index("{"):])
        tokens = sorted((t["type"], tuple(t.get("tpm2-pcrs") or ())) for t in meta["tokens"].values())
        slots = len(meta["keyslots"])
    except ValueError:
        tokens, slots = [], -1
    rc, mounts = ssh("findmnt -no SOURCE,TARGET ~/.wizard-os ~/runtipi /srv/wizard-data; ls -d ~/.wizard-os.plain-before-encrypt-* ~/runtipi.plain-before-encrypt-*; lsattr -d ~/.wizard-os 2>&1 | head -1")
    rc, leak = ssh(f"sudo grep -rIl --exclude-dir=proc '{rec}' /etc /var/log /home /root /opt /srv 2>/dev/null; "
                   f"sudo journalctl -b --no-pager | grep -c '{rec.split('-')[1]}'")
    st = status()
    report["checks"]["move"] = {"tokens": tokens, "keyslots": slots, "mounts": mounts, "status": st,
                                "leak": leak, "tail": tail[-300:]}
    verdict("refused_over_ssh", "not over SSH" in over_ssh, over_ssh[-120:])
    verdict("move", ok_done and wrong and tokens == [("systemd-recovery", ()), ("systemd-tpm2", (7,))]
            and slots == 2 and "/dev/mapper/wizard_data" in mounts and mounts.count("plain-before-encrypt") == 2
            and canary_ok() and st.get("state") == "sealed-wizard-data" and leak.strip() == "0",
            f"done={ok_done} wrong_refused={wrong} tokens={tokens} slots={slots} state={st.get('state')} leak={leak!r}")

    # ---- a: cold restart, same chip, Secure Boot on
    poweroff()
    con = boot(VARS_ON, TPM_A)
    up = wait_ssh(600)
    prompted = any(p in con.since() for p in PROMPTS)
    time.sleep(5)
    st = status()
    verdict("a_cold_restart_no_prompt", up and not prompted and canary_ok() and unit_active() == "active"
            and st.get("state") == "sealed-wizard-data", f"up={up} prompted={prompted} unit={unit_active()} state={st.get('state')}")

    # ---- confirm
    login(con)
    at_box(con, f"{WAM} encrypt --confirm", ["Old unencrypted copies deleted", "wam disk encrypt:"], 600)
    rc, left = ssh("ls -d ~/*.plain-before-encrypt-* ~/.wizard-os.plain-before-encrypt-* 2>/dev/null | wc -l")
    verdict("confirm_deletes_old_copies", left.strip() == "0" and canary_ok(), f"left={left}")

    # ---- c: Secure Boot off withholds the key; recovery key unlocks; back on, no prompt
    poweroff()
    con = boot(VARS_OFF, TPM_A)
    up = wait_ssh(600)
    time.sleep(10)
    rc, locked = ssh("findmnt /srv/wizard-data >/dev/null && echo MOUNTED || echo LOCKED; "
                     "sudo touch ~/.wizard-os/x 2>&1 | tail -1; cat /run/issue.d/wizard-data-locked.issue 2>/dev/null")
    unit = unit_active()
    st_locked = status()
    login(con)
    con.reset()
    con.send(f"{WAM} unlock\r")
    asked = con.expect(["Enter passphrase"], 120)
    con.send(rec + "\r")
    unlocked = con.expect(["Unlocked.", "wam disk encrypt:"], 300)
    time.sleep(5)
    data_back = canary_ok()
    report["checks"]["c"] = {"locked": locked, "unit": unit, "status": st_locked.get("line")}
    verdict("c_secure_boot_off_withholds_key_recovery_unlocks",
            up and "LOCKED" in locked and "Operation not permitted" in locked and "Wizard data is locked" in locked
            and unit != "active" and bool(asked) and bool(unlocked) and data_back,
            f"up={up} locked={locked!r} unit={unit} asked={bool(asked)} unlocked={bool(unlocked)} data={data_back}")
    # reseal for the new state (Secure Boot off), at the box: the next start with it off needs no key
    def reseal_at_box(con):
        con.reset()
        con.send(f"{WAM} reseal\r")
        asked = con.expect(["Enter passphrase", "Please enter", "wam disk encrypt:"], 120)
        con.send(rec + "\r")
        return bool(asked) and bool(con.expect(["sealed to this box's security chip again"], 300))
    resealed = reseal_at_box(con)
    poweroff()
    con = boot(VARS_OFF, TPM_A)
    up = wait_ssh(600)
    time.sleep(5)
    verdict("c_reseal_then_restart_no_key", resealed and up and canary_ok() and unit_active() == "active"
            and status().get("state") == "sealed-wizard-data", f"resealed={resealed} up={up}")
    # Secure Boot on again: withheld again (sealed to off), recovery key + reseal, then no prompt
    poweroff()
    con = boot(VARS_ON, TPM_A)
    up = wait_ssh(600)
    time.sleep(10)
    relocked = "LOCKED" in ssh("findmnt /srv/wizard-data >/dev/null && echo MOUNTED || echo LOCKED")[1]
    login(con)
    con.reset()
    con.send(f"{WAM} unlock\r")
    con.expect(["Enter passphrase"], 120)
    con.send(rec + "\r")
    con.expect(["Unlocked.", "wam disk encrypt:"], 300)
    resealed = reseal_at_box(con)
    poweroff()
    con = boot(VARS_ON, TPM_A)
    up = wait_ssh(600)
    time.sleep(5)
    verdict("c_secure_boot_back_on_reseal_no_prompt", relocked and resealed and up and canary_ok()
            and unit_active() == "active", f"relocked={relocked} resealed={resealed} up={up}")

    # ---- b: another chip withholds the key; the drive image holds no plaintext
    poweroff()
    con = boot(VARS_ON, TPM_B)
    up = wait_ssh(600)
    time.sleep(10)
    rc, locked = ssh("findmnt /srv/wizard-data >/dev/null && echo MOUNTED || echo LOCKED")
    poweroff()
    raw = os.path.join(R, "data.raw")
    subprocess.run(["qemu-img", "convert", "-O", "raw", DATA, raw], check=True)
    found = False
    with open(raw, "rb") as f:
        tail = b""
        while chunk := f.read(16 << 20):
            if CANARY.encode() in tail + chunk:
                found = True
                break
            tail = chunk[-64:]
    os.remove(raw)
    verdict("b_other_chip_locked_and_image_unreadable", up and "LOCKED" in locked and not found,
            f"up={up} {locked} canary_in_image={found}")

    # ---- rollback
    con = boot(VARS_ON, TPM_A)
    up = wait_ssh(600)
    login(con)
    rb = at_box(con, f"{WAM} encrypt --rollback", ["Rolled back:", "wam disk encrypt:"], 1200)
    rc, after = ssh("findmnt ~/.wizard-os >/dev/null && echo STILL-MOUNTED || echo ON-ROOT; grep -c wizard-disk-encrypt /etc/fstab /etc/crypttab")
    poweroff()
    con = boot(VARS_ON, TPM_A)
    up2 = wait_ssh(600)
    time.sleep(5)
    st = status()
    verdict("rollback", bool(rb) and "Rolled back:" in con.text and "ON-ROOT" in after and up2 and canary_ok()
            and unit_active() == "active" and st.get("state") in ("plain", "vps"),
            f"after={after!r} state={st.get('state')} line={st.get('line')}")
    poweroff()


try:
    main()
except Exception as e:  # noqa: BLE001
    verdict("harness", False, f"{type(e).__name__}: {e}")
finally:
    v = report["verdicts"]
    report["summary"] = {"pass": sum(x["pass"] for x in v.values()), "total": len(v)}
    save()
    log("SUMMARY", report["summary"])
    sys.exit(0 if v and all(x["pass"] for x in v.values()) else 1)
