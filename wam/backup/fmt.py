"""The encrypted container (docs/BACKUP.md, "File format v1").

    magic        b"WIZBAK\\x00\\x01"                       8 bytes
    header_len   uint32 big-endian
    header       UTF-8 JSON: format, cipher, chunk, nonce_prefix, slots (says nothing about the contents)
    header_mac   HMAC-SHA256(HKDF(file_key, info="wizard-backup header"), magic|header_len|header)
    chunks       AES-256-GCM over 1 MiB of plaintext each, AAD = header_mac;
                 nonce = nonce_prefix(7) | counter uint32 BE | last(1); key = HKDF(file_key, nonce_prefix, "wizard-backup payload")

The final chunk has last=1 (it may be empty) and nothing may follow it. A wrong key fails the slot unwrap; a changed
header fails the MAC; a changed, reordered, dropped or appended chunk fails GCM or the last-chunk rule. `Writer` and
`Reader` are streaming file objects: nothing ever holds more than one chunk.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct

from . import crypto
from .crypto import AuthError, BackupCryptoError

MAGIC = b"WIZBAK\x00\x01"
CHUNK = 1 << 20
TAG = 16
MAX_HEADER = 64 << 10
NONCE_PREFIX = 7


class NotABackup(BackupCryptoError):
    pass


class WrongKey(BackupCryptoError):
    pass


class Damaged(BackupCryptoError):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s, what: str) -> bytes:
    try:
        return base64.b64decode(str(s), validate=True)
    except (ValueError, TypeError):
        raise Damaged(f"the file's header is damaged ({what})") from None


# ---------------------------------------------------------------- key slots

def recovery_slot(aead, file_key: bytes, recovery_key: bytes) -> dict:
    salt, nonce = os.urandom(16), os.urandom(12)
    wrap = crypto.hkdf(recovery_key, salt, b"wizard-backup slot")
    return {"kind": "recovery", "key_id": crypto.key_id(recovery_key), "salt": _b64(salt), "nonce": _b64(nonce),
            "wrapped": _b64(aead.seal(wrap, nonce, file_key, MAGIC))}


def passphrase_slot(aead, file_key: bytes, passphrase: str, n: int = crypto.SCRYPT_N) -> dict:
    salt, nonce = os.urandom(16), os.urandom(12)
    wrap = crypto.scrypt(passphrase, salt, n, crypto.SCRYPT_R, crypto.SCRYPT_P)
    return {"kind": "passphrase", "kdf": "scrypt", "n": n, "r": crypto.SCRYPT_R, "p": crypto.SCRYPT_P,
            "salt": _b64(salt), "nonce": _b64(nonce), "wrapped": _b64(aead.seal(wrap, nonce, file_key, MAGIC))}


class Keys:
    """What the owner gave to open a file: recovery keys (32 raw bytes each) and/or passphrases."""

    def __init__(self, recovery: list[bytes] | None = None, passphrases: list[str] | None = None) -> None:
        self.recovery = [k for k in (recovery or []) if k]
        self.passphrases = [p for p in (passphrases or []) if p]

    def __bool__(self) -> bool:
        return bool(self.recovery or self.passphrases)


# ---------------------------------------------------------------- header

def read_header(f) -> tuple[dict, bytes, bytes]:
    """(header, the exact MAC'd bytes, mac). Raises NotABackup / Damaged; reads no payload."""
    magic = _read_exact(f, len(MAGIC))
    if magic != MAGIC:
        if magic[:6] == MAGIC[:6]:
            raise NotABackup("this backup file was written by a newer format version")
        raise NotABackup("this is not a Wizard backup file")
    raw_len = _read_exact(f, 4)
    if len(raw_len) != 4:
        raise Damaged("the file is cut short (inside its header)")
    (n,) = struct.unpack(">I", raw_len)
    if not 2 <= n <= MAX_HEADER:
        raise Damaged("the file's header is damaged (bad length)")
    raw = _read_exact(f, n)
    mac = _read_exact(f, 32)
    if len(raw) != n or len(mac) != 32:
        raise Damaged("the file is cut short (inside its header)")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Damaged("the file's header is damaged (not readable)") from None
    if not isinstance(header, dict) or header.get("format") != 1 or header.get("cipher") != "aes-256-gcm-stream" \
            or header.get("chunk") != CHUNK or not isinstance(header.get("slots"), list):
        raise Damaged("the file's header is damaged or from an unknown format")
    return header, magic + raw_len + raw, mac


def header_info(header: dict) -> dict:
    """What anyone may see without a key: which slots exist and the recovery key ids."""
    return {"slots": [s.get("kind") for s in header.get("slots", []) if isinstance(s, dict)],
            "key_ids": [s.get("key_id") for s in header.get("slots", []) if isinstance(s, dict) and s.get("kind") == "recovery"]}


def unlock(header: dict, signed: bytes, mac: bytes, keys: Keys, aead=None) -> bytes:
    """The file key, or WrongKey (nothing opened it) / Damaged (a key matched but the header was changed)."""
    aead = aead or crypto.backend()
    slots = [s for s in header["slots"] if isinstance(s, dict)]
    rec = [s for s in slots if s.get("kind") == "recovery"]
    pas = [s for s in slots if s.get("kind") == "passphrase"]
    file_key, id_matched = None, False
    for k in keys.recovery:
        kid = crypto.key_id(k)
        for s in sorted(rec, key=lambda s: s.get("key_id") != kid):   # the slot with this key's id first
            id_matched |= s.get("key_id") == kid
            wrap = crypto.hkdf(k, _unb64(s.get("salt"), "slot salt"), b"wizard-backup slot")
            try:
                file_key = aead.open(wrap, _unb64(s.get("nonce"), "slot nonce"), _unb64(s.get("wrapped"), "slot"), MAGIC)
                break
            except AuthError:
                continue
        if file_key:
            break
    if file_key is None:
        for p in keys.passphrases:
            for s in pas:
                try:
                    n, r, pp = int(s.get("n")), int(s.get("r")), int(s.get("p"))
                except (TypeError, ValueError):
                    raise Damaged("the file's header is damaged (passphrase slot)") from None
                if (r, pp) != (crypto.SCRYPT_R, crypto.SCRYPT_P) and not (1 <= r <= 32 and 1 <= pp <= 16):
                    raise Damaged("the file's header is damaged (passphrase slot)")
                if not 2 <= n <= crypto.SCRYPT_MAX_N or n & (n - 1):
                    # checked before any work: a file must not choose how much memory/CPU opening it costs
                    raise Damaged(f"the file's passphrase slot asks for scrypt n={n} (at most "
                                  f"{crypto.SCRYPT_MAX_N} is accepted): damaged or hostile header")
                wrap = crypto.scrypt(p, _unb64(s.get("salt"), "slot salt"), n, r, pp)
                try:
                    file_key = aead.open(wrap, _unb64(s.get("nonce"), "slot nonce"), _unb64(s.get("wrapped"), "slot"), MAGIC)
                    break
                except AuthError:
                    continue
            if file_key:
                break
    if file_key is None:
        if id_matched:
            raise Damaged("the file's header is damaged: the recovery key matches it but its key slot does not open")
        info = header_info(header)
        opens = [f"recovery key {i}" for i in info["key_ids"]] + (["a passphrase"] if pas else [])
        raise WrongKey("wrong key: this key does not open this file (it opens with " + (" or ".join(opens) or "nothing") + ")")
    if len(file_key) != 32:
        raise Damaged("the file's header is damaged (file key)")
    want = hmac.new(crypto.hkdf(file_key, info=b"wizard-backup header"), signed, hashlib.sha256).digest()
    if not hmac.compare_digest(want, mac):
        raise Damaged("the file's header is damaged (its check failed)")
    return file_key


# ---------------------------------------------------------------- streams

def _read_exact(f, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        b = f.read(n - len(buf))
        if not b:
            break
        buf += b
    return bytes(buf)


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    if counter >= 1 << 32:
        raise BackupCryptoError("file too large for one container")
    return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


class Writer:
    """Write-only stream: everything written is encrypted into `f`; close() writes the final chunk."""

    def __init__(self, f, keys: Keys, *, scrypt_n: int = crypto.SCRYPT_N, aead=None) -> None:
        if not keys:
            raise BackupCryptoError("a recovery key or a passphrase is needed to write a backup")
        self.f, self.aead = f, aead or crypto.backend()
        file_key = os.urandom(32)
        prefix = os.urandom(NONCE_PREFIX)
        slots = [recovery_slot(self.aead, file_key, k) for k in keys.recovery]
        slots += [passphrase_slot(self.aead, file_key, p, scrypt_n) for p in keys.passphrases]
        header = {"format": 1, "cipher": "aes-256-gcm-stream", "chunk": CHUNK, "nonce_prefix": _b64(prefix), "slots": slots}
        raw = json.dumps(header, separators=(",", ":")).encode()
        signed = MAGIC + struct.pack(">I", len(raw)) + raw
        self.mac = hmac.new(crypto.hkdf(file_key, info=b"wizard-backup header"), signed, hashlib.sha256).digest()
        f.write(signed + self.mac)
        self.key_ids = [s["key_id"] for s in slots if s["kind"] == "recovery"]
        self.slot_kinds = [s["kind"] for s in slots]
        self._key = crypto.hkdf(file_key, prefix, b"wizard-backup payload")
        self._prefix, self._buf, self._n, self._closed = prefix, bytearray(), 0, False
        self.plain_bytes = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        if self._closed:
            raise ValueError("write to a closed backup stream")
        self._buf += b
        self.plain_bytes += len(b)
        while len(self._buf) > CHUNK:   # strictly more: the final chunk always holds the tail (never a lone empty one)
            self._emit(bytes(self._buf[:CHUNK]), False)
            del self._buf[:CHUNK]
        return len(b)

    def _emit(self, data: bytes, last: bool) -> None:
        self.f.write(self.aead.seal(self._key, _nonce(self._prefix, self._n, last), data, self.mac))
        self._n += 1

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if not self._closed:
            self._emit(bytes(self._buf), True)
            self._buf = bytearray()
            self._closed = True


class Reader:
    """Read-only plaintext stream over an unlocked container. Every chunk is authenticated before any of its bytes
    are returned; reading past the end requires the final chunk and that nothing follows it."""

    def __init__(self, f, header: dict, mac: bytes, file_key: bytes, aead=None) -> None:
        self.f, self.aead, self.mac = f, aead or crypto.backend(), mac
        prefix = _unb64(header.get("nonce_prefix"), "nonce prefix")
        if len(prefix) != NONCE_PREFIX:
            raise Damaged("the file's header is damaged (nonce prefix)")
        self._prefix, self._key = prefix, crypto.hkdf(file_key, prefix, b"wizard-backup payload")
        self._n, self._buf, self._pos, self._done, self._peek = 0, b"", 0, False, b""
        self.cipher_bytes = 0

    def readable(self) -> bool:
        return True

    def _next(self) -> bool:
        if self._done:
            return False
        ct = self._peek + _read_exact(self.f, CHUNK + TAG - len(self._peek))
        self._peek = b""
        if not ct:
            raise Damaged("the file is cut short (its final part is missing)")
        if len(ct) == CHUNK + TAG:
            self._peek = _read_exact(self.f, 1)
        last = not self._peek
        try:
            self._buf = self.aead.open(self._key, _nonce(self._prefix, self._n, last), ct, self.mac)
        except AuthError:
            what = "cut short or damaged" if last else "damaged"
            raise Damaged(f"the file is {what} (part {self._n + 1} failed its check)") from None
        self.cipher_bytes += len(ct)
        self._n += 1
        self._pos = 0
        self._done = last
        return True

    def read(self, n: int = -1) -> bytes:
        out = bytearray()
        while n < 0 or len(out) < n:
            if self._pos >= len(self._buf):
                if not self._next():
                    break
                continue
            take = len(self._buf) - self._pos if n < 0 else min(n - len(out), len(self._buf) - self._pos)
            out += self._buf[self._pos:self._pos + take]
            self._pos += take
        return bytes(out)

    def drain(self) -> None:
        """Read to the authenticated end (proves the final chunk is there and nothing follows it)."""
        while self._next() or not self._done:
            pass

    @property
    def complete(self) -> bool:
        return self._done and self._pos >= len(self._buf)


def open_reader(f, keys: Keys, aead=None) -> Reader:
    header, signed, mac = read_header(f)
    file_key = unlock(header, signed, mac, keys, aead)
    return Reader(f, header, mac, file_key, aead)
