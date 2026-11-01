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
CYCLES = ('first_load', 'second_load')
VERDICTS = ('IMPORT_LOSES_CONTENT', 'MOUNT_LOSES_CONTENT', 'INCONSISTENT', 'NOT_REPRODUCED')
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


def files_from_tar(fileobj):
    """Streaming only. Reject duplicates/nonregular targets rather than guessing order."""
    out = {p: absent() for p in PATHS}; seen = set()
    with tarfile.open(fileobj=fileobj, mode='r|*') as t:
        for m in t:
            name = m.name.removeprefix('./').removeprefix('/')
            if name not in PATHS:
                continue
            need(name not in seen and m.isfile() and m.size <= CAP); seen.add(name)
            out[name] = sha_file(t.extractfile(m)); need(out[name]['size'] == m.size)
    return out


def archive(path):
    """Docker-save manifest (including containerd blob paths), final layer + all diff_ids.

    Never extract. The nested layer's compression is auto-detected by tarfile.
    Metadata records also bind compressed/archive representations independently.
    """
    with tarfile.open(path, 'r:*') as t:
        def small(n):
            m = t.getmember(n); need(m.isfile() and m.size <= 1048576)
            return t.extractfile(m).read()
        manifest = json.loads(small('manifest.json')); need(len(manifest) == 1)
        row = manifest[0]; cfgraw = small(row['Config']); cfg = json.loads(cfgraw)
        diffs = cfg['rootfs']['diff_ids']; need(0 < len(diffs) <= 128)
        need(all(re.fullmatch(r'sha256:[0-9a-f]{64}', d) for d in diffs))
        need(len(row['Layers']) == len(diffs))
        layer = t.getmember(row['Layers'][-1]); need(layer.isfile() and layer.size <= CAP)
        files = files_from_tar(t.extractfile(layer))
        return {'config_sha256': hashlib.sha256(cfgraw).hexdigest(), 'diff_ids': diffs,
                'last_layer': files}


def parse_shell(raw):
    """Ignore ls/mount text. Accept only exact fixed-target stat/hash records."""
    text = raw.decode('ascii', 'replace'); out = {}
    for p in PATHS:
        pat = r'(?m)^PROBE ' + re.escape(p) + r' (ABSENT|[0-9]{1,10} [0-9a-f]{64})$'
        rows = re.findall(pat, text); need(len(rows) == 1)
        if rows[0] == 'ABSENT':
            out[p] = absent()
        else:
            n, h = rows[0].split(); out[p] = {'state': 'file', 'size': int(n), 'sha256': h}
        file_row(out[p])
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
    need(len(roots) == 1); return roots[0]


def enum(value, allowed):
    return value if value in allowed else 'other'


def token(value):
    need(isinstance(value, str) and re.fullmatch(r'[0-9][0-9A-Za-z.+_~-]{0,95}', value) is not None)
    return value


