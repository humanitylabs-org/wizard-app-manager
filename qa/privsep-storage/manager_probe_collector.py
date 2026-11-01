#!/usr/bin/env python3
"""Pure, metadata-only archive/measurement collector. No Docker, extraction or app execution."""
import hashlib
import json
import re
import tarfile

BUNDLE_SIZE = 6908459
BUNDLE_SHA256 = 'b824d438aa844d0f1920c0e29a04f8f82986a81bbf812b53b5b8399b9a62ebbe'
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
PATHS = ('app/main.mjs', 'app/main.js')
METHODS = ('run', 'cp', 'export', 'readonly')
CYCLES = ('as_found', 'remove_and_fresh_load')
BOUND_ID = 'sha256:94cc09235bd65370eff851207b7ee52335098f078ed4f0a564d38d5aea7a5928'
LAST_DIFF_ID = 'sha256:4a7d6b8643f5740cfd307bab4e97cec976dcd0a6e8d6facf80fff867a630d482'
VERDICTS = ('BASELINE_IMAGE_DAMAGED_FRESH_LOAD_OK', 'FRESH_LOAD_ALSO_BAD',
            'BASELINE_OK_FRESH_OK', 'BASELINE_OK_FRESH_BAD', 'INCONSISTENT')
CAP = 4 * 1024**3


def need(ok):
    if not ok:
        raise ValueError('probe-schema-refused')


def sha_file(f):
    h = hashlib.sha256(); n = 0
    for b in iter(lambda: f.read(1048576), b''):
        n += len(b); need(n <= CAP); h.update(b)
    return {'state': 'file', 'size': n, 'sha256': h.hexdigest()}


def absent():
    return {'state': 'absent', 'size': None, 'sha256': None}


def unparsed(raw):
    """Hash unknown data; never export its paths, tool text, or private fields."""
    if isinstance(raw, str): raw = raw.encode('utf-8')
    if not isinstance(raw, bytes):
        raw = json.dumps(raw, sort_keys=True, ensure_ascii=True).encode()
    return {'unparsed': True, 'sha256': hashlib.sha256(raw).hexdigest(), 'length': len(raw)}


def is_unknown(v):
    return isinstance(v, dict) and v.get('unparsed') is True


def unknown_file(raw):
    return {'state': 'unknown', **unparsed(raw)}


def files_from_tar(fileobj):
    try:
        return _files_from_tar(fileobj)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError, EOFError, tarfile.TarError):
        return {p: unknown_file(None) for p in PATHS}


def _files_from_tar(fileobj):
    """Streaming only. Reject duplicates/nonregular targets rather than guessing order."""
    out = {p: absent() for p in PATHS}; seen = set()
    with tarfile.open(fileobj=fileobj, mode='r|*') as t:
        for m in t:
            name = m.name.removeprefix('./').removeprefix('/')
            if name not in PATHS:
                continue
            if name in seen or not m.isfile() or not 0 <= m.size <= CAP:
                out[name] = unknown_file({'duplicate': name in seen, 'type': m.type.decode('ascii', 'replace'), 'size': m.size})
                seen.add(name); continue
            seen.add(name)
            try:
                out[name] = sha_file(t.extractfile(m))
                if out[name]['size'] != m.size: out[name] = unknown_file({'size': m.size})
            except (ValueError, OSError, EOFError, tarfile.TarError):
                out[name] = unknown_file({'size': m.size})
    return out


def archive(path):
    try:
        return _archive(path)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError, EOFError, tarfile.TarError):
        # Hash the bounded file stream, not a raw manifest or member name.
        try:
            with open(path, 'rb') as f:
                row = sha_file(f)
            u = {'unparsed': True, 'sha256': row['sha256'], 'length': row['size']}
        except (ValueError, OSError):
            u = unparsed(None)
        return {'config_sha256': u, 'diff_ids': u,
                'last_layer': {p: {'state': 'unknown', **u} for p in PATHS}}


def _archive(path):
    """Docker-save manifest (including containerd blob paths), final layer + all diff_ids.

    Never extract. The nested layer's compression is auto-detected by tarfile.
    Metadata records also bind compressed/archive representations independently.
    """
    with tarfile.open(path, 'r:*') as t:
        def small(n):
            need(sum(x.name == n for x in t.getmembers()) == 1)
            m = t.getmember(n); need(m.isfile() and m.size <= 1048576)
            return t.extractfile(m).read()
        manifest = json.loads(small('manifest.json')); need(len(manifest) == 1)
        row = manifest[0]; cfgraw = small(row['Config']); cfg = json.loads(cfgraw)
        diffs = cfg['rootfs']['diff_ids']; need(0 < len(diffs) <= 128)
        need(all(re.fullmatch(r'sha256:[0-9a-f]{64}', d) for d in diffs))
        need(len(row['Layers']) == len(diffs))
        need(sum(x.name == row['Layers'][-1] for x in t.getmembers()) == 1)
        layer = t.getmember(row['Layers'][-1]); need(layer.isfile() and layer.size <= CAP)
        files = files_from_tar(t.extractfile(layer))
        return {'config_sha256': hashlib.sha256(cfgraw).hexdigest(), 'diff_ids': diffs,
                'last_layer': files}


