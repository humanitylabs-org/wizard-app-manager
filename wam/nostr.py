"""Nostr catalog source (stage 2 of the Nostr path; off unless enabled).

Finds Wizard apps through NIP-34 repository announcements (kind 30617) carrying
the hashtag `t=wizard-app`. The code itself can stay on GitHub: the
announcement's `clone` tag points wherever the repo lives. Everything after
discovery (wizard.md check, store compile, apps.json) is unchanged.

What Nostr adds over a GitHub topic is a signed publisher. Every event is
verified here (id hash + BIP-340 signature) before it is trusted, and if the
app's wizard.md declares `publisher: npub1...`, the announcement must be signed
by that key or list it as a maintainer.

Stdlib only, so install.sh stays apt-only. Enable with WAM_NOSTR=1 or
`"nostr": true` in ~/.wizard-os/app-manager.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import uuid
from urllib.parse import urlparse

DEFAULT_RELAYS = ["wss://relay.ngit.dev", "wss://gitnostr.com", "wss://relay.damus.io", "wss://nos.lol"]
ANNOUNCEMENT = 30617


def enabled(cfg: dict | None = None) -> bool:
    if os.environ.get("WAM_NOSTR", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return bool((cfg or {}).get("nostr"))


# ---------------------------------------------------------------- bech32 (npub)

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits: int, tobits: int, pad: bool) -> list[int]:
    acc = bits = 0
    out: list[int] = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        raise ValueError("invalid padding")
    return out


def npub_to_hex(npub: str) -> str:
    s = npub.strip().lower()
    hrp, _, data = s.rpartition("1")
    if hrp != "npub" or any(c not in _CHARSET for c in data):
        raise ValueError(f"not an npub: {npub}")
    vals = [_CHARSET.index(c) for c in data]
    if _polymod(_hrp_expand(hrp) + vals) != 1:
        raise ValueError(f"bad npub checksum: {npub}")
    raw = bytes(_convertbits(vals[:-6], 5, 8, False))
    if len(raw) != 32:
        raise ValueError(f"npub is not 32 bytes: {npub}")
    return raw.hex()


def hex_to_npub(hexkey: str) -> str:
    vals = _convertbits(bytes.fromhex(hexkey), 8, 5, True)
    chk = _polymod(_hrp_expand("npub") + vals + [0] * 6) ^ 1
    vals += [(chk >> 5 * (5 - i)) & 31 for i in range(6)]
    return "npub1" + "".join(_CHARSET[v] for v in vals)


# ---------------------------------------------------------- BIP-340 verification

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and a[1] != b[1]:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], _P - 2, _P) % _P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], _P - 2, _P) % _P
    x = (lam * lam - a[0] - b[0]) % _P
    return (x, (lam * (a[0] - x) - a[1]) % _P)


def _mul(p, n):
    r = None
    while n:
        if n & 1:
            r = _add(r, p)
        p = _add(p, p)
        n >>= 1
    return r


def _tagged(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _lift_x(x: int):
    if x >= _P:
        return None
    y2 = (pow(x, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y * y % _P != y2:
        return None
    return (x, y if y % 2 == 0 else _P - y)


def schnorr_verify(msg: bytes, pubkey: bytes, sig: bytes) -> bool:
    if len(pubkey) != 32 or len(sig) != 64:
        return False
    pt = _lift_x(int.from_bytes(pubkey, "big"))
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if pt is None or r >= _P or s >= _N:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32] + pubkey + msg), "big") % _N
    R = _add(_mul(_G, s), _mul(pt, _N - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def event_id(ev: dict) -> str:
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode()).hexdigest()


def verify_event(ev: dict) -> bool:
    try:
        if event_id(ev) != ev["id"]:
            return False
        return schnorr_verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"]))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------- minimal websocket client

class _WS:
    def __init__(self, url: str, timeout: float = 10):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: wizard-app-manager\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError(f"{url}: closed during handshake")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        first = status.split(b"\r\n")[0]
        if b" 101 " not in first:
            raise ConnectionError(f"{url}: {first.decode(errors='replace')}")
        self.buf = rest

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                               bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                               bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str | None:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 0x8:
                return None
            if op == 0x9:  # ping -> pong
                mask = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                continue
            msg += payload
            if b1 & 0x80:
                return msg.decode(errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


def query(relay: str, filt: dict, timeout: float = 10) -> list[dict]:
    """REQ one filter on one relay; return events until EOSE. Unverified."""
    ws = _WS(relay, timeout)
    sub = uuid.uuid4().hex[:12]
    out: list[dict] = []
    try:
        ws.send(json.dumps(["REQ", sub, filt]))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            if raw is None:
                break
            msg = json.loads(raw)
            if msg[0] == "EVENT" and msg[1] == sub:
                out.append(msg[2])
            elif msg[0] in {"EOSE", "CLOSED"} and msg[1] == sub:
                break
        try:
            ws.send(json.dumps(["CLOSE", sub]))
        except Exception:  # noqa: BLE001
            pass
    finally:
        ws.close()
    return out


# ------------------------------------------------------------- NIP-34 parsing

def _tag(ev: dict, name: str) -> list[str]:
    """All values of every tag with this name (NIP-34 multi-value tags flattened)."""
    vals: list[str] = []
    for t in ev.get("tags", []):
        if t and t[0] == name:
            vals.extend(v for v in t[1:] if v)
    return vals


def parse_announcement(ev: dict) -> dict:
    clones = _tag(ev, "clone")
    https = [c for c in clones if c.startswith("https://")]
    euc = next((t[1] for t in ev.get("tags", []) if len(t) >= 3 and t[0] == "r" and t[2] == "euc"), None)
    return {
        "pubkey": ev["pubkey"],
        "npub": hex_to_npub(ev["pubkey"]),
        "d": (_tag(ev, "d") or [""])[0],
        "name": (_tag(ev, "name") or [""])[0],
        "clone": (https or clones or [None])[0],
        "maintainers": [m for m in _tag(ev, "maintainers") if len(m) == 64],
        "euc": euc,
        "tags": _tag(ev, "t"),
        "created_at": ev.get("created_at", 0),
        "id": ev["id"],
    }


def announcements(tag: str, relays: list[str] | None = None, authors: list[str] | None = None,
                  fetch=query) -> tuple[list[dict], list[str]]:
    """Verified, de-duplicated NIP-34 announcements carrying hashtag `tag`.

    `authors` (npubs or hex) restricts to known publishers, e.g. a curated list.
    Newest announcement per (pubkey, d) wins, as for any replaceable event.
    """
    filt: dict = {"kinds": [ANNOUNCEMENT], "#t": [tag]}
    if authors:
        filt["authors"] = [npub_to_hex(a) if a.startswith("npub1") else a for a in authors]
    best: dict[tuple[str, str], dict] = {}
    errors: list[str] = []
    for relay in relays or DEFAULT_RELAYS:
        try:
            evs = fetch(relay, filt)
        except Exception as e:  # noqa: BLE001
            errors.append(f"nostr {relay}: {e}")
            continue
        for ev in evs:
            if ev.get("kind") != ANNOUNCEMENT or tag not in _tag(ev, "t"):
                continue
            if not verify_event(ev):
                errors.append(f"nostr {relay}: dropped event with bad signature {str(ev.get('id'))[:12]}")
                continue
            a = parse_announcement(ev)
            k = (a["pubkey"], a["d"])
            if k not in best or a["created_at"] > best[k]["created_at"]:
                best[k] = a
    return sorted(best.values(), key=lambda a: (a["npub"], a["d"])), errors


def publisher_rule(wizard_publisher: str | None, ann: dict) -> tuple[str, bool, str]:
    """Checker rule for Nostr-found apps: signer must match wizard.md `publisher`, when declared."""
    name = "nostr signer matches wizard.md publisher"
    if not wizard_publisher:
        return (name, True, "no publisher declared")
    try:
        want = npub_to_hex(wizard_publisher)
    except ValueError as e:
        return (name, False, str(e))
    ok = want == ann["pubkey"] or want in ann["maintainers"]
    return (name, ok, f"signed by {ann['npub']}" if not ok else "")
