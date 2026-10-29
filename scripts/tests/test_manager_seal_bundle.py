"""Focused QA oracle regressions. No Docker, dependency install or product API."""
import ast
import io

import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import manager_seal_bundle as bundle
import manager_seal_continuation as continuation


class BundleOracle(unittest.TestCase):
    def fixture(self, root):
        source = root/'source'; dist = root/'dist'; dist.mkdir()
        (source/'packages/backend').mkdir(parents=True)
        (source/'packages/backend/build.ts').write_text("await Bun.build({target: 'node', sourcemap: true, outdir: './dist'});\n")
        lines = []; sources = []; contents = []
        for name, (package, version, rel, binding, integrity) in bundle.ADDONS.items():
            lines.append('    "'+package+'": ["'+package+'@'+version+'", "", {}, "'+integrity+'"],')
            sources.append('../../../node_modules/.bun/'+package+'@'+version+'/node_modules/'+package+'/'+rel)
            contents.append(binding+';\n')
            (dist/name).write_bytes(b'\x7fELF\x02\x01'+b'\0'*10+b'\x03\x00\x3e\x00'+b'\0'*16)
        (source/'bun.lock').write_text('\n'.join(lines))
        (dist/'main.js').write_text('/*'+' synthetic '*110+'*/\n'+'\n'.join('require("./'+n+'");' for n in bundle.ADDONS)+'\n//# sourceMappingURL=main.js.map\n')
        (dist/'main.js.map').write_text(json.dumps({'version': 3, 'sources': sources, 'sourcesContent': contents}))
        return source, dist

    def test_exact_native_inventory_references_provenance_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dist = self.fixture(Path(tmp))
            result = bundle.validate_bundle(dist, source)
            self.assertEqual(set(result['files']), set(bundle.SAVED_BUNDLE))
            self.assertEqual(set(result['native_provenance']), set(bundle.ADDONS))
            self.assertTrue(all(len(m['sha256']) == 64 for m in result['files'].values()))
            self.assertFalse(result['saved_bytes_pinned'])
            with self.assertRaisesRegex(AssertionError, 'saved bundle bytes drift'):
                bundle.validate_bundle(dist, source, saved=True)

    def test_frozen_installed_package_outputs_must_match_source_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source, dist = self.fixture(root)
            installed = root/'installed'
            for package, version, rel, binding, _ in bundle.ADDONS.values():
                p = installed/'node_modules/.bun'/str(package+'@'+version)/'node_modules'/package
                (p/rel).parent.mkdir(parents=True)
                (p/rel).write_text(binding+';\n')
                (p/'package.json').write_text(json.dumps({'version': version}))
            result = bundle.validate_bundle(dist, source, installed=installed)
            self.assertTrue(all(v['frozen_install_source_compared'] for v in result['native_provenance'].values()))
            package, version, rel, _, _ = next(iter(bundle.ADDONS.values()))
            p = installed/'node_modules/.bun'/str(package+'@'+version)/'node_modules'/package
            (p/rel).write_text('unreviewed source')
            with self.assertRaisesRegex(AssertionError, 'frozen package output'):
                bundle.validate_bundle(dist, source, installed=installed)

    def test_no_blanket_node_allowlist_and_no_missing_files(self):
        for name in ['unreviewed.node', 'cpufeatures-newhash.node', 'nested', 'main.js.bak']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                source, dist = self.fixture(Path(tmp)); (dist/name).write_bytes(b'unknown')
                with self.assertRaisesRegex(AssertionError, 'inventory'): bundle.validate_bundle(dist, source)
        with tempfile.TemporaryDirectory() as tmp:
            source, dist = self.fixture(Path(tmp)); (dist/next(iter(bundle.ADDONS))).unlink()
            with self.assertRaisesRegex(AssertionError, 'inventory'): bundle.validate_bundle(dist, source)

    def test_links_directories_fifo_and_hardlinks_refused(self):
        for kind in ['symlink', 'directory', 'fifo', 'hardlink']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source, dist = self.fixture(root)
                target = dist/'main.js.map'; data = target.read_bytes(); target.unlink()
                actual = root/'outside'; actual.write_bytes(data)
                if kind == 'symlink': target.symlink_to(actual)
                elif kind == 'directory': target.mkdir()
                elif kind == 'fifo': os.mkfifo(target)
                else: os.link(actual, target)
                with self.assertRaisesRegex(AssertionError, 'regular single-link'): bundle.validate_bundle(dist, source)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source, dist = self.fixture(root); (root/'alias').symlink_to(dist)
            with self.assertRaisesRegex(AssertionError, 'real directory'): bundle.validate_bundle(root/'alias', source)

    def test_empty_and_bounded_sparse_oversize_files_refused_before_read(self):
        for size in [0, 4*bundle.MIB+1]:
            with tempfile.TemporaryDirectory() as tmp:
                source, dist = self.fixture(Path(tmp)); name = next(iter(bundle.ADDONS))
                with (dist/name).open('wb') as f: f.truncate(size)
                with self.assertRaisesRegex(AssertionError, 'size bound'): bundle.validate_bundle(dist, source)

    def test_missing_duplicate_unknown_and_traversing_js_native_references_refused(self):
        name = next(iter(bundle.ADDONS))
        for replacement in ['', 'require("./'+name+'");require("./'+name+'");', 'require("./evil.node");', 'require("./../evil.node");',
                            'require("/outside/evil.node");', 'require("evil.node");', 'require("../evil.node");']:
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                source, dist = self.fixture(Path(tmp)); js = dist/'main.js'
                js.write_text(js.read_text().replace('require("./'+name+'");', replacement))
                with self.assertRaisesRegex(AssertionError, 'native references'): bundle.validate_bundle(dist, source)

    def test_elf_architecture_lock_version_and_integrity_are_not_waived(self):
        for mutation in ['elf', 'version', 'integrity']:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                source, dist = self.fixture(Path(tmp))
                if mutation == 'elf': (dist/next(iter(bundle.ADDONS))).write_bytes(b'not an ELF')
                else:
                    lock = source/'bun.lock'
                    lock.write_text(lock.read_text().replace('cpu-features@0.0.10', 'cpu-features@0.0.11') if mutation == 'version'
                        else lock.read_text().replace(bundle.ADDONS[next(iter(bundle.ADDONS))][-1], 'sha512-unreviewed'))
                with self.assertRaises(AssertionError): bundle.validate_bundle(dist, source)

    def test_source_map_binding_inclusion_and_identity_required(self):
        for mutation in ['binding', 'source', 'duplicate', 'version', 'content-length']:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                source, dist = self.fixture(Path(tmp)); file = dist/'main.js.map'; data = json.loads(file.read_text())
                if mutation == 'binding': data['sourcesContent'][0] = 'unrelated source'
                elif mutation == 'source': data['sources'][0] = 'unreviewed package'
                elif mutation == 'duplicate': data['sources'].append(data['sources'][0]); data['sourcesContent'].append(data['sourcesContent'][0])
                elif mutation == 'version': data['version'] = 2
                else: data['sourcesContent'].pop()
                file.write_text(json.dumps(data))
                with self.assertRaises(AssertionError): bundle.validate_bundle(dist, source)

    def test_empty_assertion_has_trusted_phase_reason_and_no_payload(self):
        try: raise AssertionError()
        except AssertionError as exc: value = bundle.failure_record(exc, 'candidate-bundle-inventory')
        self.assertEqual(value['phase'], 'candidate-bundle-inventory')
        self.assertEqual(value['reason'], 'assertion_failed')
        self.assertEqual(value['exception_type'], 'AssertionError')
        sentinel = 'PRIVATE-SENTINEL-never-export'
        try: raise AssertionError(sentinel)
        except AssertionError as exc: value = bundle.failure_record(exc, 'baseline-specific-failure-validation')
        self.assertNotIn(sentinel, json.dumps(value))
        self.assertEqual(value['frames'], [])  # unittest file is not a trusted runner frame

    def test_type_error_is_trusted_without_payload_or_custom_class_name(self):
        sentinel = 'PRIVATE-SENTINEL-never-export'
        for kind, expected in [(TypeError, 'TypeError'), (type(sentinel, (TypeError,), {}), 'TypeError'),
                               (type(sentinel, (Exception,), {}), 'Exception')]:
            try: raise kind(sentinel)
            except Exception as exc: value = bundle.failure_record(exc, 'baseline-source-closure')
            self.assertEqual(value['exception_type'], expected)
            self.assertEqual(value['reason'], 'invalid_type' if expected == 'TypeError' else 'unexpected_exception')
            self.assertNotIn(sentinel, json.dumps(value))
            self.assertEqual(value['frames'], [])

    def test_trusted_bundle_trace_has_exact_file_function_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dist = self.fixture(Path(tmp)); (dist/'evil.node').write_bytes(b'x')
            value = None
            try: bundle.validate_bundle(dist, source)
            except AssertionError as exc: value = bundle.failure_record(exc, 'saved-artifact-validation')
            assert value is not None
            self.assertEqual(value['frames'][-1]['file'], 'manager_seal_bundle.py')
            self.assertEqual(value['frames'][-1]['function'], 'validate_bundle')
            self.assertGreater(value['frames'][-1]['line'], 0)

    def test_continuation_never_runs_accepted_candidate_or_adjacent_commands(self):
        text = (SCRIPTS/'manager_seal_continuation.py').read_text(); tree = ast.parse(text)
        labels = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == 'step' and n.args and isinstance(n.args[0], ast.Constant)]
        self.assertEqual(set(labels), {'saved-bundle-node-syntax', 'native-bundler-filename-probe', 'continuation-focused-harness', 'common-build', 'baseline-negative'})
        self.assertNotIn('candidate-real-docker', labels)
        self.assertNotIn('candidate-backend-bundle', labels)
        self.assertIn('verify_saved(root, manifest, runner)', text)
        self.assertIn('changes <= QA_DELTA', text)
        self.assertNotIn('packages/backend/src', '\n'.join(continuation.QA_DELTA))

    def test_inheritance_refuses_any_product_fixture_lock_or_baseline_dependency_change(self):
        previous = {'head': continuation.SOURCE, 'baseline_tar_sha256': 'pinned-baseline',
                    'files': {'bun.lock': 'old-lock', 'scripts/manager-seal-docker.py': 'old-harness',
                        'packages/backend/src/modules/docker/docker.service.ts': 'old-product',
                        'packages/backend/src/modules/docker/__tests__/mandatory-seal.test.ts': 'old-fixture'}}
        updated = json.loads(json.dumps(previous)); updated['files']['scripts/manager-seal-docker.py'] = 'new-harness'
        updated['files']['scripts/manager_seal_bundle.py'] = 'new-validator'
        self.assertEqual(continuation.source_closure(previous, updated), {'scripts/manager-seal-docker.py', 'scripts/manager_seal_bundle.py'})
        for target in ['bun.lock', 'packages/backend/src/modules/docker/docker.service.ts',
                       'packages/backend/src/modules/docker/__tests__/mandatory-seal.test.ts', 'new-unreviewed-file']:
            with self.subTest(target=target):
                bad = json.loads(json.dumps(updated)); bad['files'][target] = 'changed'
                with self.assertRaisesRegex(AssertionError, 'cannot be inherited'): continuation.source_closure(previous, bad)
        updated['baseline_tar_sha256'] = 'other-baseline'
        with self.assertRaisesRegex(AssertionError, 'source identity'): continuation.source_closure(previous, updated)

    def test_saved_archive_rejects_unexpected_paths_links_duplicates_and_size(self):
        for bad in ['evil.node', '../inside-result.json', '/inside-result.json', 'symlink', 'hardlink', 'oversize', 'duplicate']:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); (root/'saved').mkdir()
                (root/'saved/docker-receipt.json').write_text(json.dumps({'result': {'steps': {}}, 'dependencies': {}}))
                with tarfile.open(root/'saved/docker-artifacts.tar.gz', 'w:gz') as tf:
                    member = tarfile.TarInfo('inside-result.json' if bad in ['symlink', 'hardlink', 'oversize', 'duplicate'] else bad)
                    if bad in ['symlink', 'hardlink']:
                        member.type = tarfile.SYMTYPE if bad == 'symlink' else tarfile.LNKTYPE; member.linkname = '/outside'
                    elif bad == 'oversize': member.size = 33*bundle.MIB
                    if bad == 'oversize':
                        # Header-only malformed archive: bound must fire BEFORE attempting payload extraction.
                        tf.fileobj.write(member.tobuf())
                    else:
                        tf.addfile(member)
                        if bad == 'duplicate': tf.addfile(member)
                class Runner: MIB = bundle.MIB
                with self.assertRaises((AssertionError, tarfile.ReadError)):
                    continuation.extract_saved(root, root/'out', Runner)


