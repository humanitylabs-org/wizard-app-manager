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
    doc = {'schema': 'privsep-manager-probe-r2', 'original': arch(), 'engine': engine, 'cycles': cycles,
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

    def test_duplicate_fixed_member_refused(self):
        with self.assertRaises(ValueError):
            C.files_from_tar(io.BytesIO(tar_bytes([('app/main.mjs', b'a'), ('app/main.mjs', b'b')])))

    def test_symlink_fixed_member_refused(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as t:
            m = tarfile.TarInfo('app/main.mjs'); m.type = tarfile.SYMTYPE; m.linkname = '/private'; t.addfile(m)
        with self.assertRaises(ValueError): C.files_from_tar(io.BytesIO(out.getvalue()))

    def test_empty_or_bad_manifest_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad.tar'; p.write_bytes(tar_bytes([('manifest.json', b'[]')]))
            with self.assertRaises(ValueError): C.archive(p)


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
            with self.assertRaises(ValueError): C.parse_shell(raw)

    def test_cp_error_is_not_absence(self):
        cid = 'a' * 64
        self.assertTrue(C.cp_missing(('Error response from daemon: Could not find the file /app/main.js in container ' + cid + '\n').encode(), 'app/main.js', cid))
        for b in (b'permission denied', b'Cannot connect to the Docker daemon', b'No such container'):
            self.assertFalse(C.cp_missing(b, 'app/main.js', cid))

    def test_mount_paths_never_exported(self):
        raw = b'10 1 0:1 / / rw,relatime - overlay overlay rw,lowerdir=/SECRET_SENTINEL,upperdir=/private,workdir=/private\n'
        got = C.mount_facts(raw); self.assertEqual(got, MOUNT)
        self.assertNotIn('SECRET_SENTINEL', json.dumps(got))
        with self.assertRaises(ValueError): C.mount_facts(raw + raw)

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
