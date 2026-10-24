"""Backup container format: round trip, wrong keys, typos and every kind of tampering are refused (docs/BACKUP.md)."""
import io
import json
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam.backup import crypto, fmt  # noqa: E402

BACKENDS = crypto.available_backends()
FAST_N = 1 << 10   # scrypt cost for tests only (real files use 2^17)


@pytest.fixture(params=BACKENDS)
def aead(request):
    return crypto.backend(request.param)


def _write(data: bytes, keys: fmt.Keys, aead) -> bytes:
    buf = io.BytesIO()
    w = fmt.Writer(buf, keys, scrypt_n=FAST_N, aead=aead)
    for i in range(0, len(data), 300_000):
        w.write(data[i:i + 300_000])
    w.close()
    return buf.getvalue()


def _read(blob: bytes, keys: fmt.Keys, aead) -> bytes:
    r = fmt.open_reader(io.BytesIO(blob), keys, aead=aead)
    out = r.read()
    r.drain()
    return out


def _layout(blob: bytes) -> tuple[int, int]:
    """(offset of the first chunk, number of chunks)."""
    (n,) = struct.unpack(">I", blob[8:12])
    start = 12 + n + 32
    body = len(blob) - start
    return start, -(-body // (fmt.CHUNK + fmt.TAG))


def test_libcrypto_backend_is_always_available():
    assert "libcrypto" in BACKENDS


def test_backends_interoperate():
    if len(BACKENDS) < 2:
        pytest.skip("only one AES-GCM backend here")
    a, b = crypto.backend("libcrypto"), crypto.backend("cryptography")
    k, n = os.urandom(32), os.urandom(12)
    assert b.open(k, n, a.seal(k, n, b"x" * 1000, b"aad"), b"aad") == b"x" * 1000
    assert a.open(k, n, b.seal(k, n, b"y", b""), b"") == b"y"


def test_hkdf_matches_rfc5869_case_1():
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = crypto.hkdf(ikm, salt, info, 42)
    assert okm.hex() == ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
                         "34007208d5b887185865")


@pytest.mark.parametrize("size", [0, 1, fmt.CHUNK - 1, fmt.CHUNK, fmt.CHUNK + 1, 2 * fmt.CHUNK + 17])
def test_round_trip_both_slots(aead, size):
    data = os.urandom(size)
    rk = crypto.new_recovery_key()
    blob = _write(data, fmt.Keys([rk], ["correct horse"]), aead)
    assert _read(blob, fmt.Keys([rk]), aead) == data
    assert _read(blob, fmt.Keys(passphrases=["correct horse"]), aead) == data


def test_header_says_nothing_about_contents(aead):
    rk = crypto.new_recovery_key()
    blob = _write(b"secret apps and agents", fmt.Keys([rk]), aead)
    header, _, _ = fmt.read_header(io.BytesIO(blob))
    assert set(header) == {"format", "cipher", "chunk", "nonce_prefix", "slots"}
    assert header["slots"][0]["key_id"] == crypto.key_id(rk)
    assert b"secret" not in blob


def test_wrong_recovery_key_and_wrong_passphrase_refused_at_unlock(aead):
    rk = crypto.new_recovery_key()
    blob = _write(b"data", fmt.Keys([rk], ["right"]), aead)
    with pytest.raises(fmt.WrongKey, match="wrong key"):
        fmt.open_reader(io.BytesIO(blob), fmt.Keys([crypto.new_recovery_key()]), aead=aead)
    with pytest.raises(fmt.WrongKey, match="wrong key"):
        fmt.open_reader(io.BytesIO(blob), fmt.Keys(passphrases=["wrong"]), aead=aead)


def test_recovery_key_typo_detected_by_checksum():
    rk = crypto.new_recovery_key()
    text = crypto.encode_recovery_key(rk)
    assert text.startswith("WZRK-")
    assert crypto.decode_recovery_key(text.lower().replace("-", " ")) == rk
    i = 10
    typo = text[:i] + ("A" if text[i] != "A" else "B") + text[i + 1:]
    with pytest.raises(crypto.KeyTypo, match="typo"):
        crypto.decode_recovery_key(typo)
    with pytest.raises(crypto.KeyTypo, match="typo"):
        crypto.decode_recovery_key(text[:-2])


