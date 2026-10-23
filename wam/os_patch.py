#!/usr/bin/env python3
"""OS security patches for any Linux: detect the distribution, then use its own package manager and security channel.

Stdlib only, no imports from the rest of wam: the same file runs as the box's fixed root helper
(/usr/local/libexec/wizard-os/os-patch, installed root-owned by `wam maintain setup`, allowed by one sudoers line).

    os-patch check  [--refresh]   what security updates are pending, what is held back, does the box need a reboot
    os-patch apply                install pending security updates that cannot restart the box's work
    os-patch native-off|native-on  pause / restore the distribution's own automatic upgrader (Wizard runs it instead)

Safety (James, 23 Oct 2026: never restart people's servers until restarts are proven robust):
- `apply` never restarts a service and never reboots. On Debian/Ubuntu a temporary policy-rc.d refuses every service
  start/stop/restart from package scripts and needrestart is suspended; the running processes keep their old code
  until a later quiet restart. RPM and other families have no equivalent switch, so there `apply` only runs when no
  held package is pending and reports that package scripts may try-restart their own service.
- Packages whose upgrade restarts the platform itself (container runtime, private network, init) are HELD: never
  installed by `apply`, reported as waiting for the quiet-restart lane.
- Families without a security-only channel (Arch, a rolling distribution) are report-only.

Every family is one Backend subclass; adding a distribution means adding one class to BACKENDS.
Python 3.6+ and stdlib only: it runs under the host's own python3 (openSUSE Leap 15 ships 3.6).
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Upgrading these restarts what the box's work runs on: every app container, the private network, or PID 1.
HOLD = ("docker*", "containerd*", "runc", "moby-*", "podman*", "crun", "tailscale*", "systemd", "systemd-sysv",
        "libsystemd0", "udev", "dbus", "dbus-daemon", "dbus-broker")
POLICY_RC = Path("/usr/sbin/policy-rc.d")
POLICY_MARK = "# wizard-os os-patch: refuse service restarts during a security patch run"
APT_NATIVE_OFF = Path("/etc/apt/apt.conf.d/52wizard-os-maintenance")
TIMEOUT = 3600


def run(argv, timeout=600, env=None, check=False):
    e = {**os.environ, "LC_ALL": "C", "LANG": "C", **(env or {})}
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       timeout=timeout, env=e)
    if check and p.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {p.returncode}: {(p.stderr or p.stdout).strip()[-600:]}")
    return p


def os_release(path = Path("/etc/os-release")):
    out = {}
    try:
        for line in path.read_text().splitlines():
            m = re.match(r'([A-Z_]+)=(.*)', line.strip())
            if m:
                v = m[2].strip()
                out[m[1]] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v
    except OSError:
        pass
    return out


def held(name: str):
    return any(fnmatch.fnmatchcase(name, pat) for pat in HOLD)


def running_kernel():
    return os.uname().release


def newest_installed_kernel(mod_root = Path("/lib/modules")):
    try:
        dirs = [d.name for d in mod_root.iterdir() if (d / "modules.dep").exists() or (d / "kernel").is_dir()]
    except OSError:
        return None
    key = lambda v: [int(x) if x.isdigit() else x for x in re.split(r"[.\-+_]", v)]
    try:
        return max(dirs, key=key) if dirs else None
    except TypeError:
        return sorted(dirs)[-1] if dirs else None


def stale_processes(proc = Path("/proc"), limit = 40):
    """Processes still running code deleted by an upgrade (they pick up the fix only after a restart)."""
    names = set()
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return []
    for p in pids:
        try:
            maps = (p / "maps").read_text(errors="replace")
        except OSError:
            continue
        if any(l.endswith("(deleted)") and ("/lib" in l or "/bin" in l) and "/memfd:" not in l and "/dev/shm" not in l
               for l in maps.splitlines()):
            try:
                names.add((p / "comm").read_text().strip())
            except OSError:
                pass
        if len(names) >= limit:
            break
    return sorted(names)


class Backend:
    family = "unknown"
    tool = None
    native = "none"
    can_apply = False
    rolling = False

    def __init__(self, rel):
        self.rel = rel

    def available(self):
        return bool(self.tool and shutil.which(self.tool))

    # Each returns a list of {"name", "from", "to"} security updates.
    def pending(self, refresh):
        return []

    def reboot_required(self):
        k = newest_installed_kernel()
        return bool(k and k != running_kernel())

    def native_auto(self):
        return {"tool": self.native, "enabled": None}

    def apply_argv(self, names):
        raise NotImplementedError

    def apply(self, names):
        return run(self.apply_argv(names), timeout=TIMEOUT)

    def restarts_suppressed(self):
        return False


class Apt(Backend):
    family, tool, native, can_apply = "apt", "apt-get", "unattended-upgrades", True
    INST = re.compile(r"^Inst (\S+) (?:\[([^\]]+)\] )?\((\S+) ([^\[]*)\[")

    def pending(self, refresh):
        if refresh:
            run(["apt-get", "update", "-qq", "-o", "DPkg::Lock::Timeout=900"], timeout=1200, check=True)
        p = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "dist-upgrade"], check=True)
        out = []
        for line in p.stdout.splitlines():
            m = self.INST.match(line)
            if m and "security" in m[4].lower():
                out.append({"name": m[1].split(":")[0], "from": m[2], "to": m[3]})
        return out

    def reboot_required(self):
        return Path("/run/reboot-required").exists() or super().reboot_required()

    def native_auto(self):
        p = run(["apt-config", "shell", "U", "APT::Periodic::Unattended-Upgrade"]) if shutil.which("apt-config") else None
        val = (re.search(r"U='?([0-9]+)", p.stdout) or [None, "0"])[1] if p and p.returncode == 0 else "0"
        return {"tool": self.native, "installed": bool(shutil.which("unattended-upgrade")),
                "enabled": bool(shutil.which("unattended-upgrade")) and val != "0",
                "paused_by_wizard": APT_NATIVE_OFF.exists(),
                "may_restart_services": bool(shutil.which("needrestart"))}

    def apply_argv(self, names):
        return ["apt-get", "install", "-y", "-q", "--only-upgrade", "-o", "DPkg::Lock::Timeout=900",
                "-o", "Dpkg::Options::=--force-confdef",
                "-o", "Dpkg::Options::=--force-confold", *names]

    def would_install(self, names):
        p = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", *self.apply_argv(names)[1:]], timeout=600)
        if p.returncode != 0:
            return ["<unresolvable>"]
        return [m[1].split(":")[0] for m in map(self.INST.match, p.stdout.splitlines()) if m]

    def safe_set(self, names):
        """Drop any package whose upgrade would pull a held platform package in as a dependency."""
        if not any(held(n) for n in self.would_install(names)):
            return names
        keep = [n for n in names if not any(held(x) for x in self.would_install([n]))]
        return keep if keep and not any(held(x) for x in self.would_install(keep)) else []

    def apply(self, names):
        env = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_SUSPEND": "1", "NEEDRESTART_MODE": "l",
               "UCF_FORCE_CONFFOLD": "1"}
        names = self.safe_set(names)
        if not names:
            return subprocess.CompletedProcess([], 0, "", "")
        with _no_service_restarts():
            return run(self.apply_argv(names), timeout=TIMEOUT, env=env)

    def restarts_suppressed(self):
        return True


class Dnf(Backend):
    family, native, can_apply = "dnf", "dnf-automatic", True

    @property
    def tool(self):
        return "dnf5" if shutil.which("dnf5") and not shutil.which("dnf") else "dnf" if shutil.which("dnf") else "yum"

    def pending(self, refresh):
        argv = [self.tool, "-q", "updateinfo", "list", "--security", "--updates" if self.tool == "dnf5" else "--available"]
        if refresh:
            argv.insert(2, "--refresh")
        p = run(argv, timeout=900)
        if p.returncode != 0 and self.tool != "dnf5":        # older yum/dnf without --available
            p = run([a for a in argv if a != "--available"], timeout=900)
        if p.returncode != 0:
            raise RuntimeError(f"{self.tool} updateinfo exited {p.returncode}: {p.stderr.strip()[-400:]}")
        seen, out = set(), []
        for line in p.stdout.splitlines():
            parts = line.split()
            # rows: ADVISORY  TYPE(e.g. Important/Sec.)  NEVRA   (dnf4) | NAME TYPE SEVERITY NEVRA ... (dnf5)
            nevra = next((x for x in reversed(parts) if re.match(r".+-[0-9][^-]*-[^-]+\.[a-z0-9_]+$", x)), None)
            if not nevra or not parts or parts[0].lower() in ("name", "advisory"):
                continue
            m = re.match(r"(.+)-([0-9][^-]*)-([^-]+)\.([a-z0-9_]+)$", nevra)
            name = m[1].split(":")[-1] if m else nevra
            name = re.sub(r"^[0-9]+:", "", name)
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "from": None, "to": f"{re.sub(r'^[0-9]+:', '', m[2])}-{m[3]}" if m else None})
        return out

    def reboot_required(self):
        if shutil.which("needs-restarting"):
            p = run(["needs-restarting", "-r"], timeout=120)
            if p.returncode in (0, 1):
                return p.returncode == 1
        return super().reboot_required()

    def native_auto(self):
        en = None
        if shutil.which("systemctl"):
            en = any(run(["systemctl", "is-enabled", t]).stdout.strip() == "enabled"
                     for t in ("dnf-automatic.timer", "dnf-automatic-install.timer", "dnf5-automatic.timer"))
        return {"tool": self.native, "enabled": en}

    def apply_argv(self, names):
        return [self.tool, "-y", "-q", "upgrade", "--security", "--skip-broken", *[f"--exclude={g}" for g in HOLD], *names]

    def apply(self, names):
        """One transaction; when a held package makes it unresolvable, each package alone (a failing one is skipped
        and reported in not_installed, the rest still get their fixes)."""
        p = run(self.apply_argv(names), timeout=TIMEOUT)
        if p.returncode == 0 or len(names) == 1:
            return p
        for n in names:
            run(self.apply_argv([n]), timeout=900)
        return subprocess.CompletedProcess(p.args, 0, "", "")


class Zypper(Backend):
    family, tool, native, can_apply = "zypper", "zypper", "none (YaST online update)", False

    def pending(self, refresh):
        argv = ["zypper", "--non-interactive", "--xmlout", "list-patches", "--category", "security"]
        if refresh:
            run(["zypper", "--non-interactive", "refresh"], timeout=900)
        p = run(argv, timeout=900)
        if p.returncode not in (0, 100, 101, 102, 103):
            raise RuntimeError(f"zypper exited {p.returncode}: {p.stderr.strip()[-400:]}")
        out = []
        for tag in re.findall(r"<update [^>]*>", p.stdout):
            a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if a.get("category") == "security" and a.get("status", "needed") == "needed":
                out.append({"name": a.get("name"), "from": None, "to": a.get("edition"), "severity": a.get("severity")})
        return out


class Pacman(Backend):
    family, tool, native, can_apply, rolling = "pacman", "pacman", "none (rolling release)", False, True

    def pending(self, refresh):
        # Arch has no security-only channel: every pending update is reported; applying is a full upgrade.
        p = run(["pacman", "-Qu"], timeout=300)
        return [{"name": l.split()[0], "from": l.split()[1], "to": l.split()[-1]}
                for l in p.stdout.splitlines() if len(l.split()) >= 4]


class Apk(Backend):
    family, tool, native, can_apply = "apk", "apk", "none", True

    def pending(self, refresh):
        if refresh:
            run(["apk", "update", "-q"], timeout=600, check=True)
        p = run(["apk", "version", "-l", "<"], timeout=300)
        out = []
        for line in p.stdout.splitlines()[1:] if p.stdout.startswith("Installed") else p.stdout.splitlines():
            m = re.match(r"(\S+)-([0-9][^-]*-r[0-9]+)\s+<\s+(\S+)", line)
            if m:
                out.append({"name": m[1], "from": m[2], "to": m[3]})
        return out   # stable Alpine branches ship only security and bug fixes

    def apply_argv(self, names):
        return ["apk", "add", "-q", "--upgrade", *names]

    def apply(self, names):
        sim = run(["apk", "add", "-s", "--upgrade", *names], timeout=300)
        pulled = re.findall(r"Upgrading (\S+) \(", sim.stdout)
        if any(held(n) for n in pulled):
            names = [n for n in names if not any(held(x) for x in re.findall(
                r"Upgrading (\S+) \(", run(["apk", "add", "-s", "--upgrade", n], timeout=300).stdout))]
        return run(self.apply_argv(names), timeout=TIMEOUT) if names else subprocess.CompletedProcess([], 0, "", "")

    def restarts_suppressed(self):
        return True   # OpenRC packages do not restart services on upgrade


BACKENDS = {"apt": Apt, "dnf": Dnf, "zypper": Zypper, "pacman": Pacman, "apk": Apk}
FAMILY_IDS = {
    "apt": {"debian", "ubuntu", "raspbian", "linuxmint", "pop", "elementary", "kali", "neon", "zorin"},
    "dnf": {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn", "nobara"},
    "zypper": {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles", "suse", "sled"},
    "pacman": {"arch", "manjaro", "endeavouros", "garuda", "archarm"},
    "apk": {"alpine", "postmarketos"},
}


def detect(rel = None, require_tool = True):
    """The backend for this system: by /etc/os-release ID, then ID_LIKE, then whichever package manager exists."""
    rel = os_release() if rel is None else rel
    ids = [rel.get("ID", "").lower(), *rel.get("ID_LIKE", "").lower().split()]
    for i in ids:
        for fam, members in FAMILY_IDS.items():
            if i in members:
                b = BACKENDS[fam](rel)
                if not require_tool or b.available():
                    return b
    for fam, tool in (("apt", "apt-get"), ("dnf", "dnf"), ("dnf", "yum"), ("zypper", "zypper"), ("pacman", "pacman"),
                      ("apk", "apk")):
        if shutil.which(tool):
            return BACKENDS[fam](rel)
    return Backend(rel)


class _no_service_restarts:
    """A temporary /usr/sbin/policy-rc.d that refuses every service action (exit 101), honoured by invoke-rc.d and
    deb-systemd-invoke. An owner's own policy-rc.d is left alone (theirs already decides) and never overwritten."""

    def __enter__(self):
        self.ours = False
        if POLICY_RC.exists() or POLICY_RC.is_symlink():
            return self
        POLICY_RC.write_text(f"#!/bin/sh\n{POLICY_MARK}\nexit 101\n")
        POLICY_RC.chmod(0o755)
        self.ours = True
        return self

    def __exit__(self, *exc):
        if self.ours:
            try:
                if POLICY_MARK in POLICY_RC.read_text():
                    POLICY_RC.unlink()
            except OSError:
                pass
        return False


