"""Focused runner regressions; invoked by the parent-admitted early worker."""
import importlib.util
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
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(m.endswith(',readonly') for m in mounts))
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertNotIn('--privileged', cmd)
        self.assertEqual(early.RESERVE, 214748364800)
        self.assertEqual(build.FOOTPRINT, {'focused': 17179869184, 'image': 42949672960})

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
            self.assertEqual({r['name'] for r in result['installed']}, {'@scope/present', 'child'})
            workspace.joinpath('package.json').unlink()
            result = evidence.compare_install(root)
            self.assertFalse(result['workspace_comparison'][1]['exists'])
            self.assertIn('common', result['candidate_missing_or_mismatch'])

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
