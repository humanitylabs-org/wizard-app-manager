"""Read an APK's v2/v3 signing block and check it was signed by one pinned certificate (pure Python, #789).

The phone feed used to trust the `signer` field a release declares about itself. This reads what Android reads:
every v2/v3/v3.1 signer must carry the pinned certificate, its RSA signature over the signed data must verify
against that certificate's key, and the signed content digests must match the APK's bytes. Anything else (no
signing block, another key, a copied certificate with a forged signature, edited contents) is refused.
"""
from __future__ import annotations

import hashlib
import struct

MAGIC = b'APK Sig Block 42'
SCHEMES = {0x7109871a: 'v2', 0xf05368c0: 'v3', 0x1b93ad61: 'v3.1'}
# RSASSA-PKCS1-v1_5 with SHA-256 / SHA-512 (what apksigner uses for RSA keys); content digests use the same hash.
ALGORITHMS = {0x0103: hashlib.sha256, 0x0104: hashlib.sha512}
DIGEST_INFO = {0x0103: bytes.fromhex('3031300d060960864801650304020105000420'),
               0x0104: bytes.fromhex('3051300d060960864801650304020305000440')}
RSA_OID = bytes.fromhex('2a864886f70d010101')
CHUNK = 1024 * 1024
MAX_BLOCK = 16 * 1024 * 1024


class SignatureError(ValueError):
    pass


def _lp(buf, off):
    """One uint32-length-prefixed field: (value, next offset)."""
    if off + 4 > len(buf):
        raise SignatureError('truncated field')
    n = struct.unpack_from('<I', buf, off)[0]
    if off + 4 + n > len(buf):
        raise SignatureError('truncated field')
    return buf[off + 4:off + 4 + n], off + 4 + n


def _seq(buf):
    out, off = [], 0
    while off < len(buf):
        item, off = _lp(buf, off)
        out.append(item)
    return out


def _der(buf, off=0):
    """One DER TLV: (tag, value, next offset)."""
    if off + 2 > len(buf):
        raise SignatureError('bad DER')
    tag, n = buf[off], buf[off + 1]
    off += 2
    if n & 0x80:
        k = n & 0x7f
        if not 0 < k <= 4 or off + k > len(buf):
            raise SignatureError('bad DER length')
        n, off = int.from_bytes(buf[off:off + k], 'big'), off + k
    if off + n > len(buf):
        raise SignatureError('bad DER length')
    return tag, buf[off:off + n], off + n


def _children(value):
    out, off = [], 0
    while off < len(value):
        tag, v, off = _der(value, off)
        out.append((tag, v))
    return out


def cert_spki(cert):
    """The SubjectPublicKeyInfo (full DER) of an X.509 certificate."""
    tag, cert_v, _ = _der(cert)
    if tag != 0x30:
        raise SignatureError('bad certificate')
    tag, tbs, _ = _der(cert_v)
    if tag != 0x30:
        raise SignatureError('bad certificate')
    fields, off = [], 0
    while off < len(tbs):
        start = off
        t, _v, off = _der(tbs, off)
        fields.append((t, tbs[start:off]))
    if fields and fields[0][0] == 0xa0:  # explicit version
        fields = fields[1:]
    # serial, signature algorithm, issuer, validity, subject, subjectPublicKeyInfo
    if len(fields) < 6 or fields[5][0] != 0x30:
        raise SignatureError('bad certificate')
    return fields[5][1]


def rsa_key(spki):
    """(n, e) from an RSA SubjectPublicKeyInfo; anything else is refused."""
    tag, v, _ = _der(spki)
    parts = _children(v) if tag == 0x30 else []
    if len(parts) != 2 or parts[0][0] != 0x30 or parts[1][0] != 0x03:
        raise SignatureError('bad public key')
    alg = _children(parts[0][1])
    if not alg or alg[0] != (0x06, RSA_OID):
        raise SignatureError('not an RSA key')
    bits = parts[1][1]
    if not bits or bits[0] != 0:
        raise SignatureError('bad public key')
    tag, rsa, _ = _der(bits[1:])
    ints = _children(rsa) if tag == 0x30 else []
    if len(ints) != 2 or any(t != 0x02 for t, _ in ints):
        raise SignatureError('bad RSA key')
    n, e = (int.from_bytes(x, 'big') for _, x in ints)
    if n.bit_length() < 2048 or e < 3 or e % 2 == 0:
        raise SignatureError('weak RSA key')
    return n, e


def _rsa_ok(spki, alg, message, signature):
    n, e = rsa_key(spki)
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, 'big')
    if s >= n:
        return False
    digest = DIGEST_INFO[alg] + ALGORITHMS[alg](message).digest()
    expected = b'\x00\x01' + b'\xff' * (k - len(digest) - 3) + b'\x00' + digest
    return pow(s, e, n).to_bytes(k, 'big') == expected


