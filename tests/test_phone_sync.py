import fcntl
import functools
import hashlib
import io
import os
import json
import random
import struct
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import apk_sig, cli, phone_sync as ps


# ---- Real v2/v3-signed APKs for the tests, built in pure Python with throwaway keys (no key is committed). ----

def _prime(rng, bits):
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and all(
                _mr(n, rng.randrange(2, n - 2)) for _ in range(24)):
            return n


def _mr(n, a):
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    x = pow(a, d, n)
    if x in (1, n - 1):
        return True
    for _ in range(r - 1):
        x = pow(x, 2, n)
        if x == n - 1:
            return True
    return False


def _tlv(tag, value):
    n = len(value)
    head = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([tag]) + head + value


def _int(v):
    b = v.to_bytes(v.bit_length() // 8 + 1, 'big')
    return _tlv(0x02, b)


@functools.lru_cache(maxsize=None)
def _key(seed):
    """(n, e, d, certificate DER) for a deterministic throwaway 2048-bit RSA key."""
    rng, e = random.Random(seed), 65537
    while True:
        p, q = _prime(rng, 1024), _prime(rng, 1024)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            break
    n, d = p * q, pow(e, -1, phi)
    rsa_alg = _tlv(0x30, _tlv(0x06, apk_sig.RSA_OID) + b'\x05\x00')
    spki = _tlv(0x30, rsa_alg + _tlv(0x03, b'\x00' + _tlv(0x30, _int(n) + _int(e))))
    name = _tlv(0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, bytes.fromhex('550403')) + _tlv(0x0c, seed.encode()))))
    validity = _tlv(0x30, _tlv(0x17, b'260101000000Z') + _tlv(0x17, b'510101000000Z'))
    sig_alg = _tlv(0x30, _tlv(0x06, bytes.fromhex('2a864886f70d01010b')) + b'\x05\x00')
    tbs = _tlv(0x30, _tlv(0xa0, _int(2)) + _int(1) + sig_alg + name + validity + name + spki)
    return n, e, d, _tlv(0x30, tbs + sig_alg + _tlv(0x03, b'\x00' + b'\x01' * 256)), spki


def _sign(key, message):
    n, _e, d = key[:3]
    k = (n.bit_length() + 7) // 8
    digest = apk_sig.DIGEST_INFO[0x0103] + hashlib.sha256(message).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(digest) - 3) + b'\x00' + digest
    return pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big')


def _lp(b):
    return struct.pack('<I', len(b)) + b


