#!/usr/bin/env python3
"""Synthetic unit fixtures only; no Docker, VM, daemon, network or application.

All payloads below are synthetic, not substituted native evidence. Also run by
staged runner before its one guest boot; a failure aborts that boot.
"""
import ast
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
Q = HERE if (HERE / 'collector.py').exists() else HERE.parent / 'qa/privsep-storage'
CP = Q / ('collector.py' if Q == HERE else 'manager_probe_collector.py')
GP = Q / ('guest.py' if Q == HERE else 'manager_probe_guest.py')
RP = Q / ('runner.py' if Q == HERE else 'manager_probe_run.py')


def load(name, path):
    s = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


C = load('probe_fixture_collector', CP)
G = load('probe_fixture_guest', GP)
R = load('probe_fixture_runner', RP)
GOOD = {'state': 'file', 'size': C.BUNDLE_SIZE, 'sha256': C.BUNDLE_SHA256}
ZERO = {'state': 'file', 'size': 0, 'sha256': C.EMPTY_SHA256}
DIFFS = ['sha256:' + 'a' * 64, C.LAST_DIFF_ID]
MOUNT = {'fstype': 'overlay', 'read_only': False, 'read_write': True, 'lowerdir_present': True,
         'upperdir_present': True, 'workdir_present': True, 'index_on': False, 'metacopy_on': False}


def files(main=GOOD):
    return {'app/main.mjs': copy.deepcopy(main), 'app/main.js': C.absent()}


def arch(main=GOOD):
    return {'config_sha256': 'c' * 64, 'diff_ids': DIFFS.copy(), 'last_layer': files(main)}


