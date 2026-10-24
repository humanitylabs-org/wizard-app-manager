"""Crypto primitives for Wizard backups, with no new dependency.

- AES-256-GCM from the system OpenSSL `libcrypto` through ctypes (the library Python's own `_ssl`/`_hashlib` link,
  or whatever `ctypes.util.find_library('crypto')` finds), or from `cryptography` when it is importable (preferred).
  WAM_BACKUP_CRYPTO=libcrypto|cryptography forces one.
- HKDF-SHA256 (RFC 5869) with `hmac`; scrypt from `hashlib` with an explicit `maxmem`.
- The recovery key: 32 random bytes shown as `WZRK-XXXX-XXXX-...` (base32 of key + 3-byte checksum, 14 groups),
  so a typo is reported as a typo, never as a wrong key.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import os
import unicodedata


class BackupCryptoError(Exception):
    """Base for every refusal: the message is safe to show (it never contains key material)."""


class AuthError(BackupCryptoError):
    pass


class KeyTypo(BackupCryptoError):
    pass


# ---------------------------------------------------------------- HKDF / scrypt

def hkdf(ikm: bytes, salt: bytes = b"", info: bytes = b"", length: int = 32) -> bytes:
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


SCRYPT_N, SCRYPT_R, SCRYPT_P = 131072, 8, 1
# The most work a file may ask for: a passphrase slot names its own n, and 2^20 would make opening an untrusted
# file cost ~1 GiB of memory. 2^18 (256 MiB) leaves room above the 2^17 default every file is written with.
SCRYPT_MAX_N = 1 << 18


def scrypt(passphrase: str, salt: bytes, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    if n < 2 or n & (n - 1) or n > SCRYPT_MAX_N or not 1 <= r <= 32 or not 1 <= p <= 16:
        raise AuthError("the passphrase slot has unsupported scrypt settings (damaged header?)")
    pw = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * n * r * p + (32 << 20))


# ---------------------------------------------------------------- AES-256-GCM backends

class _Cryptography:
    name = "cryptography"

    def __init__(self) -> None:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aesgcm, self._invalid = AESGCM, InvalidTag

    def seal(self, key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        return self._aesgcm(key).encrypt(nonce, bytes(data), aad)

    def open(self, key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        try:
            return self._aesgcm(key).decrypt(nonce, bytes(data), aad)
        except self._invalid:
            raise AuthError("authentication failed") from None


def _libcrypto_candidates() -> list[str]:
    out: list[str] = []
    try:   # the libcrypto this Python already has loaded (its _hashlib/_ssl link it)
        import _hashlib  # noqa: F401
        with open("/proc/self/maps") as f:
            for line in f:
                path = line.split()[-1] if line.strip() else ""
                if "libcrypto" in path and path not in out and os.path.isfile(path):
                    out.append(path)
    except (ImportError, OSError):
        pass
    found = ctypes.util.find_library("crypto")
    if found:
        out.append(found)
    out += ["libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.dylib"]
    return out


class _Libcrypto:
    name = "libcrypto"
    _SET_IVLEN, _GET_TAG, _SET_TAG = 0x9, 0x10, 0x11

    def __init__(self) -> None:
        lib = None
        for cand in _libcrypto_candidates():
            try:
                lib = ctypes.CDLL(cand)
                lib.EVP_aes_256_gcm  # noqa: B018 - probe the symbol
                break
            except (OSError, AttributeError):
                lib = None
        if lib is None:
            raise BackupCryptoError("no OpenSSL libcrypto found for AES-256-GCM")
        vp, ip, cp = ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p
        lib.EVP_CIPHER_CTX_new.restype = vp
        lib.EVP_CIPHER_CTX_free.argtypes = [vp]
        lib.EVP_aes_256_gcm.restype = vp
        for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
            getattr(lib, fn).argtypes = [vp, vp, vp, cp, cp]
        for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
            getattr(lib, fn).argtypes = [vp, cp, ip, cp, ctypes.c_int]
        for fn in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
            getattr(lib, fn).argtypes = [vp, cp, ip]
        lib.EVP_CIPHER_CTX_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_int, vp]
        self.lib = lib

    def _run(self, enc: bool, key: bytes, nonce: bytes, data: bytes, aad: bytes, tag: bytes | None = None):
        lib = self.lib
        if len(key) != 32 or len(nonce) != 12:
            raise BackupCryptoError("bad key or nonce length")
        ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise BackupCryptoError("EVP_CIPHER_CTX_new failed")
        try:
            init = lib.EVP_EncryptInit_ex if enc else lib.EVP_DecryptInit_ex
            upd = lib.EVP_EncryptUpdate if enc else lib.EVP_DecryptUpdate
            fin = lib.EVP_EncryptFinal_ex if enc else lib.EVP_DecryptFinal_ex
            n = ctypes.c_int(0)
            if init(ctx, lib.EVP_aes_256_gcm(), None, None, None) != 1:
                raise BackupCryptoError("EVP init failed")
            if lib.EVP_CIPHER_CTX_ctrl(ctx, self._SET_IVLEN, 12, None) != 1:
                raise BackupCryptoError("EVP set iv length failed")
            if init(ctx, None, None, bytes(key), bytes(nonce)) != 1:
                raise BackupCryptoError("EVP key init failed")
            if aad and upd(ctx, None, ctypes.byref(n), bytes(aad), len(aad)) != 1:
                raise BackupCryptoError("EVP aad failed")
            out = ctypes.create_string_buffer(len(data) + 16)
            total = 0
            if data:
                if upd(ctx, out, ctypes.byref(n), bytes(data), len(data)) != 1:
                    raise BackupCryptoError("EVP update failed")
                total = n.value
            if not enc:
                tbuf = ctypes.create_string_buffer(bytes(tag), 16)
                if lib.EVP_CIPHER_CTX_ctrl(ctx, self._SET_TAG, 16, tbuf) != 1:
                    raise BackupCryptoError("EVP set tag failed")
            tail = ctypes.create_string_buffer(32)
            if fin(ctx, tail, ctypes.byref(n)) != 1:
                if enc:
                    raise BackupCryptoError("EVP final failed")
                raise AuthError("authentication failed")
            body = out.raw[:total] + tail.raw[:n.value]
            if enc:
                tbuf = ctypes.create_string_buffer(16)
                if lib.EVP_CIPHER_CTX_ctrl(ctx, self._GET_TAG, 16, tbuf) != 1:
                    raise BackupCryptoError("EVP get tag failed")
                return body + tbuf.raw
            return body
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    def seal(self, key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        return self._run(True, key, nonce, data, aad)

    def open(self, key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        if len(data) < 16:
            raise AuthError("authentication failed")
        return self._run(False, key, nonce, data[:-16], aad, data[-16:])


_BACKENDS = {"cryptography": _Cryptography, "libcrypto": _Libcrypto}
_cache: dict[str, object] = {}


def available_backends() -> list[str]:
    out = []
    for name in _BACKENDS:
        try:
            backend(name)
            out.append(name)
        except (ImportError, BackupCryptoError):
            pass
    return out


def backend(name: str | None = None):
    """The AES-256-GCM implementation: `cryptography` when importable, else libcrypto via ctypes."""
    name = name or os.environ.get("WAM_BACKUP_CRYPTO") or None
    order = [name] if name else ["cryptography", "libcrypto"]
    err: Exception | None = None
    for n in order:
        if n not in _BACKENDS:
            raise BackupCryptoError(f"unknown crypto backend {n!r}")
        if n in _cache:
            return _cache[n]
        try:
            _cache[n] = _BACKENDS[n]()
            return _cache[n]
        except (ImportError, BackupCryptoError) as e:
            err = e
    raise BackupCryptoError(f"no AES-256-GCM implementation available ({err})")


# ---------------------------------------------------------------- the recovery key

PREFIX = "WZRK"
_CHECK = 3


def new_recovery_key() -> bytes:
    return os.urandom(32)


def _checksum(key: bytes) -> bytes:
    return hashlib.sha256(b"wizard-backup recovery key" + key).digest()[:_CHECK]


def encode_recovery_key(key: bytes) -> str:
    s = base64.b32encode(key + _checksum(key)).decode().rstrip("=")
    return PREFIX + "-" + "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def looks_like_recovery_key(text: str) -> bool:
    return text.strip().upper().replace(" ", "").startswith(PREFIX)


def decode_recovery_key(text: str) -> bytes:
    s = "".join(text.split()).upper().replace("-", "")
    if s.startswith(PREFIX):
        s = s[len(PREFIX):]
    s = s.translate(str.maketrans("018", "OIB"))   # digits base32 never uses, read for the letters they resemble
    want = len(base64.b32encode(bytes(32 + _CHECK)).decode().rstrip("="))
    if len(s) != want or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in s):
        raise KeyTypo(f"the recovery key has a typo: expected {want} letters and digits after {PREFIX}, got {len(s)}")
    raw = base64.b32decode(s + "=" * (-len(s) % 8))
    key, chk = raw[:32], raw[32:]
    if not hmac.compare_digest(_checksum(key), chk):
        raise KeyTypo("the recovery key has a typo (its checksum does not match); check each group")
    return key


def key_id(key: bytes) -> str:
    """Public 8-hex id of a recovery key: names which key opens a file without revealing anything about it."""
    return hashlib.sha256(hkdf(key, info=b"id")).hexdigest()[:8]
