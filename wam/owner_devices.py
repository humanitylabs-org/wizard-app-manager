"""Which tailnet node is one of the owner's own devices (spec/OWNER_PROOF.md, amendment 1).

Tailscale Serve labels a request with the tailnet USER (`Tailscale-User-Login`), and every untagged node signed in as
the owner carries that label: a phone, but also the owner's other servers, where agents run. "The owner's device"
therefore means a device in the Wizard's own record of the owner's devices, not any node of the owner's login.

REUSE: the record is the Devices registry `~/.wizard-os/devices.json` (spec/DEVICES.md; wizard-ux
tools/system-devices.mjs cleanRegistry / peerGroup). It already says which tailnet machines are the owner's
(`groups: {<tailnet name>: human}`), which are machines the Wizard works on (`devices[]`, `this`, group `agent`) and
which belong to clients (`client`/`other`). No second list is kept here. A node of the owner's login counts when the
record marks it `human`, or when it is an iOS, iPadOS or Android device: that is exactly what "Connect another device"
enrolls (agent service phone-setup.mjs lists the owner's mobile peers). A laptop, Mac or server must be marked `human`
first: unlike wizard-ux's display default, macOS is NOT assumed to be the owner, because a Mac that runs agents is
common. A machine the Wizard works on (this box, `devices[]`) never counts, and neither does anything marked agent,
client or other. Marks are matched on the MagicDNS name the control plane assigns, never on the hostname the node
reports about itself.

The caller is resolved by Tailscale itself (`tailscale whois --json <X-Forwarded-For>`: the LocalAPI whois), which
gives the node's stable ID, its tags and its owner login. Any failure to resolve it or to read the record refuses
the request (fail closed). Known limit, recorded in the spec: the registry names nodes, not stable IDs, and the
agent can edit it; recording the stable ID at the owner-authenticated connect step is the follow-up.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

PHONE_OS = {"ios", "android", "ipados"}      # what "Connect another device" adds (phone-setup.mjs isPhone)
NOT_OWNER_GROUPS = {"agent", "client", "other"}
UNVERIFIED = "owner_device_unverified"      # the lookup failed: refused, nothing is assumed
REQUIRED = "owner_device_required"          # resolved, but not one of the owner's enrolled devices


class Unverified(Exception):
    """The caller's node or the device record could not be read."""


def registry_path(home: Path) -> Path:
    return Path(os.environ.get("WIZARD_DEVICES_FILE") or (Path(home) / "devices.json"))


def read_registry(home: Path) -> dict:
    """The device record, as wizard-ux writes it. Missing = empty; unreadable = Unverified (never 'no devices')."""
    try:
        raw = json.loads(registry_path(home).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise Unverified("device record unreadable") from None
    if not isinstance(raw, dict):
        raise Unverified("device record unreadable")
    return raw


def _name(value) -> str:
    return str(value or "").strip().rstrip(".").lower()


def _label(value) -> str:
    return _name(value).split(".")[0]


def node_names(node: dict) -> set[str]:
    """Every name the node is known by (for EXCLUSION: any of them naming a Wizard machine rules the node out)."""
    info = node.get("Hostinfo") or {}
    out = {_name(node.get("Name")), _label(node.get("Name")), _name(info.get("Hostname"))}
    return {n for n in out if n}


def assigned_names(node: dict) -> set[str]:
    """The names the control plane gave the node (MagicDNS): a node cannot choose these, so only they grant a mark."""
    out = {_name(node.get("Name")), _label(node.get("Name"))}
    return {n for n in out if n}


def wizard_machines(reg: dict) -> set[str]:
    """Tailnet names of every machine the Wizard works on (this box, registered devices): never the owner's own."""
    out = set()
    this = reg.get("this") if isinstance(reg.get("this"), dict) else {}
    out |= {_name(x) for x in this.get("tailnet") or []}
    for d in reg.get("devices") or []:
        if not isinstance(d, dict):
            continue
        out |= {_name(d.get("id")), _label((d.get("connect") or {}).get("host")), _name((d.get("connect") or {}).get("host"))}
        out |= {_name(x) for x in d.get("tailnet") or []}
    return {n for n in out if n}


def is_owner_device(reg: dict, node: dict) -> bool:
    """Pure membership rule over the record. See the module docstring."""
    names = node_names(node)
    if not names or names & wizard_machines(reg):
        return False
    groups = reg.get("groups") if isinstance(reg.get("groups"), dict) else {}
    # a mark under ANY of the node's names can only take access away; only an assigned name can grant it
    if {str(groups.get(n)) for n in names if n in groups} & NOT_OWNER_GROUPS:
        return False
    if any(groups.get(n) == "human" for n in assigned_names(node)):
        return True
    return str((node.get("Hostinfo") or {}).get("OS") or "").lower() in PHONE_OS


def tailscale_whois(ip: str) -> dict:
    """LocalAPI whois through the CLI: `tailscale whois --json <ip>`."""
    exe = shutil.which("tailscale")
    if not exe:
        raise Unverified("tailscale not available")
    try:
        r = subprocess.run([exe, "whois", "--json", ip], capture_output=True, text=True, timeout=5)
        out = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise Unverified("whois failed") from None
    if r.returncode or not isinstance(out, dict):
        raise Unverified("whois failed")
    return out


class Enrollment:
    """`check(hop, login)` -> None for an enrolled owner device, else the refusal code."""

    def __init__(self, home: Path, whois=None, ttl: float = 10.0):
        self.home, self._whois, self.ttl = Path(home), whois or tailscale_whois, ttl
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def node(self, hop: str) -> dict:
        try:
            ip = str(ipaddress.ip_address(str(hop).strip()))     # one address: no list, no port, no name
        except ValueError:
            raise Unverified("no usable source address") from None
        with self._lock:
            hit = self._cache.get(ip)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
        try:
            got = self._whois(ip)
        except Unverified:
            raise
        except Exception:    # noqa: BLE001  (an injected lookup may fail any way it likes: still refused)
            raise Unverified("whois failed") from None
        node = got.get("Node") if isinstance(got, dict) else None
        login = _name(((got or {}).get("UserProfile") or {}).get("LoginName"))
        if not isinstance(node, dict) or not (node.get("StableID") or node.get("ID")) or not login:
            raise Unverified("no node for that address")
        out = {**node, "_login": login}
        with self._lock:
            self._cache[ip] = (time.monotonic(), out)
        return out

    def check(self, hop: str, login: str) -> str | None:
        try:
            node = self.node(hop)
            reg = read_registry(self.home)
        except Unverified:
            return UNVERIFIED
        if node.get("Tags") or node["_login"] != _name(login):
            return REQUIRED          # a tagged node is not a person's device; the node's owner must be the asserted one
        return None if is_owner_device(reg, node) else REQUIRED
