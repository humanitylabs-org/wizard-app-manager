#!/usr/bin/env python3
"""QA-only guest content gate for images the real installer executes.

Why: the retained guest base disk carries the pinned manager image id with a
damaged stored layer (/app/main.mjs is 0 bytes). `docker load` and install.sh's
`image inspect || pull` shortcut keep that copy, so earlier native runs measured
the damaged copy, not the product. This gate measures the executable bytes with
`docker run --rm --pull=never --entrypoint sh <id> -c 'wc -c < F; sha256sum F'`
and, only for the exact empty/damaged value, evicts exactly the bound manager id
(PINNED tag first, owned containers, `image rm --no-prune -f <id>`, never prune)
and reloads the hash-bound pinned archive using the probe-r3 guest functions
(manager_probe_guest.remove_bound_image / load_fresh), then re-measures and
FAILS CLOSED when still wrong. Any other value, a failed or unparsable
measurement, missing authority or a Service mismatch also fails closed with no
eviction. Export carries sizes, hashes, ids, exits and path names only.

Product bytes are untouched. Import is lazy so host-side validate_export works
without the guest probe module.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path('/root/storage-contract-a')
HERE = Path(__file__).resolve().parent
PINNED = 'privsep-wam:595523fb'
BOUND_ID = 'sha256:94cc09235bd65370eff851207b7ee52335098f078ed4f0a564d38d5aea7a5928'
MANAGER_PATH = '/app/main.mjs'
MANAGER = {'bytes': 6908459, 'sha256': 'b824d438aa844d0f1920c0e29a04f8f82986a81bbf812b53b5b8399b9a62ebbe'}
# Second executable of the same image that production_guest.py runs from it (read from the pinned archive).
HELPER_PATH = '/app/libexec/wizard-bind-metadata'
HELPER = {'bytes': 9904, 'sha256': 'c9590812ad90f8afb61312fc8299bc357f6224b52e8df4a7b41be044cbeffc32'}
EMPTY = {'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
# Executables the Service image runs (from the hash-bound service archive 7b6e3206...).
SERVICE_PATHS = {'/app/server/index.mjs': {'bytes': 17337, 'sha256': 'b339d355ae9ccbe1a6aadcfc875aa265bfaa3e161ed4cac93e4be542803108ba'},
                 '/sbin/tini': {'bytes': 23576, 'sha256': '1358f1be32dc2a0dd8084dbda675c3b3dde8352b519b7b8a65573262551ad0fc'}}
LABELS = ('before-installer-start', 'before-install-sh', 'before-service-cases')
PATHS = ('already-correct', 'exact-evict-and-reload', 'refused-no-eviction')
REPORT_NAME = 'image-integrity.json'


def module(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    assert s is not None and s.loader is not None
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


def new_report():
    return {'schema': 'privsep-install-integrity-v1', 'gates': [], 'commands': [], 'owned_ids': [],
            'owned_images': [], 'baseline_images': [], 'phases': []}


def parse_measure(raw, path):
    """Strict by design: an integrity proof, not a degrading collector."""
    m = re.fullmatch(rb'[ \t]*([0-9]{1,12})\n([0-9a-f]{64})  ' + re.escape(path.encode()) + rb'\n', raw)
    assert m is not None, 'integrity measurement incomplete'
    return {'bytes': int(m[1]), 'sha256': m[2].decode()}


def measure(P, image, path):
    assert re.fullmatch('sha256:[0-9a-f]{64}', image) and (path in (MANAGER_PATH, HELPER_PATH) or path in SERVICE_PATHS)
    argv = ['docker', 'run', '--rm', '--pull=never', '--no-healthcheck', '--network', 'none', '--entrypoint', 'sh',
            image, '-c', 'wc -c < ' + path + '; sha256sum ' + path]
    return parse_measure(P.run('integrity-measure', argv)[1], path)


def gate(label, service=False, allow_evict=True, root=None):
    """Run one gate; the row is persisted before and after every step."""
    assert label in LABELS
    root = Path(root or ROOT)
    path = root / REPORT_NAME
    P = module('integrity_probe_r3', HERE / 'manager_probe_guest.py')
    report = json.loads(path.read_text()) if path.exists() else new_report()
    setattr(P, 'ROOT', root); setattr(P, 'report', report)

    def save():
        path.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n'); path.chmod(0o600)
    setattr(P, 'save', save)
    row = {'label': label, 'path': 'pending', 'passed': False, 'before': None, 'after': None,
           'helper_before': None, 'helper_after': None, 'allow_evict': bool(allow_evict), 'error': None}
    report['gates'].append(row); save()
    try:
        assert re.fullmatch('sha256:[0-9a-f]{64}', BOUND_ID) and P.C.BOUND_ID == BOUND_ID and P.PINNED == PINNED
        backend = P.bound_backend()            # archive sha + final diff id from the sealed backend.json
        assert (root / 'backend.tar.gz').is_file()
        with (root / 'backend.tar.gz').open('rb') as f:
            assert P.C.sha_file(f)['sha256'] == P.ARCHIVE_SHA == backend['archive_sha256']
        report['baseline_images'] = P.image_ids()
        assert BOUND_ID in report['baseline_images'], 'bound manager image not present'
        assert P.docker_json('integrity-bound-id', ['image', 'inspect', '--format', '{{json .Id}}', BOUND_ID]) == BOUND_ID
        report['baseline_id'] = BOUND_ID
        row['before'] = measure(P, BOUND_ID, MANAGER_PATH)
        row['helper_before'] = measure(P, BOUND_ID, HELPER_PATH); save()
        if row['before'] == MANAGER and row['helper_before'] == HELPER:
            row['path'] = 'already-correct'; row['after'] = dict(row['before']); row['helper_after'] = dict(row['helper_before'])
        elif allow_evict and EMPTY in (row['before'], row['helper_before']) and \
                row['before'] in (MANAGER, EMPTY) and row['helper_before'] in (HELPER, EMPTY):
            row['path'] = 'exact-evict-and-reload'; save()
            P.remove_bound_image()              # probe-r3: PINNED tag, owned containers, rm --no-prune -f exact id
            P.load_fresh(None)                  # probe-r3: hash-bound archive, exact id readback, retag, inventory equal
            row['after'] = measure(P, BOUND_ID, MANAGER_PATH)
            row['helper_after'] = measure(P, BOUND_ID, HELPER_PATH); save()
            assert row['after'] == MANAGER and row['helper_after'] == HELPER, 'fresh manager image still wrong'
        else:
            row['path'] = 'refused-no-eviction'; save()
            raise AssertionError('manager content is not the pinned value and eviction is not authorised here')
        if service:
            sid = json.loads((root / 'service-attestation-before.json').read_text())['engine_image_id']
            assert P.docker_json('integrity-service-id', ['image', 'inspect', '--format', '{{json .Id}}', sid]) == sid
            row['service'] = {p: measure(P, sid, p) for p in SERVICE_PATHS}; save()
            assert row['service'] == SERVICE_PATHS, 'Service executable differs from the pinned archive'
        row['passed'] = True
        return row
    except BaseException as e:
        row['error'] = type(e).__name__
        raise
    finally:
        save()


def validate_export(doc, require=()):
    """Host-side: closed key set, hash-only content, every gate passed, final bytes pinned."""
    assert doc.get('schema') == 'privsep-install-integrity-v1' and doc['gates']
    allowed = {'label', 'path', 'passed', 'before', 'after', 'helper_before', 'helper_after', 'allow_evict', 'error', 'service'}
    for row in doc['gates']:
        assert set(row) <= allowed and row['label'] in LABELS and row['path'] in PATHS
        assert row['passed'] is True and row['error'] is None
        assert row['after'] == MANAGER and row['helper_after'] == HELPER
        assert row['before'] in (MANAGER, EMPTY) and row['helper_before'] in (HELPER, EMPTY)
        correct = row['before'] == MANAGER and row['helper_before'] == HELPER
        assert (row['path'] == 'already-correct') == correct and row['path'] != 'refused-no-eviction'
        if row['path'] == 'exact-evict-and-reload':
            assert row['allow_evict'] is True and EMPTY in (row['before'], row['helper_before'])
        if 'service' in row:
            assert row['service'] == SERVICE_PATHS
    assert [r['label'] for r in doc['gates']][:len(require)] == list(require)
    assert doc['owned_ids'] == []
    return {'gates': [{k: r[k] for k in ('label', 'path', 'before', 'after', 'helper_before', 'helper_after')} for r in doc['gates']],
            'evicted': any(r['path'] == 'exact-evict-and-reload' for r in doc['gates'])}


if __name__ == '__main__':
    assert len(sys.argv) >= 2
    gate(sys.argv[1], service='service' in sys.argv[2:], allow_evict='verify' not in sys.argv[2:])