def parse_shell(raw):
    """Ignore ls/mount text. Accept only exact fixed-target stat/hash records."""
    text = raw.decode('ascii', 'replace'); out = {}
    for p in PATHS:
        pat = r'(?m)^PROBE ' + re.escape(p) + r' (ABSENT|[0-9]{1,10} [0-9a-f]{64})$'
        rows = re.findall(pat, text)
        candidates = re.findall(r'(?m)^PROBE ' + re.escape(p) + r'(?: |$).*$', text)
        if len(rows) != 1 or len(candidates) != 1:
            out[p] = unknown_file(raw); continue
        if rows[0] == 'ABSENT':
            out[p] = absent()
        else:
            n, h = rows[0].split(); out[p] = {'state': 'file', 'size': int(n), 'sha256': h}
        try: file_row(out[p])
        except ValueError: out[p] = unknown_file(raw)
    return out


def cp_missing(raw, path, cid):
    need(path in PATHS and re.fullmatch('[0-9a-f]{64}', cid) is not None)
    expected = 'Error response from daemon: Could not find the file /' + path + ' in container ' + cid
    return raw.decode('ascii', 'replace').strip() == expected


def mount_facts(raw):
    roots = []
    for line in raw.decode('ascii', 'replace').splitlines():
        if ' - ' not in line:
            continue
        a, b = line.split(' - ', 1); left = a.split(); right = b.split()
        if len(left) >= 6 and len(right) >= 3 and left[4] == '/':
            opts = set(left[5].split(',') + right[2].split(','))
            roots.append({'fstype': enum(right[0], ('overlay', 'fuse.overlayfs', 'ext4', 'xfs')),
                          'read_only': 'ro' in opts, 'read_write': 'rw' in opts,
                          'lowerdir_present': any(x.startswith('lowerdir=') for x in opts),
                          'upperdir_present': any(x.startswith('upperdir=') for x in opts),
                          'workdir_present': any(x.startswith('workdir=') for x in opts),
                          'index_on': 'index=on' in opts, 'metacopy_on': 'metacopy=on' in opts})
    return roots[0] if len(roots) == 1 else unparsed(raw)


def enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else unparsed(value)


def token(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._+~:-]{1,96}', value) else unparsed(value)


def mapping(v):
    return v if isinstance(v, dict) else {}