def engine_facts(version, info, kernel, filesystem, free):
    statuses = dict(info.get('DriverStatus') or [])
    dtype = statuses.get('driver-type', '')
    return {'server_version': token(version['Server']['Version']), 'client_version': token(version['Client']['Version']),
            'storage_driver': enum(info.get('Driver'), ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs')),
            'driver_status': {'driver_type': enum(dtype, ('io.containerd.snapshotter.v1',)),
                              'backing_filesystem': enum(statuses.get('Backing Filesystem'), ('extfs', 'ext4', 'xfs', 'btrfs', 'zfs', 'tmpfs')),
                              'supports_dtype': enum(statuses.get('Supports d_type'), ('true', 'false')),
                              'native_overlay_diff': enum(statuses.get('Native Overlay Diff'), ('true', 'false')),
                              'using_metacopy': enum(statuses.get('Using metacopy'), ('true', 'false'))},
            'containerd_image_store': dtype == 'io.containerd.snapshotter.v1',
            'snapshotter': enum(info.get('Driver'), ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs')),
            'kernel': token(kernel), 'docker_filesystem': enum(filesystem, ('ext4', 'xfs', 'btrfs', 'zfs', 'overlay', 'tmpfs')),
            'guest_free_bytes': free}


def image_facts(doc):
    return {'id': doc['Id'], 'diff_ids': doc['RootFS']['Layers'],
            'layer_count': len(doc['RootFS']['Layers']), 'last_diff_id': doc['RootFS']['Layers'][-1],
            'graph_driver': enum((doc.get('GraphDriver') or {}).get('Name'), ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs')),
            'rootfs_type': enum(doc['RootFS'].get('Type'), ('layers',))}


def file_row(row):
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


def classify(doc):
    """Only unanimous, complete evidence earns a causal class. Drift is inconsistent."""
    original = doc['original']
    if not bundle(original['last_layer']['app/main.mjs']):
        return 'INCONSISTENT'
    per = []
    for cy in doc['cycles']:
        image, saved = cy['image'], cy['saved']
        rows = [cy['methods'][m]['app/main.mjs'] for m in METHODS]
        # main.js must have been whiteouted from the merged root, not resurrected.
        if any(cy['methods'][m]['app/main.js']['state'] != 'absent' for m in METHODS):
            per.append('INCONSISTENT'); continue
        last = saved['last_layer']['app/main.mjs']
        if empty(last) and all(empty(r) for r in rows):
            per.append('IMPORT_LOSES_CONTENT')
        elif bundle(last) and all(empty(r) for r in rows):
            per.append('MOUNT_LOSES_CONTENT')
        elif bundle(last) and all(bundle(r) for r in rows):
            per.append('NOT_REPRODUCED')
        else:
            per.append('INCONSISTENT')
    return per[0] if len(per) == 2 and len(set(per)) == 1 else 'INCONSISTENT'


def validate_export(doc):
    """Strict fixed schema; no arbitrary strings, paths, logs, Config.Env or tar members."""
    need(set(doc) == {'schema', 'original', 'engine', 'cycles', 'verdict', 'review_required', 'full_acceptance', 'reload_without_prune'})
    need(doc['schema'] == 'privsep-manager-probe-v1' and doc['review_required'] is True and doc['full_acceptance'] is False)
    need(doc['reload_without_prune'] is True)
    def digest(v, prefix=''):
        need(isinstance(v, str) and re.fullmatch(prefix + '[0-9a-f]{64}', v) is not None)
    def diffs(v):
        need(isinstance(v, list) and 0 < len(v) <= 128)
        for x in v: digest(x, 'sha256:')
    def arch(v):
        need(set(v) == {'config_sha256', 'diff_ids', 'last_layer'}); digest(v['config_sha256']); diffs(v['diff_ids'])
        need(set(v['last_layer']) == set(PATHS))
        for x in v['last_layer'].values(): file_row(x)
    arch(doc['original'])
    e = doc['engine']
    need(set(e) == {'server_version', 'client_version', 'storage_driver', 'driver_status', 'containerd_image_store', 'snapshotter', 'kernel', 'docker_filesystem', 'guest_free_bytes'})
    for k in ('server_version', 'client_version', 'kernel'): token(e[k])
    drivers = ('overlayfs', 'overlay2', 'overlay', 'vfs', 'btrfs', 'zfs', 'other')
    need(e['storage_driver'] in drivers and e['snapshotter'] in drivers)
    need(type(e['containerd_image_store']) is bool and type(e['guest_free_bytes']) is int and e['guest_free_bytes'] >= 0)
    need(e['docker_filesystem'] in ('ext4', 'xfs', 'btrfs', 'zfs', 'overlay', 'tmpfs', 'other'))
    ds = e['driver_status']; need(set(ds) == {'driver_type', 'backing_filesystem', 'supports_dtype', 'native_overlay_diff', 'using_metacopy'})
    need(ds['driver_type'] in ('io.containerd.snapshotter.v1', 'other'))
    need(ds['backing_filesystem'] in ('extfs', 'ext4', 'xfs', 'btrfs', 'zfs', 'tmpfs', 'other'))
    for k in ('supports_dtype', 'native_overlay_diff', 'using_metacopy'): need(ds[k] in ('true', 'false', 'other'))
    need(e['containerd_image_store'] == (ds['driver_type'] == 'io.containerd.snapshotter.v1'))
    need(isinstance(doc['cycles'], list) and [x['name'] for x in doc['cycles']] == list(CYCLES))
    for cy in doc['cycles']:
        need(set(cy) == {'name', 'image', 'methods', 'saved', 'mount', 'cleanup_absent'})
        need(cy['cleanup_absent'] is True); arch(cy['saved'])
        im = cy['image']; need(set(im) == {'id', 'diff_ids', 'layer_count', 'last_diff_id', 'graph_driver', 'rootfs_type'})
        digest(im['id'], 'sha256:'); diffs(im['diff_ids'])
        need(type(im['layer_count']) is int and im['layer_count'] == len(im['diff_ids']) and im['last_diff_id'] == im['diff_ids'][-1])
        need(im['graph_driver'] in drivers and im['rootfs_type'] in ('layers', 'other'))
        need(set(cy['methods']) == set(METHODS))
        for rows in cy['methods'].values():
            need(set(rows) == set(PATHS))
            for x in rows.values(): file_row(x)
        mt = cy['mount']; need(set(mt) == {'fstype', 'read_only', 'read_write', 'lowerdir_present', 'upperdir_present', 'workdir_present', 'index_on', 'metacopy_on'})
        need(mt['fstype'] in ('overlay', 'fuse.overlayfs', 'ext4', 'xfs', 'other'))
        for k in set(mt) - {'fstype'}: need(type(mt[k]) is bool)
    need(doc['verdict'] in VERDICTS and doc['verdict'] == classify(doc))
    return doc