def leftover_guard():
    """A policy-rc.d we wrote but never removed (the run was killed): remove it so normal service starts work."""
    try:
        if POLICY_RC.exists() and POLICY_MARK in POLICY_RC.read_text():
            POLICY_RC.unlink()
            return True
    except OSError:
        pass
    return False


def check(refresh = False, rel = None):
    b = detect(rel)
    doc = {"family": b.family, "distro": (b.rel.get("PRETTY_NAME") or b.rel.get("ID") or "unknown"),
           "can_apply": b.can_apply and not b.rolling, "rolling": b.rolling, "native_auto": None,
           "running_kernel": running_kernel(), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if b.family == "unknown":
        return {**doc, "state": "unsupported", "reason": "No supported package manager found on this system."}
    try:
        pend = b.pending(refresh and os.geteuid() == 0)
        doc["native_auto"] = b.native_auto()
        doc["reboot_required"] = b.reboot_required()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {**doc, "state": "error", "reason": str(exc)[-600:]}
    doc["security"] = [p for p in pend if not held(p["name"])]
    doc["held"] = [p for p in pend if held(p["name"])]
    doc["state"] = "pending" if pend else "current"
    return doc


def apply(rel = None):
    if os.geteuid() != 0:
        return {"state": "refused", "reason": "Applying OS updates needs the root helper (wam maintain setup)."}
    removed = leftover_guard()
    before = check(refresh=True, rel=rel)
    if before["state"] not in ("pending", "current"):
        return before
    b = detect(rel)
    if not before["can_apply"]:
        return {**before, "state": "report_only", "reason": f"{b.family}: no safe security-only install route; reported only."}
    if not before["security"]:
        return {**before, "state": "current" if not before["held"] else "held", "installed": [], "guard_cleaned": removed}
    names = [p["name"] for p in before["security"]]
    t0 = time.monotonic()
    p = b.apply(names)
    after = check(refresh=False, rel=rel)
    still = {x["name"] for x in after.get("security") or []}
    res = {**after, "installed": [n for n in names if n not in still], "not_installed": sorted(still & set(names)),
           "exit_code": p.returncode, "seconds": round(time.monotonic() - t0, 1),
           "restarts": "suppressed" if b.restarts_suppressed() else "package scripts may try-restart their own service",
           "needs_restart": stale_processes(), "guard_cleaned": removed}
    if p.returncode != 0:
        res.update(state="error", reason=(p.stderr or p.stdout).strip()[-600:])
    else:
        res["state"] = "applied"
    return res


def native(on, rel = None):
    b = detect(rel)
    if b.family != "apt":
        return {"family": b.family, "changed": False, "note": "This system's own updater is left as the owner set it."}
    if os.geteuid() != 0:
        return {"state": "refused", "reason": "needs root"}
    if on:
        changed = APT_NATIVE_OFF.exists()
        APT_NATIVE_OFF.unlink(missing_ok=True)
    else:
        changed = not APT_NATIVE_OFF.exists()
        APT_NATIVE_OFF.write_text("// Wizard OS runs security patches itself (wam maintain), without service restarts.\n"
                                  "// Remove this file, or run `wam maintain --os-security check`, to give them back.\n"
                                  'APT::Periodic::Unattended-Upgrade "0";\n')
    return {"family": "apt", "changed": changed, "native_auto": b.native_auto()}


# Quiet restart privilege boundary: no caller-selected unit outside needrestart's fresh list.
SERVICE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_@:.-]*\.service\Z")