def sample(saved=GOOD, materialized=GOOD):
    engine = C.engine_facts({'Server': {'Version': '29.0.0', 'Components': [{'Name': 'containerd', 'Version': '2.1.4'}]}, 'Client': {'Version': '29.0.0', 'Components': [{'Name': 'containerd', 'Version': '2.1.4'}]}},
                            {'Driver': 'overlayfs', 'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]},
                            '6.1.0-1-amd64', 'ext4', 10000000000)
    cycles = []
    for name in C.CYCLES:
        cycles.append({'name': name, 'image': {'id': C.BOUND_ID, 'diff_ids': DIFFS.copy(),
                       'layer_count': len(DIFFS), 'last_diff_id': DIFFS[-1], 'graph_driver': 'other', 'rootfs_type': 'layers'},
                       'methods': {m: files(materialized) for m in C.METHODS}, 'saved': arch(saved),
                       'mount': copy.deepcopy(MOUNT), 'cleanup_absent': True})
    doc = {'schema': 'privsep-manager-probe-r3', 'original': arch(), 'engine': engine, 'cycles': cycles,
           'review_required': True, 'full_acceptance': False, 'reload_without_prune': True,
           'baseline_id': C.BOUND_ID, 'fresh_loaded_id': C.BOUND_ID,
           'fresh_id_equals_baseline': True, 'baseline_removed_before_load': True}
    doc['verdict'] = C.classify(doc); return doc


def tar_bytes(rows):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as t:
        for name, data in rows:
            m = tarfile.TarInfo(name); m.size = len(data); t.addfile(m, io.BytesIO(data))
    return out.getvalue()


class ArchiveFixtures(unittest.TestCase):
    def test_last_layer_and_before_diff_ids(self):
        payload = b'SYNTHETIC_BUNDLE_NOT_NATIVE_EVIDENCE'
        layer = tar_bytes([('app/.wh.main.js', b''), ('app/main.mjs', payload)])
        cfg = json.dumps({'rootfs': {'type': 'layers', 'diff_ids': DIFFS}}).encode()
        for compressed in (False, True):
            nested = gzip.compress(layer) if compressed else layer
            manifest = json.dumps([{'Config': 'config.json', 'Layers': ['old.tar', 'blobs/sha256/last']}]).encode()
            raw = tar_bytes([('manifest.json', manifest), ('config.json', cfg), ('old.tar', tar_bytes([])),
                             ('blobs/sha256/last', nested)])
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'save.tar.gz'; path.write_bytes(gzip.compress(raw))
                got = C.archive(path)
            self.assertEqual(got['diff_ids'], DIFFS)
            self.assertEqual(got['last_layer'], files({'state': 'file', 'size': len(payload),
                                                      'sha256': hashlib.sha256(payload).hexdigest()}))

    def test_flat_export_zero_and_absence(self):
        got = C.files_from_tar(io.BytesIO(tar_bytes([('./app/main.mjs', b''), ('etc/SECRET_SENTINEL', b'private')])) )
        self.assertEqual(got, files(ZERO)); self.assertNotIn('SECRET_SENTINEL', json.dumps(got))

    def test_duplicate_fixed_member_unknown(self):
        got = C.files_from_tar(io.BytesIO(tar_bytes([('app/main.mjs', b'a'), ('app/main.mjs', b'b')])))
        self.assertEqual(got['app/main.mjs']['state'], 'unknown')

    def test_symlink_fixed_member_refused(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as t:
            m = tarfile.TarInfo('app/main.mjs'); m.type = tarfile.SYMTYPE; m.linkname = '/private'; t.addfile(m)
        self.assertEqual(C.files_from_tar(io.BytesIO(out.getvalue()))['app/main.mjs']['state'], 'unknown')

    def test_empty_or_bad_manifest_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad.tar'; p.write_bytes(tar_bytes([('manifest.json', b'[]')]))
            self.assertTrue(C.is_unknown(C.archive(p)['diff_ids']))


class Classification(unittest.TestCase):
    def test_five_classes(self):
        for before, after, want in ((ZERO, GOOD, 'BASELINE_IMAGE_DAMAGED_FRESH_LOAD_OK'),
                                    (ZERO, ZERO, 'FRESH_LOAD_ALSO_BAD'),
                                    (GOOD, GOOD, 'BASELINE_OK_FRESH_OK'),
                                    (GOOD, ZERO, 'BASELINE_OK_FRESH_BAD')):
            for saved_bad in (False, True):
                d = sample()
                for cy, row in zip(d['cycles'], (before, after)):
                    cy['methods'] = {m: files(row) for m in C.METHODS}
                    if saved_bad: cy['saved'] = arch(row)
                d['verdict'] = C.classify(d)
                self.assertEqual(d['verdict'], want); C.validate_export(d)
        d = sample(ZERO, GOOD)
        self.assertEqual(d['verdict'], 'INCONSISTENT'); C.validate_export(d)

    def test_arbitrary_bad_or_absent_is_not_mistaken_for_ok(self):
        for bad in (C.absent(), {'state': 'file', 'size': 7, 'sha256': 'f' * 64}):
            d = sample(GOOD, bad)
            self.assertEqual(d['verdict'], 'FRESH_LOAD_ALSO_BAD'); C.validate_export(d)

    def test_view_disagreement(self):
        d = sample(GOOD, ZERO); d['cycles'][0]['methods']['cp'] = files(GOOD)
        self.assertEqual(C.classify(d), 'INCONSISTENT')

    def test_before_after_difference_is_the_experiment(self):
        d = sample(GOOD, ZERO); d['cycles'][1] = sample()['cycles'][1]
        self.assertEqual(C.classify(d), 'BASELINE_IMAGE_DAMAGED_FRESH_LOAD_OK')

    def test_diff_id_change_is_preserved_and_inconsistent(self):
        d = sample(ZERO, ZERO); d['cycles'][1]['saved']['diff_ids'] = ['sha256:' + 'f' * 64]
        d['verdict'] = C.classify(d); self.assertEqual(d['verdict'], 'INCONSISTENT')
        C.validate_export(d); self.assertNotEqual(d['cycles'][1]['saved']['diff_ids'], d['original']['diff_ids'])

    def test_resurrected_main_js_inconsistent(self):
        d = sample(); d['cycles'][0]['methods']['export']['app/main.js'] = GOOD
        self.assertEqual(C.classify(d), 'INCONSISTENT')

    def test_wrong_original_never_earns_causal_verdict(self):
        d = sample(); d['original']['last_layer'] = files(ZERO)
        self.assertEqual(C.classify(d), 'INCONSISTENT')

    def test_schema_refuses_leaks_and_false_pass(self):
        changes = [lambda d: d.update(secret='SECRET_SENTINEL'), lambda d: d.update(full_acceptance=True),
                   lambda d: d.update(verdict='BASELINE_OK_FRESH_BAD'), lambda d: d.update(cycles=d['cycles'][:1]),
                   lambda d: d['engine'].update(Config={'Env': ['SECRET_SENTINEL']}),
                   lambda d: d['cycles'][0]['image'].update(config='SECRET_SENTINEL'),
                   lambda d: d['cycles'][0]['mount'].update(lowerdir='/SECRET_SENTINEL'),
                   lambda d: d['cycles'][0]['methods']['cp']['app/main.mjs'].update(size=0),
                   lambda d: d['cycles'][0].update(cleanup_absent=False),
                   lambda d: d.update(fresh_id_equals_baseline=False),
                   lambda d: d.update(baseline_removed_before_load=False),
                   lambda d: d.update(fresh_loaded_id='sha256:' + 'f' * 64)]
        for change in changes:
            d = sample(); change(d)
            with self.assertRaises(ValueError): C.validate_export(d)


class Projection(unittest.TestCase):
    def test_shell_hash_and_size_only(self):
        raw = ('ls-private-file-name\nPROBE app/main.mjs 0 ' + C.EMPTY_SHA256 + '\nPROBE app/main.js ABSENT\n').encode()
        self.assertEqual(C.parse_shell(raw), files(ZERO)); self.assertNotIn('private', json.dumps(C.parse_shell(raw)))

    def test_shell_duplicate_incomplete_and_invalid_refused(self):
        line = ('PROBE app/main.mjs 0 ' + C.EMPTY_SHA256 + '\n').encode()
        for raw in (b'', line, line * 2 + b'PROBE app/main.js ABSENT\n', line.replace(b' 0 ', b' 1 ') + b'PROBE app/main.js ABSENT\n'):
            self.assertTrue(any(C.is_unknown(v) for v in C.parse_shell(raw).values()))

    def test_cp_error_is_not_absence(self):
        cid = 'a' * 64
        self.assertTrue(C.cp_missing(('Error response from daemon: Could not find the file /app/main.js in container ' + cid + '\n').encode(), 'app/main.js', cid))
        for b in (b'permission denied', b'Cannot connect to the Docker daemon', b'No such container'):
            self.assertFalse(C.cp_missing(b, 'app/main.js', cid))

    def test_mount_paths_never_exported(self):
        raw = b'10 1 0:1 / / rw,relatime - overlay overlay rw,lowerdir=/SECRET_SENTINEL,upperdir=/private,workdir=/private\n'
        got = C.mount_facts(raw); self.assertEqual(got, MOUNT)
        self.assertNotIn('SECRET_SENTINEL', json.dumps(got))
        self.assertTrue(C.is_unknown(C.mount_facts(raw + raw)))

    def test_engine_projection_discards_private_fields(self):
        version = {'Server': {'Version': '29.0.0', 'Components': [{'Name': 'containerd', 'Version': '2.1.4'}]}, 'Client': {'Version': '29.0.0', 'Components': [{'Name': 'containerd', 'Version': '2.1.4'}]}, 'Name': 'SECRET_SENTINEL'}
        info = {'Driver': 'overlayfs', 'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1'],
                ['SECRET_SENTINEL', 'private']], 'DockerRootDir': '/private', 'ID': 'SECRET_SENTINEL'}
        got = C.engine_facts(version, info, '6.1.0-amd64', 'ext4', 123)
        self.assertTrue(got['containerd_image_store']); self.assertNotIn('SECRET_SENTINEL', json.dumps(got))

    def test_image_projection_discards_env_and_graph_paths(self):
        got = C.image_facts({'Id': 'sha256:' + 'a'*64, 'RootFS': {'Layers': DIFFS, 'Type': 'layers'},
                            'Config': {'Env': ['SECRET_SENTINEL']}, 'GraphDriver': {'Name': 'overlay2', 'Data': {'UpperDir': '/SECRET_SENTINEL'}}})
        self.assertEqual(got['last_diff_id'], DIFFS[-1]); self.assertNotIn('SECRET_SENTINEL', json.dumps(got))


