"""Focused runner regressions; invoked by the parent-admitted early worker."""
import ast
import importlib.util
import inspect
import tarfile
import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS/(name+'.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load('manager-seal-build')
early = load('manager-seal-early')
evidence = load('manager_seal_install_evidence')


class ManagerSealRunners(unittest.TestCase):
    def test_qa_tracked_source_collision_uses_fresh_named_output_and_preserves_prior(self):
        with tempfile.TemporaryDirectory(prefix='manager-seal-runner-') as tmp:
            root = Path(tmp)
            (root/'qa').mkdir()
            source = root/'qa/tracked-fixture'
            source.write_text('preserve source')
            output = build.output_directory(root, 'focused')
            prior = output/'focused-receipt.json'
            prior.write_text('preserve previous evidence')
            self.assertEqual(output, root/'manager-seal-results')
            self.assertEqual(build.output_directory(root, 'image'), output)
            with self.assertRaises(FileExistsError):
                build.output_directory(root, 'focused')
            self.assertEqual(source.read_text(), 'preserve source')
            self.assertEqual(prior.read_text(), 'preserve previous evidence')

    def test_early_dependency_scratch_is_tmpfs_capped_no_swap_readonly_no_socket_no_host_cache(self):
        cmd = early.create_command(Path('/srv/work/manager-seal-synthetic'), 'synthetic', 1234, 1234, 'a'*40, 'b'*64)
        self.assertEqual(cmd[cmd.index('--memory')+1], str(early.MEMORY))
        self.assertEqual(cmd[cmd.index('--memory-swap')+1], str(early.MEMORY))
        self.assertIn('--read-only', cmd)
        self.assertIn('--pull=never', cmd)
        self.assertIn('--cap-drop=ALL', cmd)
        self.assertIn('--security-opt=no-new-privileges:true', cmd)
        self.assertIn('--log-driver=none', cmd)
        tmpfs = cmd[cmd.index('--tmpfs')+1]
        self.assertIn('size='+str(early.SCRATCH), tmpfs)
        self.assertIn('mode=0700,uid=1234,gid=1234', tmpfs)
        mounts = [cmd[i+1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all(m.endswith(',readonly') for m in mounts))
        self.assertIn('type=bind,src=/srv/work/manager-seal-synthetic/manager-seal-early-results/bootstrap-tmp,dst=/tmp,readonly', mounts)
        self.assertEqual(cmd.count('--tmpfs'), 1)
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertNotIn('--privileged', cmd)
        self.assertEqual(early.RESERVE, 214748364800)
        self.assertEqual(build.FOOTPRINT, {'focused': 17179869184, 'image': 42949672960})

    def test_bunx_redirect_is_one_uid_bound_readonly_alias_not_another_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            alias = early.prepare_bunx_tmp_alias(output, 1234)
            self.assertEqual([p.name for p in alias.iterdir()], ['bunx-1234-node-gyp@latest'])
            link = alias/'bunx-1234-node-gyp@latest'
            self.assertTrue(link.is_symlink())
            self.assertEqual(str(link.readlink()), '/work/tmp/bunx-1234-node-gyp@latest')
            self.assertEqual(alias.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                early.prepare_bunx_tmp_alias(output, 1234)

    def test_bunx_alias_verifies_actual_write_through_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alias, target_parent = root/'alias', root/'target'
            alias.mkdir()
            target_parent.mkdir()
            name = early.bunx_node_gyp_cache(1234)
            target = target_parent/name
            target.mkdir()
            link = alias/name
            link.symlink_to(target, target_is_directory=True)
            result = early.verify_bunx_tmp_alias(alias, target_parent, 1234)
            self.assertTrue(result['write_through_verified'])
            self.assertEqual(list(target.iterdir()), [])
            (alias/'unexpected').touch()
            with self.assertRaises(AssertionError):
                early.verify_bunx_tmp_alias(alias, target_parent, 1234)
            (alias/'unexpected').unlink()
            link.unlink()
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(AssertionError):
                early.verify_bunx_tmp_alias(alias, target_parent, 1234)

    def test_workspace_bins_resolve_locally_without_root_hoist_and_refuse_missing_or_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'source'
            bins = root/'packages/backend/node_modules/.bin'
            bins.mkdir(parents=True)
            store = root/'node_modules/.bun/tool@1/node_modules/tool'
            store.mkdir(parents=True)
            target = store/'cli.js'
            target.write_text('#!/usr/bin/env node\n')
            target.chmod(0o755)
            for name in ('tsgo', 'vitest'):
                (bins/name).symlink_to(target)
                self.assertEqual(early.workspace_executable(root, 'backend', name), bins/name)
            self.assertFalse((root/'node_modules/.bin').exists())
            (bins/'tsgo').unlink()
            with self.assertRaisesRegex(RuntimeError, 'workspace-executable-missing'):
                early.workspace_executable(root, 'backend', 'tsgo')
            outside = Path(tmp)/'external-tool'
            outside.write_text('synthetic')
            outside.chmod(0o755)
            (bins/'tsgo').symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, 'workspace-executable-escape'):
                early.workspace_executable(root, 'backend', 'tsgo')
            target.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'workspace-executable-missing'):
                early.workspace_executable(root, 'backend', 'vitest')

    def test_toolchain_required_files_pinned_and_environment_cannot_use_latest_gyp(self):
        cmd = early.create_command(Path('/srv/work/manager-seal-synthetic'), 'synthetic', 1000, 1000, 'a'*40, 'b'*64)
        env = [cmd[i+1] for i, value in enumerate(cmd) if value == '--env']
        for setting in ('CC=/usr/bin/gcc', 'CXX=/usr/bin/g++', 'MAKE=/usr/bin/make',
                        'npm_config_node_gyp=/work/bin/node-gyp', 'npm_config_python=/usr/bin/python3',
                        'npm_config_nodedir=/work/tooling/node-v22.22.3', 'npm_config_jobs=2', 'MAKEFLAGS=-j2'):
            self.assertIn(setting, env)
        self.assertIn(early.IMAGE, cmd)
        self.assertEqual(set(early.TOOL_SHA), {'/usr/bin/gcc', '/usr/bin/g++', '/usr/bin/ld',
                                            '/usr/bin/make', '/usr/bin/python3', early.NODE_GYP})
        self.assertTrue(all(len(value) == 64 for value in early.TOOL_SHA.values()))
        self.assertEqual(early.NODE_SHA, 'e6ec2c188d83d813f81f2de8aea084d74dce603ac1abedd0a30ad941b10087b2')
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp)/'node-gyp/bin/node-gyp.js'
            tool.parent.mkdir(parents=True)
            tool.write_text('synthetic')
            package = tool.parents[1]/'package.json'
            package.write_text('{"version":"11.5.0"}')
            with patch.object(early, 'TOOL_SHA', {str(tool): early.digest(tool)}), patch.object(early, 'NODE_GYP', str(tool)):
                with self.assertRaisesRegex(RuntimeError, 'toolchain-missing-executable'):
                    early.verify_tool_files()
                tool.chmod(0o755)
                self.assertEqual(early.verify_tool_files(), {str(tool): early.digest(tool)})
                tool.write_text('changed')
                with self.assertRaisesRegex(RuntimeError, 'toolchain-executable-hash'):
                    early.verify_tool_files()

    def test_toolchain_compile_link_load_is_before_full_install_and_propagates_failure(self):
        tree = ast.parse(inspect.getsource(early.inside))
        calls = {n.func.id: n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in ('toolchain_preflight', 'dependency_step')}
        self.assertLess(calls['toolchain_preflight'], calls['dependency_step'])
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work/'logs').mkdir()
            calls = []
            def download(url, path, sha, cap): path.touch()
            def headers(archive, target):
                p = target/('node-'+early.NODE_VERSION+'/include/node/node_version.h')
                p.parent.mkdir(parents=True)
                p.write_text('#define NODE_MAJOR_VERSION 22\n#define NODE_MINOR_VERSION 22\n#define NODE_PATCH_VERSION 3\n')
                return {'synthetic': True}
            def step(label, argv, cwd):
                calls.append((label, argv, cwd))
                if label == 'toolchain-node-version':
                    (work/'logs/toolchain-node-version.log').write_text(early.NODE_VERSION+'\n')
                if label == 'toolchain-node-gyp-build':
                    raise RuntimeError(label+' failed: 23')
            with patch.object(early, 'verify_tool_files', return_value={}), \
                 patch.object(early, 'download_public', side_effect=download), \
                 patch.object(early, 'extract_node_headers', side_effect=headers):
                with self.assertRaisesRegex(RuntimeError, 'toolchain-node-gyp-build failed: 23'):
                    early.toolchain_preflight(work, step)
            self.assertIn(('toolchain-compile-link-make', ['make', '-j2'], work/'toolchain-probe'), calls)
            self.assertNotIn('toolchain-node-gyp-load', [row[0] for row in calls])
            self.assertIn('napi_create_int32', (work/'toolchain-probe/addon.cc').read_text())

    def test_headers_reject_escape_links_and_inflated_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, (name, kind, size) in enumerate([
                    ('../escape', tarfile.REGTYPE, 0),
                    ('node-'+early.NODE_VERSION+'/link', tarfile.SYMTYPE, 0),
                    ('node-'+early.NODE_VERSION+'/huge', tarfile.REGTYPE, 256*1024**2+1)]):
                archive = root/f'{i}.tgz'
                with tarfile.open(archive, 'w:gz') as tf:
                    m = tarfile.TarInfo(name)
                    m.type, m.size, m.linkname = kind, size, '/outside'
                    # Deliberately header-only malformed archive. Python 3.14
                    # rejects addfile(nonzero-size, fileobj=None) before the
                    # extraction preflight under test can inspect the header.
                    tf.fileobj.write(m.tobuf())
                with self.assertRaisesRegex(RuntimeError, 'toolchain-headers-'):
                    early.extract_node_headers(archive, root/f'out-{i}')

    def test_error_filenames_cannot_exhaust_or_match_real_error_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'install.log'
            noise = (b'[extract] react-error-boundary/error.js better-ajv-errors/not-found.js\n'*5000)
            path.write_bytes(noise + b'\x1b[31merror\x1b[0m: ENOENT failed copying package real-missing\n' + noise)
            clean = Path(tmp)/'clean.log'
            metadata = evidence.sanitize_log(path, clean)
            rows = evidence.error_index(clean)
            self.assertEqual(len(rows), 1)
            self.assertIn('real-missing', rows[0]['text'])
            self.assertEqual(rows[0]['line'], 5001)
            self.assertEqual(metadata['raw_sha256'], evidence.sha(path))
            self.assertEqual(metadata['sanitized_sha256'], evidence.sha(clean))

    def test_http_header_sentinels_removed_before_export_not_package_names(self):
        data = (b'\x1b[32m[fetch] < Set-Cookie: private-cookie-sentinel\x1b[0m\n'
                b'> Authorization: private-auth-sentinel\nCookie: private-cookie2-sentinel\n'
                b'  X-Api-Key: private-key-sentinel\n[fetch] < Server: private-header-sentinel\n'
                b'Location: private-location-sentinel\n'
                b'error: EACCES failed copying cookie-parser\n'
                b'[extract] cookie-parser/index.js\n')
        clean = b''.join(evidence.sanitize_line(line) for line in data.splitlines(keepends=True))
        self.assertNotIn(b'private-', clean)
        self.assertIn(b'error: EACCES failed copying cookie-parser', clean)
        self.assertIn(b'[extract] cookie-parser/index.js', clean)

    def test_full_evidence_transport_roundtrip_not_tail_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = {}
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                for name in ('dependencies.sanitized.log', 'dependency-comparison.json', 'dependency-errors.json'):
                    path = root/name
                    data = b'begin\n'+b'x'*100000+b'\nerror: middle\n'+b'y'*100000+b'\nend\n'
                    path.write_bytes(data)
                    expected[name] = evidence.emit_artifact(path)
            log = root/'transport.log'
            log.write_text(stream.getvalue())
            output = root/'recovered'
            output.mkdir()
            self.assertEqual(evidence.recover_artifacts(log, output), expected)
            for name in expected:
                self.assertEqual((root/name).read_bytes(), (output/name).read_bytes())
            self.assertNotIn('data', expected['dependencies.sanitized.log'])

    def test_export_refuses_incomplete_corrupt_or_oversize_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'dependencies.sanitized.log'
            path.write_text('public log')
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                evidence.emit_artifact(path)
            lines = stream.getvalue().splitlines(keepends=True)
            for i, text in enumerate((''.join(lines[:-1]), ''.join(lines).replace('cHVibGljIGxvZw==', 'YmFk'), ''.join(lines).replace('"bytes": 10', '"bytes": 999999999'))):
                log = root/f'bad-{i}.log'
                log.write_text(text)
                output = root/f'out-{i}'
                output.mkdir()
                with self.assertRaises((AssertionError, ValueError)):
                    evidence.recover_artifacts(log, output)
            with patch.object(evidence, 'ARTIFACT_CAP', 2):
                with self.assertRaises(AssertionError):
                    evidence.emit_artifact(path)

    def test_lock_parser_preserves_string_commas_and_nested_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bun.lock'
            path.write_text('{"lockfileVersion":1,"literal":",}","packages":{},}')
            self.assertEqual(evidence.read_lock(path)['literal'], ',}')
        self.assertEqual(str(evidence.lock_path('@a/b/@c/d/e')), 'node_modules/@a/b/node_modules/@c/d/node_modules/e')

    def test_comparison_identifies_missing_platform_workspace_and_actual_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'package.json').write_text('{"name":"root"}')
            workspace = root/'packages/common'
            workspace.mkdir(parents=True)
            (workspace/'package.json').write_text('{"name":"common","version":"1.0.0"}')
            present = root/'node_modules/@scope/present'
            present.mkdir(parents=True)
            (present/'package.json').write_text('{"name":"@scope/present","version":"1.2.3"}')
            (root/'node_modules/common').symlink_to(workspace, target_is_directory=True)
            nested = present/'node_modules/child'
            nested.mkdir(parents=True)
            (nested/'package.json').write_text('{"name":"child","version":"2.0.0"}')
            lock = {'lockfileVersion':1, 'workspaces':{'':{'name':'root'}, 'packages/common':{'name':'common','version':'1.0.0'}},
                    'packages':{'@scope/present':['@scope/present@1.2.3'], 'common':['common@workspace:packages/common'],
                                '@scope/present/child':['child@2.0.0'], 'missing':['missing@3.0.0'],
                                'other-os':['other-os@1.0.0','',{'os':'darwin'}]}}
            (root/'bun.lock').write_text(json.dumps(lock))
            result = evidence.compare_install(root)
            self.assertEqual(result['candidate_missing_or_mismatch'], ['missing'])
            self.assertTrue(all(not row['differences'] for row in result['workspace_comparison']))
            self.assertEqual(result['lock_package_count'], 5)
            self.assertEqual({r['name'] for r in result['installed']}, {'@scope/present', 'child', 'common'})
            workspace.joinpath('package.json').unlink()
            result = evidence.compare_install(root)
            self.assertFalse(result['workspace_comparison'][1]['exists'])
            self.assertIn('common', result['candidate_missing_or_mismatch'])

    def test_isolated_store_scopes_versions_peers_and_bins_are_not_missing_hoists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'package.json').write_text('{"name":"root"}')
            for version, peer in [('1.2.3', '+aaaa'), ('1.2.3', '+bbbb'), ('2.0.0', '')]:
                package = root/f'node_modules/.bun/@scope+tool@{version}{peer}/node_modules/@scope/tool'
                package.mkdir(parents=True)
                (package/'package.json').write_text(json.dumps({'name':'@scope/tool', 'version':version}))
                (package/'cli.js').write_text('#!/usr/bin/env node\n')
                (package/'cli.js').chmod(0o755)
            lock = {'lockfileVersion':1, 'workspaces':{'':{'name':'root'}}, 'packages':{
                '@scope/tool':['@scope/tool@1.2.3', '', {'bin':{'tool':'cli.js'}}],
                'parent/@scope/tool':['@scope/tool@2.0.0'],
                'absent-version':['@scope/tool@3.0.0']}}
            (root/'bun.lock').write_text(json.dumps(lock))
            result = evidence.compare_install(root)
            self.assertEqual(result['candidate_missing_or_mismatch'], ['absent-version'])
            self.assertEqual(len(result['installed']), 3)
            row = result['packages'][0]
            self.assertEqual(row['hoisted_state'], 'missing')
            self.assertEqual(row['state'], 'present')
            self.assertEqual(len(row['locations']), 2)
            self.assertTrue(row['bins'][0]['exists'])
            self.assertTrue(row['bins'][0]['executable'])

    def test_inventory_deduplicates_store_links_without_reading_escape_or_source_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp)/'export', Path(tmp)/'outside'
            root.mkdir()
            outside.mkdir()
            (outside/'package.json').write_text('not-json-must-not-be-read')
            package = root/'node_modules/.bun/tool@1.0.0/node_modules/tool'
            package.mkdir(parents=True)
            (package/'package.json').write_text('{"name":"tool","version":"1.0.0"}')
            (root/'node_modules/tool').symlink_to(package, target_is_directory=True)
            (root/'node_modules/escape').symlink_to(outside, target_is_directory=True)
            source = package/'src'
            source.mkdir()
            (source/'package.json').write_text('not-json-must-not-be-read')
            installed = evidence.installed_inventory(root, {})
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0]['name'], 'tool')

    def test_capture_preserves_exit_timeout_and_explicit_byte_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = evidence.capture_install([sys.executable, '-c', 'print("public error"); raise SystemExit(23)'], root, root/'exit.log', 5)
            self.assertEqual(result['exit_code'], 23)
            self.assertTrue(result['log_complete'])
            result = evidence.capture_install([sys.executable, '-c', 'import time; time.sleep(10)'], root, root/'timeout.log', 0.05)
            self.assertEqual(result['termination_reason'], 'timeout')
            self.assertFalse(result['log_complete'])
            self.assertNotEqual(result['exit_code'], 0)
            with patch.object(evidence, 'INSTALL_LOG_CAP', 64):
                result = evidence.capture_install([sys.executable, '-c', 'import os; os.write(1,b"x"*1000000)'], root, root/'cap.log', 5)
            self.assertEqual(result['termination_reason'], 'log-byte-cap')
            self.assertFalse(result['log_complete'])
            self.assertLessEqual(result['log_bytes'], 64+65536)

    def test_dependency_failure_retains_real_exit_if_comparison_itself_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outcome = {}
            def capture(argv, cwd, log, timeout):
                log.write_bytes(b'error: ENOENT exact-package\nSet-Cookie: secret-sentinel\n')
                return {'exit_code':1, 'log_complete':True, 'termination_reason':None,
                        'log_sha256':evidence.sha(log), 'log_bytes':log.stat().st_size}
            stream = io.StringIO()
            with patch.object(evidence, 'capture_install', side_effect=capture) as call, contextlib.redirect_stdout(stream):
                evidence.dependency_step(root, root, 'a'*64, outcome)
            call.assert_called_once()
            self.assertEqual(outcome['exit_code'], 1)
            self.assertFalse(outcome['comparison_complete'])
            self.assertEqual(len(outcome['artifacts']), 3)
            transport = root/'transport.log'
            transport.write_text(stream.getvalue())
            output = root/'recovered'
            output.mkdir()
            self.assertEqual(evidence.recover_artifacts(transport, output), outcome['artifacts'])
            self.assertNotIn(b'secret-sentinel', (output/'dependencies.sanitized.log').read_bytes())
            self.assertIn(b'exact-package', (output/'dependency-errors.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