def restart_check():
    b = detect()
    doc = {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
           "reboot_required": b.reboot_required(), "services": [], "state": "current"}
    if not shutil.which("systemctl"):
        return dict(doc, state="report_only", reason="Service restarts require systemd.")
    p = run(["systemctl", "is-system-running"], timeout=30)
    doc["system_state"] = p.stdout.strip()
    if not shutil.which("needrestart"):
        return dict(doc, state="report_only", reason="needrestart is not installed; no services will be restarted.")
    p = run(["needrestart", "-b", "-r", "l"], timeout=180)
    if p.returncode != 0:
        return dict(doc, state="error", reason=(p.stderr or p.stdout)[-600:])
    names = [line.partition(":")[2].strip() for line in p.stdout.splitlines() if line.startswith("NEEDRESTART-SVC:")]
    if any(not SERVICE.fullmatch(n) for n in names):
        return dict(doc, state="error", reason="needrestart returned an invalid service name.")
    doc["services"] = sorted(set(names))
    return doc


def restart_service(name):
    if os.geteuid() != 0 or not SERVICE.fullmatch(name):
        return {"state": "refused", "reason": "Root and a valid system service are required."}
    before = restart_check()
    if before["state"] == "error":
        return before
    if name not in before["services"]:
        return {"state": "current", "service": name, "reason": "Not in needrestart's current list; left alone."}
    # Restarting a user manager can kill this very maintenance runner. IPC/login
    # brokers can tear down work even when their unit comes back. Move them to reboot.
    if name.startswith("user@") or name in ("dbus.service", "dbus-broker.service", "systemd-logind.service"):
        return {"state": "report_only", "service": name, "reason": "Login/IPC manager requires an explicit quiet reboot."}
    # Never start a stopped service and never stop a service just to test it.
    if run(["systemctl", "is-active", "--quiet", name], timeout=30).returncode != 0:
        return {"state": "refused", "service": name, "reason": "Service is not active; left alone."}
    p = run(["systemctl", "try-restart", name], timeout=180)
    active = run(["systemctl", "is-active", "--quiet", name], timeout=30).returncode == 0
    if active and p.returncode == 0:
        time.sleep(1)
        active = run(["systemctl", "is-active", "--quiet", name], timeout=30).returncode == 0
    if p.returncode or not active:
        recovery = None
        if not active:
            recovery = run(["systemctl", "start", name], timeout=180)
        recovered = run(["systemctl", "is-active", "--quiet", name], timeout=30).returncode == 0
        return {"state": "error", "service": name, "recovered": recovered,
                "reason": (p.stderr or p.stdout or "Service did not recover after restart.")[-600:],
                "recovery_error": (recovery.stderr or recovery.stdout)[-600:] if recovery and recovery.returncode else ""}
    return {"state": "restarted", "service": name}