def _layout(f, size):
    """(signing block start, central directory offset, EOCD offset, EOCD bytes, signing block pairs)."""
    tail = min(size, 65535 + 22)
    f.seek(size - tail)
    end = f.read(tail)
    eocd = end.rfind(b'PK\x05\x06')
    if eocd < 0 or len(end) - eocd < 22:
        raise SignatureError('not a ZIP file')
    eocd_bytes = end[eocd:]
    cd_size, cd_off = struct.unpack_from('<II', eocd_bytes, 12)
    eocd_off = size - tail + eocd
    if cd_off + cd_size != eocd_off or cd_off < 32:
        raise SignatureError('APK has no signing block')
    f.seek(cd_off - 24)
    footer = f.read(24)
    if footer[8:] != MAGIC:
        raise SignatureError('APK has no signing block')
    block = struct.unpack_from('<Q', footer)[0]
    start = cd_off - block - 8
    if not 32 <= block <= MAX_BLOCK or start < 0:
        raise SignatureError('bad signing block')
    f.seek(start)
    raw = f.read(block + 8)
    if struct.unpack_from('<Q', raw)[0] != block:
        raise SignatureError('bad signing block')
    pairs, off, body = {}, 8, raw[:-24]
    while off < len(body):
        if off + 12 > len(body):
            raise SignatureError('bad signing block')
        length, pid = struct.unpack_from('<QI', body, off)
        if length < 4 or off + 8 + length > len(body):
            raise SignatureError('bad signing block')
        pairs.setdefault(pid, []).append(body[off + 12:off + 8 + length])
        off += 8 + length
    return start, cd_off, eocd_off, eocd_bytes, pairs


def _content_digest(f, hasher, sections):
    chunks = []
    for section in sections:
        if isinstance(section, bytes):  # in-memory section (the edited EOCD)
            for i in range(0, len(section), CHUNK):
                piece = section[i:i + CHUNK]
                chunks.append(hasher(b'\xa5' + struct.pack('<I', len(piece)) + piece).digest())
            continue
        begin, stop = section
        f.seek(begin)
        while begin < stop:
            piece = f.read(min(CHUNK, stop - begin))
            if not piece:
                raise SignatureError('truncated APK')
            begin += len(piece)
            chunks.append(hasher(b'\xa5' + struct.pack('<I', len(piece)) + piece).digest())
    return hasher(b'\x5a' + struct.pack('<I', len(chunks)) + b''.join(chunks)).digest()


def _signer(scheme, raw):
    """(signed data, digests, first certificate, signatures, public key) of one v2/v3 signer."""
    signed, off = _lp(raw, 0)
    digests, off2 = _lp(signed, 0)
    certs, off2 = _lp(signed, off2)
    if scheme != 'v2':
        # The outer minSdkVersion/maxSdkVersion are not signed; Android refuses the signer unless they equal the
        # signed copies (AOSP ApkSignatureSchemeV3Verifier), so a box must too (Codex P2 on WAM#790).
        if off + 8 > len(raw) or off2 + 8 > len(signed) or raw[off:off + 8] != signed[off2:off2 + 8]:
            raise SignatureError(f'{scheme} signer SDK range does not match its signed data')
        off += 8
    sigs, off = _lp(raw, off)
    key, _ = _lp(raw, off)
    certs = _seq(certs)
    if not certs:
        raise SignatureError('signer has no certificate')
    pairs = lambda b: {struct.unpack_from('<I', x)[0]: _lp(x, 4)[0] for x in _seq(b) if len(x) >= 8}
    return signed, pairs(digests), certs[0], pairs(sigs), key


def signer_digests(path):
    """sha256 of the first certificate of every v2/v3 signer (no verification). For diagnostics."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        pairs = _layout(f, f.tell())[4]
    out = []
    for pid, scheme in SCHEMES.items():
        for block in pairs.get(pid, []):
            for raw in _seq(_lp(block, 0)[0]):
                out.append((scheme, hashlib.sha256(_signer(scheme, raw)[2]).hexdigest()))
    return out


def verify(path, pinned):
    """Raise SignatureError unless every v2/v3 signer of the APK at `path` is the pinned certificate (sha256 hex)
    with a valid RSA signature, and the signed content digests match the file. Returns the schemes checked."""
    pinned = pinned.lower()
    with open(path, 'rb') as f:
        f.seek(0, 2)
        start, cd_off, eocd_off, eocd, pairs = _layout(f, f.tell())
        # The EOCD's central-directory offset is digested as if the signing block were not there.
        eocd_edit = eocd[:16] + struct.pack('<I', start) + eocd[20:]
        sections = [(0, start), (cd_off, eocd_off), eocd_edit]
        checked, cache = [], {}
        for pid, scheme in SCHEMES.items():
            for block in pairs.get(pid, []):
                signers = _seq(_lp(block, 0)[0])
                if not signers:
                    raise SignatureError(f'{scheme} block has no signer')
                for raw in signers:
                    signed, digests, cert, sigs, key = _signer(scheme, raw)
                    if hashlib.sha256(cert).hexdigest() != pinned:
                        raise SignatureError(f'{scheme} signer is not the WizardOS key')
                    if key != cert_spki(cert):
                        raise SignatureError(f'{scheme} public key does not match its certificate')
                    usable = [a for a in sigs if a in ALGORITHMS]
                    if not usable:
                        raise SignatureError(f'{scheme} signer uses no supported signature algorithm')
                    for alg in usable:
                        if not _rsa_ok(key, alg, signed, sigs[alg]):
                            raise SignatureError(f'{scheme} signature does not verify')
                        if alg not in digests:
                            raise SignatureError(f'{scheme} signed data has no content digest')
                        if alg not in cache:
                            cache[alg] = _content_digest(f, ALGORITHMS[alg], sections)
                        if digests[alg] != cache[alg]:
                            raise SignatureError(f'{scheme} content digest does not match the APK')
                checked.append(scheme)
    if not checked:
        raise SignatureError('APK has no v2/v3 signature')
    return checked