def build_apk(seed='wizardos-test', *, cert_seed=None, schemes=('v2', 'v3'), payload=b'classes.dex bytes'):
    """A small APK signed with key `seed`; cert_seed puts another key's certificate in the signed data."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('AndroidManifest.xml', b'manifest')
        z.writestr('classes.dex', payload)
    raw = buf.getvalue()
    eocd = raw.rfind(b'PK\x05\x06')
    cd_off = struct.unpack_from('<I', raw, eocd + 16)[0]
    entries, cd, end = raw[:cd_off], raw[cd_off:eocd], raw[eocd:]
    chunks = [hashlib.sha256(b'\xa5' + struct.pack('<I', len(x)) + x).digest() for x in (entries, cd, end) if x]
    content = hashlib.sha256(b'\x5a' + struct.pack('<I', len(chunks)) + b''.join(chunks)).digest()
    key, cert = _key(seed), _key(cert_seed or seed)[3]
    pairs = b''
    for scheme in schemes:
        pid = {'v2': 0x7109871a, 'v3': 0xf05368c0}[scheme]
        sdk = struct.pack('<II', 24, 0x7fffffff) if scheme == 'v3' else b''
        signed = _lp(_lp(struct.pack('<I', 0x0103) + _lp(content))) + _lp(_lp(cert)) + sdk + _lp(b'')
        signer = _lp(signed) + sdk + _lp(_lp(struct.pack('<I', 0x0103) + _lp(_sign(key, signed)))) + _lp(_key(cert_seed or seed)[4])
        value = _lp(_lp(signer))
        pairs += struct.pack('<QI', len(value) + 4, pid) + value
    size = len(pairs) + 8 + 16
    block = struct.pack('<Q', size) + pairs + struct.pack('<Q', size) + apk_sig.MAGIC
    end = end[:16] + struct.pack('<I', cd_off + len(block)) + end[20:]
    return entries + block + cd + end


def _signed_digest(seed='wizardos-test'):
    return hashlib.sha256(_key(seed)[3]).hexdigest()


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    home, root = tmp_path / 'home', tmp_path / 'runtipi'
    home.mkdir()
    (home / 'release.json').write_text(json.dumps({'status': 'pinned', 'release': '0.3.40'}))
    # The fixture APK is really signed by a throwaway "WizardOS" key; SIGNER pins that key's certificate.
    monkeypatch.setattr(ps, 'SIGNER', _signed_digest())
    state = {'apk': build_apk(), 'hits': [], 'status': 200, 'redirect': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state['hits'].append(self.path)
            if self.path == '/latest.json':
                body = json.dumps(state['pointer']).encode()
                self.send_response(state['status'])
            elif self.path == '/release.json':
                body = json.dumps(state.get('release')).encode()
                self.send_response(200 if state.get('release') else 404)
            elif self.path == '/redirect.apk':
                self.send_response(302); self.send_header('Location', '/phone.apk'); self.end_headers(); return
            else:
                body = state['apk']
                self.send_response(200)
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    state['pointer'] = {'app': 'ai.wizards.os', 'versionCode': 78, 'versionName': '0.10.14',
                        'sha256': hashlib.sha256(state['apk']).hexdigest(), 'bytes': len(state['apk']),
                        'signer': ps.SIGNER, 'asset_url': origin + '/phone.apk', 'notes': 'Fixture release',
                        'min_box_snapshot': '0.3.40', 'source_commit': 'a' * 40}
    state.update(home=home, root=root, feed=root / 'app-data/wizard-os/phone', url=origin + '/latest.json')
    state['run'] = lambda: ps.sync(home, root, pointer_url=state['url'], fixture=True)
    yield SimpleNamespace(**state, state=state)
    server.shutdown(); server.server_close(); thread.join()


def test_mirror_idempotent_and_relative_feed(mirror):
    m = mirror
    assert m.run() == 'updated:versionCode=78'
    feed = json.loads((m.feed / 'latest.json').read_text())
    assert feed == {k: m.pointer[k] for k in ('app', 'versionCode', 'versionName', 'bytes', 'sha256', 'signer', 'notes')} | {
        'version': '0.10.14', 'url': 'wizard-os-0.10.14.apk'}
    apk = m.feed / feed['url']
    assert apk.read_bytes() == m.apk
    assert apk.stat().st_mode & 0o777 == 0o644
    assert m.feed.stat().st_mode & 0o777 == 0o755
    assert m.run() == 'unchanged'
    assert m.state['hits'].count('/phone.apk') == 1


def test_tampered_sha_rejected(mirror):
    mirror.pointer['sha256'] = '0' * 64
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not (mirror.feed / 'latest.json').exists()
    assert list(mirror.feed.glob('*.apk')) == []
    assert list(mirror.feed.glob('.phone-*')) == []


def test_pointer_404_noop(mirror):
    mirror.state['status'] = 404
    assert mirror.run() == 'skipped:pointer_404'
    assert not mirror.feed.exists()


@pytest.mark.parametrize('installed', ['0.3.39', '0.3.40-rc.1', None])
def test_min_snapshot_gate(mirror, installed):
    (mirror.home / 'release.json').write_text(json.dumps({'status': 'pinned', 'release': installed}))
    assert mirror.run() == 'skipped:min_box_snapshot'
    assert not mirror.feed.exists()
    assert mirror.state['hits'] == ['/latest.json']


def test_never_downgrades(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    before = (mirror.feed / 'latest.json').read_bytes()
    mirror.pointer['versionCode'] = 77
    assert mirror.run() == 'skipped:no_downgrade'
    assert (mirror.feed / 'latest.json').read_bytes() == before
    assert mirror.state['hits'].count('/phone.apk') == 1


def test_invalid_new_release_preserves_old(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    before = (mirror.feed / 'latest.json').read_bytes()
    mirror.pointer.update(versionCode=79, versionName='0.10.15', sha256='0' * 64)
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert (mirror.feed / 'latest.json').read_bytes() == before
    assert list(p.name for p in mirror.feed.glob('*.apk')) == ['wizard-os-0.10.14.apk']


def test_upgrade_cleans_only_superseded_apks(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    (mirror.feed / 'keep.txt').write_text('owner file')
    mirror.pointer.update(versionCode=79, versionName='0.10.15')
    assert mirror.run() == 'updated:versionCode=79'
    assert list(p.name for p in mirror.feed.glob('*.apk')) == ['wizard-os-0.10.15.apk']
    assert (mirror.feed / 'keep.txt').exists()


def test_corrupt_local_apk_repaired(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    (mirror.feed / 'wizard-os-0.10.14.apk').write_bytes(b'corrupt')
    assert mirror.run() == 'updated:versionCode=78'
    assert (mirror.feed / 'wizard-os-0.10.14.apk').read_bytes() == mirror.apk


def test_immutable_apk_collision_rejected(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    mirror.pointer.update(versionCode=79, sha256='0' * 64)
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert (mirror.feed / 'wizard-os-0.10.14.apk').read_bytes() == mirror.apk


def test_github_style_redirect_followed(mirror):
    mirror.pointer['asset_url'] = mirror.url.replace('/latest.json', '/redirect.apk')
    assert mirror.run() == 'updated:versionCode=78'
    assert '/redirect.apk' in mirror.state['hits'] and '/phone.apk' in mirror.state['hits']


@pytest.mark.parametrize('key,value', [('app', 'other.app'), ('versionCode', True), ('versionCode', -1),
                                      ('bytes', ps.MAX_APK + 1), ('sha256', 'bad'), ('signer', '0' * 64),
                                      ('source_commit', 'bad'), ('versionName', '../bad'),
                                      ('min_box_snapshot', 'bad'), ('notes', []),
                                      ('asset_url', 'https://evil.test/app.apk')])
def test_pointer_field_validation(mirror, key, value):
    mirror.pointer[key] = value
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not mirror.feed.exists()


def test_small_pointer_cap(mirror, monkeypatch):
    monkeypatch.setattr(ps, 'MAX_POINTER', 16)
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not mirror.feed.exists()


def test_actual_size_mismatch(mirror):
    mirror.pointer['bytes'] += 1
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert not (mirror.feed / 'latest.json').exists()
    assert list(mirror.feed.glob('*.apk')) == []


def test_offline_is_successful_noop(mirror):
    assert ps.sync(mirror.home, mirror.root, pointer_url='http://127.0.0.1:1/latest.json', fixture=True) == 'skipped:unavailable_or_invalid'


def test_timer_always_enabled_even_check_mode(mirror, tmp_path):
    (mirror.home / 'maintain.json').write_text('{"mode":"check"}')
    (mirror.home / 'updates.json').write_text('{"mode":"ask"}')
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    units = tmp_path / 'units'
    assert ps.ensure_timer(mirror.home, mirror.root, unit_dir=units, runner=runner) == 'timer:wizard-phone-sync.timer'
    timer = (units / 'wizard-phone-sync.timer').read_text()
    service = (units / 'wizard-phone-sync.service').read_text()
    assert 'OnCalendar=hourly' in timer and 'Persistent=true' in timer
    assert ' phone-sync\n' in service and 'maintain' not in service and '--apply' not in service
    assert f'RUNTIPI_ROOT={mirror.root}' in service and f'WIZARD_OS_HOME={mirror.home}' in service
    assert calls == [['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', '--now', 'wizard-phone-sync.timer']]
    assert json.loads((mirror.home / 'updates.json').read_text()) == {'mode': 'ask'}


def test_cli_one_line_exit_zero_on_noop(mirror, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Config', lambda: SimpleNamespace(home=mirror.home, runtipi_root=mirror.root))
    monkeypatch.setattr(ps, 'sync', lambda *a: 'skipped:pointer_404')
    assert cli.main(['phone-sync']) == 0
    assert capsys.readouterr().out == 'phone-sync skipped:pointer_404\n'


def test_install_wires_timer_independently():
    text = (Path(__file__).parents[1] / 'install.sh').read_text()
    assert 'RUNTIPI_ROOT="$RUNTIPI_ROOT" "$HOME/.local/bin/wam" phone-sync --ensure-timer' in text


def test_publication_orders_apk_before_feed(mirror, monkeypatch):
    original = ps._atomic_json
    def publish(path, value):
        assert (path.parent / value['url']).read_bytes() == mirror.apk
        assert not path.exists()
        return original(path, value)
    monkeypatch.setattr(ps, '_atomic_json', publish)
    assert mirror.run() == 'updated:versionCode=78'


def test_overlapping_runs_skip(mirror):
    mirror.feed.mkdir(parents=True)
    with (mirror.feed / '.sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mirror.run() == 'skipped:busy'
    assert mirror.state['hits'] == ['/latest.json']


def test_restrictive_umask_still_publishes_readable_files(mirror):
    mask = os.umask(0o077)
    try:
        assert mirror.run() == 'updated:versionCode=78'
    finally:
        os.umask(mask)
    assert mirror.feed.parent.stat().st_mode & 0o777 == 0o755
    assert mirror.feed.stat().st_mode & 0o777 == 0o755
    for name in ['latest.json', 'wizard-os-0.10.14.apk']:
        assert (mirror.feed / name).stat().st_mode & 0o777 == 0o644


def test_production_asset_contract(mirror):
    pointer = dict(mirror.pointer, asset_url='https://github.com/humanitylabs-org/wizard-ux/releases/download/android-v0.10.14/wizard-os-0.10.14.apk')
    assert ps.validate(pointer) == pointer
    with pytest.raises(ValueError):
        ps.validate(mirror.pointer)


def test_default_pointer_is_public_mirror_beside_box_snapshots():
    # wizard-os is private: a pointer there 404s on every box (regression, 31 Oct 2026).
    from wam import updates
    assert ps.POINTER == updates.RELEASE_BASE + '/phone/latest.json'


def test_published_mirror_pointer_validates():
    import json
    pointer = json.loads((Path(__file__).resolve().parents[1] / 'releases/phone/latest.json').read_text())
    assert ps.validate(pointer)['app'] == 'ai.wizards.os'


# wizard-ux#1399: CI publishes android-v<version> on wizard-ux but cannot edit the mirror pointer, so boxes also
# read the newest release's phone.json and mirror whichever valid release is newest.
def _release(m, code, name, **extra):
    m.state['release'] = dict(m.pointer, versionCode=code, versionName=name, **extra)
    return lambda: ps.sync(m.home, m.root, pointer_url=m.url, release_url=m.url.replace('/latest.json', '/release.json'), fixture=True)


def test_newer_ci_release_is_mirrored_over_stale_pointer(mirror):
    run = _release(mirror, 98, '0.10.34')
    assert run() == 'updated:versionCode=98'
    feed = json.loads((mirror.feed / 'latest.json').read_text())
    assert (feed['versionCode'], feed['url']) == (98, 'wizard-os-0.10.34.apk')
    assert run() == 'unchanged'


def test_older_release_never_beats_pointer(mirror):
    run = _release(mirror, 70, '0.10.6')
    assert run() == 'updated:versionCode=78'


def test_missing_release_falls_back_to_pointer(mirror):
    run = _release(mirror, 115, '0.10.51')
    mirror.state['release'] = None
    assert run() == 'updated:versionCode=78'


def test_release_used_when_pointer_missing(mirror):
    run = _release(mirror, 115, '0.10.51')
    mirror.state['status'] = 404
    assert run() == 'updated:versionCode=115'


def test_invalid_release_is_ignored(mirror):
    run = _release(mirror, 115, '0.10.51', signer='0' * 64)
    assert run() == 'updated:versionCode=78'


def test_release_needing_newer_box_falls_back_to_pointer(mirror):
    run = _release(mirror, 115, '0.10.51', min_box_snapshot='9.0.0')
    assert run() == 'updated:versionCode=78'


def test_default_sync_reads_pointer_and_newest_release(mirror, monkeypatch):
    seen = []
    def fake(opener, url, fixture):
        seen.append(url)
        raise ValueError('offline')
    monkeypatch.setattr(ps, '_pointer', fake)
    assert ps.sync(mirror.home, mirror.root) == 'skipped:unavailable_or_invalid'
    assert seen == [ps.POINTER, ps.RELEASE_POINTER]
    assert ps.RELEASE_POINTER == 'https://github.com/humanitylabs-org/wizard-ux/releases/latest/download/phone.json'


def test_ci_release_phone_json_validates():
    # The phone.json the wizard-ux Phone release workflow published for android-v0.10.51.
    released = {'app': 'ai.wizards.os', 'versionCode': 115, 'versionName': '0.10.51',
                'sha256': '8f67e8b81a96766a74e1317bca4d372ee0ef64d58021c2a88e83af4f1ad85266', 'bytes': 29219224,
                'signer': ps.SIGNER,
                'asset_url': 'https://github.com/humanitylabs-org/wizard-ux/releases/download/android-v0.10.51/wizard-os-0.10.51.apk',
                'notes': 'Shared WizardOS phone release 0.10.51, built and signed from main; no owner address is built in.',
                'min_box_snapshot': '0.3.40', 'source_commit': 'e9d143a4a34a389adbaf841b037302948572501f'}
    assert ps.validate(released) == released


def test_newest_release_with_bad_apk_falls_back_to_pointer(mirror):
    # Codex P2 on WAM#779: metadata of the newest source validates but its APK bytes do not.
    run = _release(mirror, 90, '0.10.26', sha256='1' * 64)
    assert run() == 'updated:versionCode=78'
    assert [p.name for p in mirror.feed.glob('*.apk')] == ['wizard-os-0.10.14.apk']
    assert list(mirror.feed.glob('.phone-*')) == []


# ---- wizard-app-manager#789: the APK's real signer decides, and a CI release can't run far ahead of review. ----

def _feed(m):
    return json.loads((m.feed / 'latest.json').read_text()) if (m.feed / 'latest.json').exists() else None


def _serve(m, apk):
    m.state['apk'] = apk
    m.pointer.update(sha256=hashlib.sha256(apk).hexdigest(), bytes=len(apk))


def test_real_signed_fixture_verifies(tmp_path):
    path = tmp_path / 'a.apk'
    path.write_bytes(build_apk())
    assert apk_sig.verify(path, _signed_digest()) == ['v2', 'v3']
    assert apk_sig.signer_digests(path) == [('v2', _signed_digest()), ('v3', _signed_digest())]


def test_v2_only_apk_is_accepted(mirror):
    _serve(mirror, build_apk(schemes=('v2',)))
    assert mirror.run() == 'updated:versionCode=78'


@pytest.mark.parametrize('apk', [
    pytest.param(lambda: build_apk('attacker'), id='other-key'),
    pytest.param(lambda: build_apk('attacker', cert_seed='wizardos-test'), id='copied-cert-forged-signature'),
    pytest.param(lambda: build_apk(schemes=()), id='no-v2-v3-signature'),
    pytest.param(lambda: build_apk().replace(b'classes.dex bytes', b'classes.dex evil!'), id='edited-after-signing'),
])
def test_apk_signed_by_another_key_is_refused(mirror, apk):
    # Correct sha256, size and the pinned `signer` field: only the APK's own signing block tells them apart.
    _serve(mirror, apk())
    assert mirror.pointer['signer'] == ps.SIGNER
    assert mirror.run() == 'skipped:unavailable_or_invalid'
    assert _feed(mirror) is None
    assert list(mirror.feed.glob('*.apk')) == [] and list(mirror.feed.glob('.phone-*')) == []


def test_foreign_ci_release_falls_back_to_reviewed_pointer(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    good = mirror.state['apk']
    run = _release(mirror, 79, '0.10.15')
    forged = build_apk('attacker')
    mirror.state['apk'] = forged  # the fixture serves it for every APK URL
    mirror.state['release'].update(sha256=hashlib.sha256(forged).hexdigest(), bytes=len(forged))
    assert run() == 'unchanged'
    assert _feed(mirror)['versionCode'] == 78
    assert [p.name for p in mirror.feed.glob('*.apk')] == ['wizard-os-0.10.14.apk']
    assert (mirror.feed / 'wizard-os-0.10.14.apk').read_bytes() == good


def test_mirrored_apk_with_a_foreign_signer_is_not_left_unchanged(mirror, monkeypatch):
    assert mirror.run() == 'updated:versionCode=78'
    monkeypatch.setattr(ps, 'SIGNER', _signed_digest('someone-else'))
    assert mirror.run() == 'skipped:unavailable_or_invalid'


def test_forged_versioncode_ci_release_is_ignored(mirror):
    # A "latest" wizard-ux release more than MAX_CI_LEAD above the reviewed pointer never reaches the feed ...
    run = _release(mirror, 78 + ps.MAX_CI_LEAD + 1, '0.10.99')
    assert run() == 'updated:versionCode=78'
    assert run() == 'unchanged'
    # ... so a later real release still gets through (no skipped:no_downgrade freeze).
    mirror.state['release'].update(versionCode=79, versionName='0.10.15')
    assert run() == 'updated:versionCode=79'


def test_ci_release_at_the_cap_is_mirrored(mirror):
    run = _release(mirror, 78 + ps.MAX_CI_LEAD, '0.10.34')
    assert run() == f'updated:versionCode={78 + ps.MAX_CI_LEAD}'


def test_feed_frozen_by_a_forged_counter_recovers(mirror):
    # A box that mirrored a huge versionCode before #789: the reviewed pointer lifts it back down.
    mirror.feed.mkdir(parents=True)
    (mirror.feed / 'latest.json').write_text(json.dumps({'versionCode': 2147483000, 'url': 'wizard-os-9.9.9.apk', 'sha256': '0' * 64}))
    (mirror.feed / 'wizard-os-9.9.9.apk').write_bytes(b'forged')
    assert mirror.run() == 'updated:versionCode=78'
    assert [p.name for p in mirror.feed.glob('*.apk')] == ['wizard-os-0.10.14.apk']


def test_pointer_offline_caps_ci_release_against_mirrored_feed(mirror):
    assert mirror.run() == 'updated:versionCode=78'
    mirror.state['status'] = 404
    run = _release(mirror, 78 + ps.MAX_CI_LEAD + 1, '0.10.99')
    assert run() == 'skipped:ci_release_too_far_ahead'
    assert _feed(mirror)['versionCode'] == 78
    mirror.state['release'].update(versionCode=80, versionName='0.10.16')
    assert run() == 'updated:versionCode=80'