class GuestFlow(unittest.TestCase):
    def test_as_found_then_exact_removal_then_fresh_load_with_fake_commands(self):
        # Real guest orchestration against synthetic commands, NOT native evidence.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); payload = b'SYNTHETIC_ARCHIVE'
            (root / 'backend.tar.gz').write_bytes(payload)
            archive_sha = hashlib.sha256(payload).hexdigest()
            backend = {'archive_sha256': archive_sha, 'config_sha256': arch()['config_sha256'],
                       'rootfs': {'diff_ids': DIFFS}, 'manifest': [{'RepoTags': ['synthetic:backend']}]}
            (root / 'backend.json').write_text(json.dumps(backend))
            image = C.BOUND_ID; cid = 'f' * 64
            seen = []; present = [True]; removed = [False]
            state = {'owned_ids': [], 'owned_images': [], 'baseline_images': [image], 'baseline_id': image,
                     'commands': [], 'checks': [True], 'phases': ['fresh', 'probe']}
            raw = ('10 1 0:1 / / rw - overlay overlay rw,lowerdir=/private,upperdir=/private,workdir=/private\n'
                   'PROBE app/main.mjs 0 ' + C.EMPTY_SHA256 + '\nPROBE app/main.js ABSENT\n').encode()
            def fake_run(label, argv, timeout=120, required=True, stderr=False):
                seen.append((label, [str(x) for x in argv]))
                if label.startswith('load-'):
                    self.assertTrue(removed[0]); self.assertFalse(present[0])
                    progress = json.loads((root / 'probe-progress.json').read_text())
                    self.assertEqual([x['name'] for x in progress['cycles']], ['as_found'])
                    present[0] = True; return 0, ('Loaded image ID: ' + image).encode()
                if label in ('run-shell', 'run-readonly-shell'): return 0, raw
                if label == 'create-probe': return 0, cid.encode()
                if label == 'cp-fixed-file':
                    if 'main.mjs' in argv[-2]: Path(argv[-1]).write_bytes(b''); return 0, b''
                    return 1, ('Error response from daemon: Could not find the file /app/main.js in container ' + cid).encode()
                if label == 'export-container':
                    Path(argv[-2]).write_bytes(tar_bytes([('app/main.mjs', b'')])); return 0, b''
                if label in ('readback-container-absent', 'readback-baseline-absent'): return 1, b''
                if label == 'save-roundtrip': Path(argv[-2]).write_bytes(b'SYNTHETIC_SAVE'); return 0, b''
                if label == 'remove-exact-baseline-image':
                    self.assertEqual(argv[-1], C.BOUND_ID)
                    self.assertEqual([x[0] for x in seen].count('save-roundtrip'), 1)
                    present[0] = False; removed[0] = True
                if label == 'docker-filesystem': return 0, b'ext4\n'
                if label == 'kernel-version': return 0, b'6.1.0-amd64\n'
                return 0, b''
            def fake_json(label, args):
                if label.endswith('-id') or label == 'fresh-loaded-id-only': return image
                if label == 'engine-info':
                    return {'Driver': 'overlayfs', 'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]}
                if label == 'engine-version':
                    return {'Server': {'Version': '29.0.0', 'Components': [{'Name': 'containerd', 'Version': '2.1.4'}]},
                            'Client': {'Version': '29.0.0'}}
                self.assertEqual(label, 'image-inspect')
                return [{'Id': image, 'RootFS': {'Layers': DIFFS, 'Type': 'layers'}}]
            with patch.object(G, 'ROOT', root), patch.object(G, 'report', state), \
                 patch.object(G, 'ARCHIVE_SHA', archive_sha), \
                 patch.object(G, 'run', fake_run), patch.object(G, 'docker_json', fake_json), \
                 patch.object(G.shutil, 'disk_usage', lambda p: type('Usage', (), {'free': 123})()), \
                 patch.object(G, 'image_ids', lambda: [image] if present[0] else []), \
                 patch.object(G, 'save', lambda: None), patch.object(G.C, 'archive', lambda p: arch()):
                G.probe()
            exported = json.loads((root / 'manager-probe-export.json').read_text())
            C.validate_export(exported)
            first, second = exported['cycles']
            self.assertEqual(first['methods'], {m: files(ZERO) for m in C.METHODS})
            self.assertEqual(second['methods'], first['methods'])
            self.assertEqual([x[0] for x in seen].count('remove-exact-baseline-image'), 1)
            self.assertEqual([x[0] for x in seen].count('load-remove_and_fresh_load'), 1)
            self.assertEqual([x[0] for x in seen].count('save-roundtrip'), 2)
            self.assertEqual(state['owned_ids'], [])
            self.assertTrue(exported['fresh_id_equals_baseline'])
            self.assertFalse(any('prune' in args for _, args in seen))

    def test_cannot_load_without_successful_eviction(self):
        with patch.object(G, 'report', {}), patch.object(G, 'run') as run:
            with self.assertRaises(AssertionError): G.load_fresh(arch())
            run.assert_not_called()

    def test_cannot_remove_a_different_baseline(self):
        state = {'baseline_id': 'sha256:' + 'f' * 64, 'baseline_images': [C.BOUND_ID]}
        with patch.object(G, 'report', state), patch.object(G, 'run') as run:
            with self.assertRaises(AssertionError): G.remove_bound_image()
            run.assert_not_called()

    def test_cannot_remove_with_foreign_container(self):
        state = {'baseline_id': C.BOUND_ID, 'baseline_images': [C.BOUND_ID], 'owned_ids': []}
        with patch.object(G, 'report', state), patch.object(G, 'run', return_value=(0, b'a' * 64)) as run:
            with self.assertRaises(AssertionError): G.remove_bound_image()
            self.assertEqual(run.call_count, 1)

    def test_cannot_remove_other_pinned_image(self):
        state = {'baseline_id': C.BOUND_ID, 'baseline_images': [C.BOUND_ID], 'owned_ids': []}
        with patch.object(G, 'report', state), patch.object(G, 'run', return_value=(0, b'')) as run, \
             patch.object(G, 'image_ids', return_value=[C.BOUND_ID]), \
             patch.object(G, 'docker_json', return_value='sha256:' + 'f' * 64):
            with self.assertRaises(AssertionError): G.remove_bound_image()
            self.assertEqual(run.call_count, 1)


class AdmissionAndScope(unittest.TestCase):
    def test_scope_and_shell_syntax(self):
        subprocess.run(['sh', '-n'], input=G.SHELL.encode(), check=True, capture_output=True, timeout=10)
        s = GP.read_text()
        for banned in ("'prune'", "'compose'", "'build'", "'pull'", "'node'"):
            self.assertNotIn(banned, s)
        self.assertIn("'--read-only'", s); self.assertIn("'--no-healthcheck'", s)
        self.assertEqual(R.START_FLOOR, 285781630976)
        self.assertEqual(R.RUN_FLOOR, 277191696384)

    def test_entrypoint_preflight_returns_before_native_lock(self):
        source = RP.read_text(); ast.parse(source)
        self.assertLess(source.index('if args.preflight:'), source.index('base.native_lock()'))
        self.assertIn("r.guest('one-poweroff'", source)
        self.assertEqual(source.count('self.start(1)'), 1)
        self.assertIn('original_disks_and_retained_unchanged', source)
        self.assertIn('all_boot_pids_absent', source)
        self.assertIn('collector.validate_export(export)', source)
        self.assertEqual(R.SUFFIX, '-manager-probe')

    def test_product_filter_excludes_only_qa(self):
        rows = [{'path': 'packages/backend/src/main.ts'}, {'path': 'qa/privsep-storage/new.py'},
                {'path': 'tests/test_manager_probe.py'}]
        self.assertEqual(R.product_rows(rows), rows[:1])

    def test_guest_guard_precedes_phase_actions(self):
        source = GP.read_text()
        body = ast.parse(source)
        main = next(n for n in body.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        self.assertIsInstance(main.body[1], ast.Assert)
        self.assertIn('/run/wizard-storage-production-synthetic', ast.unparse(main.body[1]))
        self.assertIn('?mode=ro', RP.read_text())


# Reconstructed realistic Docker 27/28 shapes, not captured/native evidence.
# Private-looking paths are sentinels used to prove the projection never leaks.
REAL_MOUNT = '''total 6752
-rw-r--r--    1 root     root       6908459 Apr  1  2025 main.mjs
lrwxrwxrwx    1 root     root             8 Apr  1  2025 node_modules -> /modules
1244 1231 0:78 / / rw,relatime shared:409 - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/PRIVATE/l:/var/lib/docker/overlay2/PRIVATE2/l,upperdir=/var/lib/docker/overlay2/PRIVATE/diff,workdir=/var/lib/docker/overlay2/PRIVATE/work,index=off,metacopy=off,userxattr
1245 1244 0:79 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
1246 1244 0:80 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755
1247 1244 0:81 / /sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs ro
1248 1244 8:1 /var/lib/docker/containers/PRIVATE/hosts /etc/hosts rw,relatime - ext4 /dev/sda1 rw,errors=remount-ro
'''


def real_version(docker='28.0.4', containerd='v1.7.27'):
    return {'Client': {'Version': docker, 'ApiVersion': '1.48', 'GitCommit': 'b8034c0',
                       'GoVersion': 'go1.23.7', 'Os': 'linux', 'Arch': 'amd64', 'Context': 'default'},
            'Server': {'Platform': {'Name': 'Docker Engine - Community'},
                       'Version': docker, 'ApiVersion': '1.48', 'MinAPIVersion': '1.24',
                       'Os': 'linux', 'Arch': 'amd64', 'KernelVersion': '6.1.0-37-amd64',
                       'Components': [{'Name': 'Engine', 'Version': docker, 'Details': {'ApiVersion': '1.48'}},
                                      {'Name': 'containerd', 'Version': containerd, 'Details': {'GitCommit': 'PRIVATE'}},
                                      {'Name': 'runc', 'Version': '1.2.5'}, {'Name': 'docker-init', 'Version': '0.19.0'}]}}


def real_info(driver='overlayfs'):
    rows = ([['driver-type', 'io.containerd.snapshotter.v1']] if driver == 'overlayfs' else
            [['Backing Filesystem', 'extfs'], ['Supports d_type', 'true'], ['Using metacopy', 'false'],
             ['Native Overlay Diff', 'true'], ['userxattr', 'false']])
    return {'ID': 'PRIVATE', 'Name': 'PRIVATE', 'Containers': 0, 'Images': 1, 'Driver': driver,
            'DriverStatus': rows, 'DockerRootDir': '/var/lib/docker/PRIVATE', 'ServerVersion': '28.0.4'}


class RealisticProjection(unittest.TestCase):
    def test_docker_27_28_containerd_and_kernel_variations(self):
        for docker in ('27.5.1', '28.0.4'):
            for containerd in ('v1.7.27', '1.7.27', 'v2.0.5'):
                for driver in ('overlayfs', 'overlay2'):
                    for kernel in ('6.1.0-37-amd64', '6.12.43+deb13-amd64'):
                        got = C.engine_facts(real_version(docker, containerd), real_info(driver), kernel, 'ext4', 123)
                        self.assertEqual(got['containerd_version'], containerd)
                        self.assertEqual(got['server_version'], docker)
                        self.assertEqual(got['client_version'], docker)
                        self.assertEqual(got['kernel'], kernel)
                        if driver == 'overlay2': self.assertEqual(got['driver_status']['backing_filesystem'], 'extfs')
                        d = sample(); d['engine'] = got; C.validate_export(d)
                        self.assertNotIn('PRIVATE', json.dumps(got))

    def test_bad_missing_engine_fields_hash_raw_value(self):
        version = real_version(); version['Server']['Version'] = 'PRIVATE /bad\nversion'
        got = C.engine_facts(version, {}, None, 'PRIVATE/bad', None)
        self.assertEqual(got['server_version'], C.unparsed('PRIVATE /bad\nversion'))
        self.assertEqual(got['server_version']['length'], len('PRIVATE /bad\nversion'.encode()))
        self.assertEqual(got['server_version']['sha256'], hashlib.sha256(b'PRIVATE /bad\nversion').hexdigest())
        self.assertNotIn('PRIVATE', json.dumps(got)); d = sample(); d['engine'] = got; C.validate_export(d)

    def test_driverstatus_missing_malformed_and_duplicate_nonfatal(self):
        for status in (None, {}, 3, 'PRIVATE', [['x']], [[{}, 'value']],
                       [['driver-type', 'a'], ['driver-type', 'b']], [['driver-type', {}]]):
            info = real_info(); info['DriverStatus'] = status
            got = C.engine_facts(real_version(), info, '6.1.0-37-amd64', 'ext4', 12)
            d = sample(); d['engine'] = got; C.validate_export(d)
            self.assertTrue(C.is_unknown(got['driver_status']['driver_type']))

    def test_component_and_version_missing_wrong_shapes_nonfatal(self):
        for version in (None, [], {}, {'Server': []}, {'Server': {'Components': [None, 3]}},
                        {'Server': {'Components': [{'Name': 'containerd'}, {'Name': 'containerd', 'Version': 'v1.7.27'}]}}):
            got = C.engine_facts(version, None, None, None, None)
            d = sample(); d['engine'] = got; C.validate_export(d)
            self.assertTrue(C.is_unknown(got['containerd_version']))

    def test_realistic_mount_ls_and_fixed_records(self):
        raw = (REAL_MOUNT + 'PROBE app/main.mjs 0 ' + C.EMPTY_SHA256 + '\nPROBE app/main.js ABSENT\n').encode()
        self.assertEqual(C.mount_facts(raw), MOUNT)
        self.assertEqual(C.parse_shell(raw), files(ZERO))
        self.assertNotIn('PRIVATE', json.dumps(C.mount_facts(raw)))
        for text in ('', REAL_MOUNT + REAL_MOUNT, '12 1 0:1 / / rw - missing\n'):
            self.assertTrue(C.is_unknown(C.mount_facts(text.encode())))

    def test_unfamiliar_root_fstype_unknown_not_abort(self):
        got = C.mount_facts(REAL_MOUNT.replace('- overlay overlay', '- erofs none').encode())
        self.assertTrue(C.is_unknown(got['fstype']))
        d = sample(); d['cycles'][0]['mount'] = got; C.validate_export(d)

    def test_image_rootfs_unfamiliar_metadata_nonfatal(self):
        for root in (None, {}, [], {'Layers': []}, {'Layers': 'PRIVATE'}, {'Layers': [None]}):
            got = C.image_facts({'Id': C.BOUND_ID, 'RootFS': root, 'GraphDriver': []})
            self.assertTrue(C.is_unknown(got['diff_ids']))
            d = sample(); d['cycles'][0]['image'] = got; d['verdict'] = C.classify(d)
            self.assertEqual(d['verdict'], 'INCONSISTENT'); C.validate_export(d)
            self.assertNotIn('PRIVATE', json.dumps(got))

    def test_malformed_tar_manifest_and_layer_nonfatal(self):
        cfg = json.dumps({'rootfs': {'diff_ids': DIFFS}}).encode()
        manifest = json.dumps([{'Config': 'config.json', 'Layers': ['old', 'last']}]).encode()
        for raw in (b'not tar PRIVATE', tar_bytes([('manifest.json', b'bad json PRIVATE')]),
                    tar_bytes([('manifest.json', manifest), ('config.json', cfg), ('last', b'not tar PRIVATE')])):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / 'save.tar'; p.write_bytes(raw); got = C.archive(p)
            d = sample(); d['cycles'][0]['saved'] = got; d['verdict'] = C.classify(d)
            self.assertEqual(d['verdict'], 'INCONSISTENT'); C.validate_export(d)
            self.assertNotIn('PRIVATE', json.dumps(got))

    def test_unknown_measurement_does_not_earn_damage_verdict(self):
        d = sample()
        for m in C.METHODS: d['cycles'][0]['methods'][m]['app/main.mjs'] = C.unknown_file(b'PRIVATE')
        d['verdict'] = C.classify(d); self.assertEqual(d['verdict'], 'INCONSISTENT'); C.validate_export(d)
        d['cycles'][0]['methods']['cp']['app/main.mjs']['private'] = 'PRIVATE'
        with self.assertRaises(ValueError): C.validate_export(d)


# An actual executable on PATH, not a mock of G.run/json/archive/parse methods.
# It never talks to a daemon. All bytes and engine responses are canned fixtures.
SHIM = r'''import hashlib,json,os,shutil,sys
from pathlib import Path
r=Path(os.environ['PROBE_SHIM_ROOT']); a=sys.argv[1:]; exe=Path(sys.argv[0]).name
cfg=json.loads((r/'shim-config.json').read_text()); s=json.loads((r/'shim-state.json').read_text())
with (r/'shim-log.jsonl').open('a') as f: f.write(json.dumps([exe,*a])+'\n')
def done(value='',rc=0,err=False):
    (r/'shim-state.json').write_text(json.dumps(s))
    text=json.dumps(value) if not isinstance(value,str) else value
    if text: print(text,file=sys.stderr if err else sys.stdout)
    sys.exit(rc)
if exe=='findmnt': done('ext4')
if exe=='uname': done('6.12.43+deb13-amd64')
if a[0] in ('info','version'):
    progress=json.loads((r/'probe-progress.json').read_text()); native=json.loads((r/'native.json').read_text())
    assert [c['name'] for c in progress['cycles']]==['as_found','remove_and_fresh_load']
    assert all(set(c['methods'])=={'run','readonly','cp','export'} and c['saved'] is not None for c in progress['cycles'])
    assert native['measurements_complete_before_facts'] is True
    (r/'facts-checkpoint-verified').write_text('true')
    if cfg['mode']=='bad-facts': done('PRIVATE invalid JSON',7)
    done(cfg[a[0]])
if a[:2]==['image','ls']: done(cfg['id'] if s['image'] else '')
if a[:2]==['image','inspect']:
    if not s['image'] or (a[-1]==cfg['tag'] and not s['tag']): done('No such image',1,True)
    if '--format' in a: done(json.dumps(cfg['id']))
    done([cfg['inspect']])
if a[0]=='inspect': done('No such object',1,True)
if a[0]=='tag': s['tag']=True; done()
if a[0]=='ps': done(cfg['cid'] if s['container'] else '')
if a[0]=='create': s['container']=True; done(cfg['cid'])
if a[0]=='rm': assert a[-1]==cfg['cid']; s['container']=False; done()
if a[:2]==['image','rm']:
    assert a[-1] in (cfg['tag'],cfg['id']) and '--no-prune' in a
    if a[-1]==cfg['tag']: s['tag']=False
    else: s['image']=False
    done()
if a[0]=='load':
    assert not s['image']; p=json.loads((r/'probe-progress.json').read_text())
    assert [c['name'] for c in p['cycles']]==['as_found']
    s['image']=True; s['fresh']=True; done('Loading layer 1/17\nLoaded image: fixture/backend:latest')
if a[0]=='run':
    done(cfg['shell'] if cfg['mode']!='bad-parsers' else 'PRIVATE bad shell records')
if a[0]=='cp':
    if a[-2].endswith('/app/main.js'): done('Error response from daemon: Could not find the file /app/main.js in container '+cfg['cid'],1,True)
    Path(a[-1]).write_bytes(b''); done()
if a[0]=='export':
    shutil.copyfile(r/('export.tar' if cfg['mode']!='bad-parsers' else 'bad.tar'),a[a.index('-o')+1]); done()
if a[0]=='save':
    shutil.copyfile(r/('backend.tar.gz' if cfg['mode']!='bad-parsers' else 'bad.tar'),a[a.index('-o')+1]); done()
raise AssertionError('unsupported fixture command '+repr(a))
'''


class ExecutableShim(unittest.TestCase):
    def exercise(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bindir = root / 'bin'; bindir.mkdir()
            layer = tar_bytes([('app/main.mjs', b'')])
            cfgraw = json.dumps({'rootfs': {'type': 'layers', 'diff_ids': DIFFS}}).encode()
            manifest = json.dumps([{'Config': 'blobs/sha256/config', 'Layers': ['old.tar', 'blobs/sha256/last']}]).encode()
            archive = gzip.compress(tar_bytes([('manifest.json', manifest), ('blobs/sha256/config', cfgraw),
                                               ('old.tar', tar_bytes([])), ('blobs/sha256/last', gzip.compress(layer))]))
            if mode == 'bad-archive': archive = b'PRIVATE unsupported original archive'
            sha = hashlib.sha256(archive).hexdigest()
            (root / 'backend.tar.gz').write_bytes(archive); (root / 'export.tar').write_bytes(layer)
            (root / 'bad.tar').write_bytes(b'PRIVATE unsupported archive')
            backend = {'archive_sha256': sha, 'config_sha256': hashlib.sha256(cfgraw).hexdigest(),
                       'rootfs': {'diff_ids': DIFFS}, 'manifest': [{'RepoTags': ['fixture/backend:latest']}]}
            (root / 'backend.json').write_text(json.dumps(backend))
            config = {'id': C.BOUND_ID, 'cid': 'e' * 64, 'tag': G.PINNED, 'mode': mode,
                      'version': real_version(), 'info': real_info(),
                      'inspect': {'Id': C.BOUND_ID, 'RootFS': {'Type': 'layers', 'Layers': DIFFS},
                                  'GraphDriver': {'Name': 'overlayfs', 'Data': {'PRIVATE': '/PRIVATE'}},
                                  'Config': {'Env': ['PRIVATE=not-exported']}},
                      'shell': REAL_MOUNT + 'PROBE app/main.mjs 0 ' + C.EMPTY_SHA256 + '\nPROBE app/main.js ABSENT\n'}
            if mode == 'bad-parsers': config['inspect']['RootFS'] = {'Layers': []}
            (root / 'shim-config.json').write_text(json.dumps(config))
            (root / 'shim-state.json').write_text(json.dumps({'image': True, 'container': False, 'tag': True, 'fresh': False}))
            for n in ('docker', 'uname', 'findmnt'):
                p = bindir / n; p.write_text('#!' + sys.executable + ' -B\n' + SHIM); p.chmod(0o700)
            state = {'commands': [], 'checks': [True], 'phases': ['fresh', 'probe'],
                     'owned_ids': [], 'owned_images': [], 'baseline_images': [C.BOUND_ID], 'baseline_id': C.BOUND_ID}
            # Only fixture location/archive binding change. Real subprocesses,
            # command parser, tar reader, measurements and checkpoint I/O run.
            with patch.dict(os.environ, {'PATH': str(bindir) + os.pathsep + os.environ.get('PATH', ''), 'PROBE_SHIM_ROOT': str(root)}), \
                 patch.object(G, 'ROOT', root), patch.object(G, 'ARCHIVE_SHA', sha), patch.object(G, 'report', state):
                G.probe()
            exported = json.loads((root / 'manager-probe-export.json').read_text()); C.validate_export(exported)
            native = json.loads((root / 'native.json').read_text())
            self.assertTrue(native['measurements_complete_before_facts'])
            self.assertEqual((root / 'facts-checkpoint-verified').read_text(), 'true')
            self.assertEqual(len(exported['cycles']), 2)
            for cy in exported['cycles']:
                self.assertEqual(set(cy['methods']), set(C.METHODS)); self.assertIsNotNone(cy['saved'])
            operations = [x['operation'] for x in native['commands']]
            self.assertEqual(operations.count('save-roundtrip'), 2)
            self.assertLess(max(i for i, x in enumerate(operations) if x == 'save-roundtrip'), operations.index('engine-info'))
            self.assertFalse(state['owned_ids']); self.assertTrue(exported['fresh_id_equals_baseline'])
            self.assertNotIn('PRIVATE', json.dumps(exported)); self.assertNotIn('PRIVATE', json.dumps(native))
            if mode == 'realistic':
                self.assertEqual(exported['engine']['containerd_version'], 'v1.7.27')
                self.assertEqual(exported['engine']['kernel'], '6.12.43+deb13-amd64')
                self.assertEqual(exported['cycles'][0]['methods'], {m: files(ZERO) for m in C.METHODS})
            if mode == 'bad-facts': self.assertTrue(C.is_unknown(exported['engine']['server_version']))
            if mode == 'bad-parsers':
                self.assertEqual(exported['verdict'], 'INCONSISTENT')
                self.assertTrue(C.is_unknown(exported['cycles'][0]['image']['diff_ids']))
                self.assertEqual(exported['cycles'][0]['methods']['run']['app/main.mjs']['state'], 'unknown')
            # The canned archive has an empty bundle, NOT the production bundle.
            self.assertEqual(exported['verdict'], 'INCONSISTENT')

    def test_real_probe_path_executable_docker_shim(self): self.exercise('realistic')

    def test_real_probe_path_failed_engine_facts_nonfatal(self): self.exercise('bad-facts')

    def test_real_probe_path_unknown_parsers_nonfatal(self): self.exercise('bad-parsers')

    def test_real_probe_path_unknown_original_archive_nonfatal(self): self.exercise('bad-archive')

    def test_wrong_archive_hash_still_rejected_before_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'backend.tar.gz').write_bytes(b'wrong archive')
            with patch.object(G, 'ROOT', root), patch.object(G, 'report', {'checks': [True], 'phases': ['probe']}), patch.object(G, 'run') as run:
                with self.assertRaises(AssertionError): G.probe()
                run.assert_not_called()

    def test_wrong_bound_id_in_inspect_still_rejected_before_measurements(self):
        with patch.object(G, 'report', {'baseline_id': C.BOUND_ID}), \
             patch.object(G, 'docker_json', return_value=[{'Id': 'sha256:' + 'f' * 64}]), patch.object(G, 'run') as run:
            with self.assertRaises(AssertionError): G.cycle('as_found', arch())
            run.assert_not_called()

    def test_optional_fact_missing_binary_timeout_and_invalid_json(self):
        for exception in (FileNotFoundError(), TimeoutError()):
            with patch.object(G, 'run', side_effect=exception): self.assertTrue(C.is_unknown(G.optional_fact('fixture', ['missing'])))
        with patch.object(G, 'run', return_value=(0, b'PRIVATE invalid JSON')):
            self.assertEqual(G.optional_fact('fixture', ['missing'], True), C.unparsed(b'PRIVATE invalid JSON'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