def apply_held():
    if os.geteuid() != 0:
        return {"state": "refused", "reason": "needs root"}
    before = check(refresh=False)
    if before["state"] not in ("pending", "current"):
        return before
    b = detect()
    if b.family != "apt":
        return {"state": "report_only", "held": before.get("held", []),
                "reason": "Held platform packages are installable only on apt in this increment."}
    targets = before.get("held") or []
    if not targets:
        return {"state": "current", "installed": []}
    leftover_guard()
    if POLICY_RC.exists() or POLICY_RC.is_symlink():
        return {"state": "refused", "reason": "An owner policy-rc.d exists; cannot guarantee restart suppression without replacing it."}
    # Pin the security candidates we checked, refuse removals, suppress service actions.
    names = [p["name"] + "=" + p["to"] for p in targets]
    with _no_service_restarts():
        p = run(b.apply_argv(names) + ["--no-remove"], timeout=TIMEOUT,
                env={"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_SUSPEND": "1", "NEEDRESTART_MODE": "l"})
    after = check(refresh=False)
    if after["state"] not in ("pending", "current"):
        return dict(after, state="error", reason="Cannot verify held platform update.")
    still = {x["name"] for x in after.get("held", [])}
    installed = [x["name"] for x in targets if x["name"] not in still]
    return {"state": "applied" if p.returncode == 0 and not still.intersection(x["name"] for x in targets) else "error",
            "installed": installed, "held": after.get("held", []), "reboot_required": after.get("reboot_required"),
            "reason": (p.stderr or p.stdout)[-600:] if p.returncode else ""}