def test_header_byte_flip_refused(aead):
    rk = crypto.new_recovery_key()
    blob = bytearray(_write(b"data" * 100, fmt.Keys([rk]), aead))
    raw = bytes(blob)
    header_json = raw[12:12 + struct.unpack(">I", raw[8:12])[0]]
    # flip a byte of the nonce prefix value inside the JSON (still valid JSON, still opens the slot)
    h = json.loads(header_json)
    np_ = h["nonce_prefix"]
    pos = raw.index(np_.encode()) + 1
    blob[pos] = ord("A") if blob[pos] != ord("A") else ord("B")
    with pytest.raises(fmt.Damaged):
        _read(bytes(blob), fmt.Keys([rk]), aead)
    # flip the header MAC
    blob2 = bytearray(raw)
    blob2[12 + len(header_json) + 3] ^= 1
    with pytest.raises(fmt.Damaged, match="header"):
        _read(bytes(blob2), fmt.Keys([rk]), aead)
    # flip the magic
    blob3 = bytearray(raw)
    blob3[0] ^= 1
    with pytest.raises(fmt.NotABackup):
        _read(bytes(blob3), fmt.Keys([rk]), aead)


def test_any_chunk_byte_flip_refused(aead):
    rk = crypto.new_recovery_key()
    blob = _write(os.urandom(2 * fmt.CHUNK + 100), fmt.Keys([rk]), aead)
    start, n = _layout(blob)
    assert n == 3
    for off in (start, start + fmt.CHUNK // 2, start + fmt.CHUNK + fmt.TAG + 5, len(blob) - 1):
        b = bytearray(blob)
        b[off] ^= 0x40
        with pytest.raises(fmt.Damaged):
            _read(bytes(b), fmt.Keys([rk]), aead)


def test_truncation_refused_mid_chunk_and_at_chunk_boundary(aead):
    rk = crypto.new_recovery_key()
    blob = _write(os.urandom(2 * fmt.CHUNK + 100), fmt.Keys([rk]), aead)
    start, _ = _layout(blob)
    mid = blob[:start + fmt.CHUNK // 3]
    boundary = blob[:start + 2 * (fmt.CHUNK + fmt.TAG)]      # exactly before the final chunk
    one = blob[:start + (fmt.CHUNK + fmt.TAG)]               # after the first chunk only
    for cut in (mid, boundary, one, blob[:start], blob[:start - 5]):
        with pytest.raises(fmt.Damaged):
            _read(cut, fmt.Keys([rk]), aead)


def test_exact_multiple_has_a_final_chunk(aead):
    """A payload of exactly N chunks still ends with an authenticated last chunk, so a cut at a boundary is caught."""
    rk = crypto.new_recovery_key()
    blob = _write(os.urandom(2 * fmt.CHUNK), fmt.Keys([rk]), aead)
    start, _ = _layout(blob)
    with pytest.raises(fmt.Damaged):
        _read(blob[:start + fmt.CHUNK + fmt.TAG], fmt.Keys([rk]), aead)
    assert len(_read(blob, fmt.Keys([rk]), aead)) == 2 * fmt.CHUNK


def test_appended_bytes_refused(aead):
    rk = crypto.new_recovery_key()
    for size in (10, fmt.CHUNK + 5):
        blob = _write(os.urandom(size), fmt.Keys([rk]), aead)
        for extra in (b"\x00", os.urandom(fmt.CHUNK + fmt.TAG)):
            with pytest.raises(fmt.Damaged):
                _read(blob + extra, fmt.Keys([rk]), aead)


def test_chunk_reorder_and_drop_refused(aead):
    rk = crypto.new_recovery_key()
    blob = _write(os.urandom(2 * fmt.CHUNK + 100), fmt.Keys([rk]), aead)
    start, _ = _layout(blob)
    c = fmt.CHUNK + fmt.TAG
    head, c1, c2, c3 = blob[:start], blob[start:start + c], blob[start + c:start + 2 * c], blob[start + 2 * c:]
    for bad in (head + c2 + c1 + c3, head + c1 + c1 + c3, head + c2 + c3, head + c1 + c3):
        with pytest.raises(fmt.Damaged):
            _read(bad, fmt.Keys([rk]), aead)


def test_chunk_from_another_file_refused(aead):
    rk = crypto.new_recovery_key()
    a = _write(b"a" * 100, fmt.Keys([rk]), aead)
    b = _write(b"b" * 100, fmt.Keys([rk]), aead)
    sa, _ = _layout(a)
    sb, _ = _layout(b)
    with pytest.raises(fmt.Damaged):
        _read(a[:sa] + b[sb:], fmt.Keys([rk]), aead)


def test_scrypt_uses_explicit_maxmem_and_rejects_silly_params():
    assert len(crypto.scrypt("pw", b"s" * 16, 1 << 10)) == 32
    with pytest.raises(crypto.AuthError):
        crypto.scrypt("pw", b"s" * 16, 3)