class BaselineArchive(unittest.TestCase):
    def archive(self, root, entries):
        archive = root/'baseline.tar'
        with tarfile.open(archive, 'w') as tf:
            for name, kind, data, mode in entries:
                m = tarfile.TarInfo(name); m.type = kind; m.mode = mode
                m.uid = m.gid = 12345; m.mtime = 1
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE): m.linkname = '/outside'
                m.size = len(data)
                tf.addfile(m, io.BytesIO(data))
        return archive

    def regular(self, name='nested/source.ts', data=b'PUBLIC synthetic source\n', mode=0o666):
        return (name, tarfile.REGTYPE, data, mode)

    def test_valid_nested_bytes_private_modes_ignore_ownership_and_no_filter_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); destination = root/'out'; destination.mkdir(mode=0o700)
            archive = self.archive(root, [self.regular(), ('nested', tarfile.DIRTYPE, b'', 0o7777),
                self.regular('nested/run.sh', b'#!/bin/sh\n', 0o7777), self.regular('empty', b'')])
            # Forbid both convenience extract APIs even on Python with filters.
            with (patch.object(tarfile.TarFile, 'extractall', side_effect=AssertionError('unsafe extractall')),
                  patch.object(tarfile.TarFile, 'extract', side_effect=AssertionError('unsafe extract'))):
                count = continuation.extract_baseline(archive, destination)
            self.assertEqual(count, len(b'PUBLIC synthetic source\n#!/bin/sh\n'))
            self.assertEqual((destination/'nested/source.ts').read_bytes(), b'PUBLIC synthetic source\n')
            for name, mode in [('nested', 0o700), ('nested/source.ts', 0o600), ('nested/run.sh', 0o700), ('empty', 0o600)]:
                s = (destination/name).stat()
                self.assertEqual(stat.S_IMODE(s.st_mode), mode)
                self.assertEqual(s.st_uid, os.getuid())
            self.assertEqual((destination/'empty').stat().st_size, 0)

    def test_absolute_traversal_ambiguous_paths_and_duplicates_prevalidated(self):
        for name in ['/outside', '../outside', 'nested/../../outside', './file', 'nested/./file',
                     'nested//file', 'nested/../file', 'back\\slash', '']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
                archive = self.archive(root, [self.regular('first'), self.regular(name)])
                with self.assertRaises(AssertionError): continuation.extract_baseline(archive, out)
                self.assertEqual(list(out.iterdir()), [])
        for entries in [[self.regular('same'), self.regular('same')],
                        [self.regular('same'), ('same', tarfile.DIRTYPE, b'', 0o700)],
                        [self.regular('same'), self.regular('same/child')]]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
                with self.assertRaises(AssertionError): continuation.extract_baseline(self.archive(root, entries), out)
                self.assertEqual(list(out.iterdir()), [])

    def test_links_special_and_contiguous_file_members_refused(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE,
                     tarfile.BLKTYPE, tarfile.CONTTYPE, b'Z']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
                with self.assertRaises(AssertionError):
                    continuation.extract_baseline(self.archive(root, [self.regular('first'), ('bad', kind, b'', 0o777)]), out)
                self.assertEqual(list(out.iterdir()), [])

    def test_sparse_negative_and_oversize_headers_rejected_before_payload(self):
        for mutation in ['sparse', 'negative', 'oversize', 'directory-data', 'linkname']:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
                archive = root/'baseline.tar'; m = tarfile.TarInfo('bad')
                if mutation == 'sparse': m.pax_headers = {'GNU.sparse.size': '1'}
                elif mutation == 'negative': m.size = -1
                elif mutation == 'oversize': m.size = continuation.BASELINE_BYTES+1
                elif mutation == 'directory-data': m.type = tarfile.DIRTYPE; m.size = 1
                else: m.linkname = '/outside'
                archive.write_bytes(m.tobuf(format=tarfile.PAX_FORMAT)+b'\0'*10240)
                with self.assertRaises((AssertionError, tarfile.ReadError)):
                    continuation.extract_baseline(archive, out)
                self.assertEqual(list(out.iterdir()), [])

    def test_member_count_and_aggregate_budget_before_writes(self):
        for constant, limit, entries in [('BASELINE_MEMBERS', 1, [self.regular('a'), self.regular('b')]),
                                        ('BASELINE_BYTES', 4, [self.regular('a', b'123'), self.regular('b', b'456')])]:
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
                with patch.object(continuation, constant, limit):
                    with self.assertRaisesRegex(AssertionError, 'budget'):
                        continuation.extract_baseline(self.archive(root, entries), out)
                self.assertEqual(list(out.iterdir()), [])

    def test_symlink_root_and_any_existing_symlink_ancestor_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
            archive = self.archive(root, [self.regular()])
            alias = root/'alias'; alias.symlink_to(out, target_is_directory=True)
            ancestor = root/'ancestor'; ancestor.symlink_to(root, target_is_directory=True)
            for destination in [alias, ancestor/'out']:
                with self.subTest(destination=destination), self.assertRaises(OSError):
                    continuation.extract_baseline(archive, destination)
            self.assertEqual(list(out.iterdir()), [])

    def test_nonempty_or_wide_root_and_existing_link_entries_refused(self):
        for kind in ['wide', 'file', 'directory', 'symlink', 'hardlink']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
                archive = self.archive(root, [self.regular()])
                outside = root/'outside'; outside.write_bytes(b'unchanged')
                if kind == 'wide': out.chmod(0o755)
                elif kind == 'file': (out/'existing').write_bytes(b'unchanged')
                elif kind == 'directory': (out/'nested').mkdir()
                elif kind == 'symlink': (out/'nested').symlink_to(root, target_is_directory=True)
                else: os.link(outside, out/'existing')
                with self.assertRaises(AssertionError): continuation.extract_baseline(archive, out)
                self.assertEqual(outside.read_bytes(), b'unchanged')

    def test_symlink_inserted_at_nested_mkdir_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
            elsewhere = root/'elsewhere'; elsewhere.mkdir(mode=0o700)
            archive = self.archive(root, [self.regular()]); mkdir = os.mkdir
            def inject(path, mode=0o777, *, dir_fd=None):
                if path == 'nested':
                    os.symlink(str(elsewhere), path, dir_fd=dir_fd)
                    raise FileExistsError()
                return mkdir(path, mode, dir_fd=dir_fd)
            with patch.object(continuation.os, 'mkdir', side_effect=inject):
                with self.assertRaises(OSError): continuation.extract_baseline(archive, out)
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_truncated_payload_cannot_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'out'; out.mkdir(mode=0o700)
            m = tarfile.TarInfo('file'); m.size = 1024
            archive = root/'baseline.tar'; archive.write_bytes(m.tobuf()+b'abc')
            with self.assertRaises((AssertionError, tarfile.ReadError)):
                continuation.extract_baseline(archive, out)

    def test_both_runners_share_safe_helper_and_parent_requires_actual_3112(self):
        continued = (SCRIPTS/'manager_seal_continuation.py').read_text()
        full = (SCRIPTS/'manager-seal-docker.py').read_text()
        self.assertIn("extract_baseline(root/'baseline.tar', baseline)", continued)
        self.assertIn("continuation.extract_baseline(root/'baseline.tar', baseline)", full)
        self.assertNotIn('extractall(', continued+full)
        self.assertIn("assert sys.version_info[:3] == (3, 11, 2)", continued)
        self.assertLess(continued.index('assert sys.version_info[:3]'), continued.index("step('continuation-focused-harness'"))
        self.assertEqual(continuation.BASELINE_BYTES, 64*1024**2)
        self.assertEqual(continuation.BASELINE_MEMBERS, 9999)


if __name__ == '__main__': unittest.main()