def schedule_reboot():
    if os.geteuid() != 0:
        return {"state": "refused", "reason": "needs root"}
    if not detect().reboot_required():
        return {"state": "current", "reason": "A reboot is no longer required."}
    # A short delay lets the unprivileged caller fsync the receipt before shutdown. No detached shell.
    p = run(["systemd-run", "--unit=wizard-os-maintenance-reboot", "--on-active=10s", "--collect",
             "/usr/bin/systemctl", "reboot"], timeout=30)
    return {"state": "scheduled" if p.returncode == 0 else "error", "reason": (p.stderr or p.stdout)[-600:]}


def main(argv = None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["check"] and set(argv[1:]) <= {"--refresh"}:
        res = check(refresh="--refresh" in argv)
    elif argv == ["apply"]:
        res = apply()
    elif argv == ["restart-check"]:
        res = restart_check()
    elif len(argv) == 2 and argv[0] == "restart-service":
        res = restart_service(argv[1])
    elif argv == ["apply-held"]:
        res = apply_held()
    elif argv == ["reboot"]:
        res = schedule_reboot()
    elif argv in (["native-off"], ["native-on"]):
        res = native(argv[0] == "native-on")
    else:
        print("usage: os-patch check [--refresh] | apply | native-off | native-on", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2))
    return 0 if res.get("state") not in ("error", "refused", "unsupported") else 1


if __name__ == "__main__":
    sys.exit(main())