def engine_facts(version, info, kernel, filesystem, free):
    version, info = mapping(version), mapping(info)
    raw_status = info.get('DriverStatus')
    statuses = {}
    malformed = not isinstance(raw_status, list)
    if isinstance(raw_status, list):
        for row in raw_status:
            if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or row[0] in statuses:
                malformed = True; continue
            statuses[row[0]] = row[1]
    def status(key, allowed):
        return unparsed(raw_status) if malformed else enum(statuses.get(key), allowed)
    dtype = statuses.get('driver-type') if not malformed else None
    components = mapping(version.get('Server')).get('Components')
    containerd = [x.get('Version') for x in components if isinstance(x, dict) and x.get('Name') == 'containerd'] if isinstance(components, list) else []
    driver = enum(info.get('Driver'), ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs'))
    return {'server_version': token(mapping(version.get('Server')).get('Version')),
            'client_version': token(mapping(version.get('Client')).get('Version')),
            'containerd_version': token(containerd[0]) if len(containerd) == 1 else unparsed(containerd),
            'storage_driver': driver,
            'driver_status': {'driver_type': status('driver-type', ('io.containerd.snapshotter.v1',)),
                              'backing_filesystem': status('Backing Filesystem', ('extfs', 'ext4', 'xfs', 'btrfs', 'zfs', 'tmpfs')),
                              'supports_dtype': status('Supports d_type', ('true', 'false')),
                              'native_overlay_diff': status('Native Overlay Diff', ('true', 'false')),
                              'using_metacopy': status('Using metacopy', ('true', 'false'))},
            'containerd_image_store': dtype == 'io.containerd.snapshotter.v1' if isinstance(dtype, str) else unparsed(dtype),
            'snapshotter': driver, 'kernel': token(kernel),
            'docker_filesystem': enum(filesystem, ('ext4', 'xfs', 'btrfs', 'zfs', 'overlay', 'tmpfs')),
            'guest_free_bytes': free if type(free) is int and free >= 0 else unparsed(free)}


def image_facts(doc):
    doc = mapping(doc); root = mapping(doc.get('RootFS')); layers = root.get('Layers')
    valid = isinstance(layers, list) and 0 < len(layers) <= 128 and all(
        isinstance(x, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', x) for x in layers)
    projected = layers if isinstance(layers, list) else []
    return {'id': doc.get('Id'), 'diff_ids': layers if valid else unparsed(layers),
            'layer_count': len(projected) if valid else unparsed(layers),
            'last_diff_id': projected[-1] if valid else unparsed(layers),
            'graph_driver': enum(mapping(doc.get('GraphDriver')).get('Name'), ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs')),
            'rootfs_type': enum(root.get('Type'), ('layers',))}


def unknown_row(v, file=False):
    need(set(v) == ({'state', 'unparsed', 'sha256', 'length'} if file else {'unparsed', 'sha256', 'length'}))
    need(v['unparsed'] is True and type(v['length']) is int and 0 <= v['length'] <= CAP)
    need(isinstance(v['sha256'], str) and re.fullmatch('[0-9a-f]{64}', v['sha256']) is not None)
    if file: need(v['state'] == 'unknown')


def file_row(row):
    if is_unknown(row):
        unknown_row(row, file=True); return
    need(set(row) == {'state', 'size', 'sha256'})
    if row['state'] == 'absent':
        need(row['size'] is None and row['sha256'] is None)
    else:
        need(row['state'] == 'file' and type(row['size']) is int and 0 <= row['size'] <= CAP)
        need(isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256']) is not None)
        need((row['size'] == 0) == (row['sha256'] == EMPTY_SHA256))


def bundle(row):
    return row == {'state': 'file', 'size': BUNDLE_SIZE, 'sha256': BUNDLE_SHA256}


def empty(row):
    return row == {'state': 'file', 'size': 0, 'sha256': EMPTY_SHA256}


def contains_unknown(v):
    if is_unknown(v): return True
    if isinstance(v, dict): return any(contains_unknown(x) for x in v.values())
    if isinstance(v, list): return any(contains_unknown(x) for x in v)
    return False


def classify(doc):
    # Missing measurements cannot earn a causal verdict. Auxiliary facts do not
    # change the r2 experiment; the complete-data decision function is unchanged.
    if contains_unknown(doc['original']): return 'INCONSISTENT'
    for cy in doc['cycles']:
        if contains_unknown([cy['image']['diff_ids'], cy['saved'], cy['methods']]): return 'INCONSISTENT'
    return _classify_r2(doc)


def _classify_r2(doc):
    """Compare merged views before/after eviction; round-trip localizes store damage.

    A correct save with bad merged views is still damage (snapshotter rather than
    archive content). Mixed merged views, metadata drift, or contradictory saved
    bytes do not earn a causal verdict. Complete failed commands never get here.
    """
    original = doc['original']
    if not bundle(original['last_layer']['app/main.mjs']) or original['diff_ids'][-1] != LAST_DIFF_ID:
        return 'INCONSISTENT'
    if len(doc['cycles']) != 2 or [cy['name'] for cy in doc['cycles']] != list(CYCLES):
        return 'INCONSISTENT'
    good = []
    for cy in doc['cycles']:
        image, saved = cy['image'], cy['saved']
        if (image['id'] != BOUND_ID or image['diff_ids'] != original['diff_ids'] or
                saved['diff_ids'] != original['diff_ids']):
            return 'INCONSISTENT'
        rows = [cy['methods'][m]['app/main.mjs'] for m in METHODS]
        if any(r != rows[0] for r in rows):
            return 'INCONSISTENT'
        if any(cy['methods'][m]['app/main.js']['state'] != 'absent' for m in METHODS):
            return 'INCONSISTENT'
        last = saved['last_layer']['app/main.mjs']
        if last != rows[0] and not bundle(last):
            return 'INCONSISTENT'
        good.append(bundle(rows[0]))
    return {(False, True): 'BASELINE_IMAGE_DAMAGED_FRESH_LOAD_OK',
            (False, False): 'FRESH_LOAD_ALSO_BAD',
            (True, True): 'BASELINE_OK_FRESH_OK',
            (True, False): 'BASELINE_OK_FRESH_BAD'}[tuple(good)]


def validate_export(doc):
    """Fixed schema, allowing only bounded hash-only unknowns, never arbitrary text."""
    need(set(doc) == {'schema', 'original', 'engine', 'cycles', 'verdict', 'review_required', 'full_acceptance', 'reload_without_prune', 'baseline_id', 'fresh_loaded_id', 'fresh_id_equals_baseline', 'baseline_removed_before_load'})
    need(doc['schema'] == 'privsep-manager-probe-r3' and doc['review_required'] is True and doc['full_acceptance'] is False)
    need(doc['reload_without_prune'] is True)
    need(doc['baseline_id'] == doc['fresh_loaded_id'] == BOUND_ID)
    need(doc['fresh_id_equals_baseline'] is True and doc['baseline_removed_before_load'] is True)
    def digest(v, prefix=''):
        if is_unknown(v): unknown_row(v); return
        need(isinstance(v, str) and re.fullmatch(prefix + '[0-9a-f]{64}', v) is not None)
    def diffs(v):
        if is_unknown(v): unknown_row(v); return
        need(isinstance(v, list) and 0 < len(v) <= 128)
        for x in v: digest(x, 'sha256:')
    def fact(v, allowed=None, kind=None):
        if is_unknown(v): unknown_row(v); return
        if allowed is not None: need(isinstance(v, str) and v in allowed)
        elif kind is not None: need(type(v) is kind and (kind is not int or v >= 0))
        else: need(isinstance(v, str) and not is_unknown(token(v)))
    def arch(v):
        need(set(v) == {'config_sha256', 'diff_ids', 'last_layer'}); digest(v['config_sha256']); diffs(v['diff_ids'])
        need(set(v['last_layer']) == set(PATHS))
        for x in v['last_layer'].values(): file_row(x)
    arch(doc['original'])
    e = doc['engine']
    need(set(e) == {'server_version', 'client_version', 'containerd_version', 'storage_driver', 'driver_status', 'containerd_image_store', 'snapshotter', 'kernel', 'docker_filesystem', 'guest_free_bytes'})
    for k in ('server_version', 'client_version', 'containerd_version', 'kernel'): fact(e[k])
    drivers = ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs', 'other')
    fact(e['storage_driver'], drivers); fact(e['snapshotter'], drivers)
    fact(e['containerd_image_store'], kind=bool); fact(e['guest_free_bytes'], kind=int)
    fact(e['docker_filesystem'], ('ext4', 'xfs', 'btrfs', 'zfs', 'overlay', 'tmpfs', 'other'))
    ds = e['driver_status']; need(set(ds) == {'driver_type', 'backing_filesystem', 'supports_dtype', 'native_overlay_diff', 'using_metacopy'})
    fact(ds['driver_type'], ('io.containerd.snapshotter.v1', 'other'))
    fact(ds['backing_filesystem'], ('extfs', 'ext4', 'xfs', 'btrfs', 'zfs', 'tmpfs', 'other'))
    for k in ('supports_dtype', 'native_overlay_diff', 'using_metacopy'): fact(ds[k], ('true', 'false', 'other'))
    if not is_unknown(e['containerd_image_store']) and not is_unknown(ds['driver_type']):
        need(e['containerd_image_store'] == (ds['driver_type'] == 'io.containerd.snapshotter.v1'))
    need(isinstance(doc['cycles'], list) and [x['name'] for x in doc['cycles']] == list(CYCLES))
    for cy in doc['cycles']:
        need(set(cy) == {'name', 'image', 'methods', 'saved', 'mount', 'cleanup_absent'})
        need(cy['cleanup_absent'] is True); arch(cy['saved'])
        im = cy['image']; need(set(im) == {'id', 'diff_ids', 'layer_count', 'last_diff_id', 'graph_driver', 'rootfs_type'})
        need(im['id'] == BOUND_ID); diffs(im['diff_ids'])
        fact(im['layer_count'], kind=int); digest(im['last_diff_id'], 'sha256:')
        if not contains_unknown([im['diff_ids'], im['layer_count'], im['last_diff_id']]):
            need(im['layer_count'] == len(im['diff_ids']) and im['last_diff_id'] == im['diff_ids'][-1])
        fact(im['graph_driver'], drivers); fact(im['rootfs_type'], ('layers', 'other'))
        need(set(cy['methods']) == set(METHODS))
        for rows in cy['methods'].values():
            need(set(rows) == set(PATHS))
            for x in rows.values(): file_row(x)
        mt = cy['mount']
        if is_unknown(mt): unknown_row(mt)
        else:
            need(set(mt) == {'fstype', 'read_only', 'read_write', 'lowerdir_present', 'upperdir_present', 'workdir_present', 'index_on', 'metacopy_on'})
            fact(mt['fstype'], ('overlay', 'fuse.overlayfs', 'ext4', 'xfs', 'other'))
            for k in set(mt) - {'fstype'}: need(type(mt[k]) is bool)
    need(doc['verdict'] in VERDICTS and doc['verdict'] == classify(doc))
    return doc
