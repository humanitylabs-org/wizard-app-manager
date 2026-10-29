"""Focused QA oracle regressions. No Docker, dependency install or product API."""
import ast

import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


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


if __name__ == '__main__': unittest.main()
