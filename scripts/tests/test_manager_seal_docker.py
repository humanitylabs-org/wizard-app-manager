"""Small harness regressions, NOT installed Docker/API security acceptance."""
import base64

import hashlib
import importlib.util

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('manager_docker', SCRIPTS/'manager-seal-docker.py')
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class DockerStageHarness(unittest.TestCase):
    def test_uses_actual_passing_early_runner_not_broken_focused_runner(self):
        text = (SCRIPTS/'manager-seal-docker.py').read_text()
        self.assertIn("early.toolchain_preflight(Path('/work'), step)", text)
        self.assertIn('early.verify_bunx_tmp_alias', text)
        self.assertIn("early.workspace_executable(checkout, 'backend', 'vitest')", text)
        self.assertNotIn("'backend-unit'", text)
        self.assertEqual(runner.MEASURED_SCRATCH, 1390694400)
        self.assertLess(runner.MEASURED_SCRATCH+64*1024**2+runner.ARTIFACT_BYTES, runner.early.SCRATCH)

    def command(self):
        return runner.create_command(Path('/srv/work/manager-seal-synthetic'), 'manager-test', 'm12345678',
            '/var/lib/docker/volumes/m12345678/_data', 1000, 1000, 104, 'a'*40, 'b'*64)

    def test_daemon_bind_root_is_same_canonical_volume_path_not_container_only_work(self):
        cmd = self.command()
        mounts = [cmd[i+1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertIn('type=volume,src=m12345678,dst=/var/lib/docker/volumes/m12345678/_data,volume-nocopy', mounts)
        self.assertEqual(cmd[cmd.index('--fixture-root')+1], '/var/lib/docker/volumes/m12345678/_data')
        self.assertNotIn('src=/work', ' '.join(mounts))
        self.assertLessEqual(len(('/var/lib/docker/volumes/m12345678/_data'+'/bind-probe.sock').encode()), 107)

    def test_socket_authority_explicit_no_privilege_or_unbounded_disk_cache(self):
        cmd = self.command()
        self.assertEqual(cmd[cmd.index('--user')+1], '1000:1000')
        self.assertIn('type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock', cmd)
        self.assertEqual(cmd[cmd.index('--group-add')+1], '104')
        self.assertIn('--read-only', cmd)
        self.assertIn('--log-driver=none', cmd)
        self.assertIn('--pull=never', cmd)
        self.assertNotIn('--privileged', cmd)
        self.assertEqual(cmd[cmd.index('--memory')+1], str(runner.early.MEMORY))
        self.assertEqual(cmd[cmd.index('--memory-swap')+1], str(runner.early.MEMORY))
        self.assertEqual(cmd.count('--tmpfs'), 1)
        self.assertIn('DOCKER_CONFIG=/work/docker-config', cmd)

    def test_both_daemon_bind_readers_use_exact_owner_without_wider_modes_or_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = f'{os.getuid()}:{os.getgid()}'
            seen = []
            def fake_run(cmd):
                # The demonstrated command without --user fails this regression.
                self.assertIn('--user', cmd)
                self.assertEqual(cmd[cmd.index('--user')+1], identity)
                for flag in ['--read-only', '--pull=never', '--cap-drop=ALL', '--network=none', '--security-opt=no-new-privileges:true']:
                    self.assertIn(flag, cmd)
                self.assertNotIn('--cap-add', cmd)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                marker = root/'bind-probe.txt'
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
                self.assertIn(f'type=bind,src={root},dst=/probe,readonly', cmd)
                image = cmd[cmd.index('--entrypoint=python3')+1]
                seen.append(image)
                return ('3.12.9' if image == runner.PYTHON_IMAGE else '3.11.2')+'\n'+identity+'\n'+marker.read_text().strip()
            with patch.object(runner, 'run', side_effect=fake_run):
                result = runner.daemon_bind_preflight(root, 'synthetic-owner')
            self.assertEqual(seen, [runner.PYTHON_IMAGE, runner.early.IMAGE])
            self.assertEqual(result['user'], identity)
            self.assertFalse((root/'bind-probe.txt').exists())

    def test_fixture_identity_rejects_root_wrong_owner_widened_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(runner.os, 'getuid', return_value=0):
                with self.assertRaisesRegex(AssertionError, 'nonroot'): runner.fixture_identity(root)
            with patch.object(runner.os, 'getuid', return_value=os.getuid()+1):
                with self.assertRaisesRegex(AssertionError, 'owner'): runner.fixture_identity(root)
            root.chmod(0o750)
            with self.assertRaisesRegex(AssertionError, '0700'): runner.fixture_identity(root)
            root.chmod(0o700)
            link = root/'link'; link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(AssertionError, 'real 0700'): runner.fixture_identity(link)

    def test_bind_probe_refuses_wrong_actual_identity_and_cleans_marker_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(runner, 'run', return_value='3.12.9\n0:0\nPUBLIC synthetic manager daemon bind proof'):
                with self.assertRaises(AssertionError): runner.daemon_bind_preflight(root, 'synthetic-owner')
            self.assertFalse((root/'bind-probe.txt').exists())
            with patch.object(runner, 'run', side_effect=RuntimeError('synthetic daemon failure')):
                with self.assertRaises(RuntimeError): runner.daemon_bind_preflight(root, 'synthetic-owner')
            self.assertFalse((root/'bind-probe.txt').exists())

    def test_compose_services_provider_and_never_started_sentinels_share_explicit_identity(self):
        text = (SCRIPTS.parent/'packages/backend/src/modules/docker/__tests__/mandatory-seal.test.ts').read_text()
        self.assertIn('user: identity,', text)
        self.assertIn('user: f.identity,', text)
        self.assertIn("'--user', f.identity", text)
        self.assertIn("'{{.Config.User}}'", text)
        self.assertIn("'home/history.txt'), 'Synthetic continuity only.\\n', { mode: 0o600 }", text)
        self.assertIn("'cat', '/run/provider-key/key'", text)

    def test_no_missing_python313_pull_or_silent_python313_claim(self):
        text = (SCRIPTS/'manager-seal-docker.py').read_text()
        self.assertNotIn("['docker', 'pull'", text)
        self.assertNotIn("['docker', 'build'", text)
        self.assertNotEqual(runner.PYTHON_IMAGE, runner.early.IMAGE)
        self.assertIn("(early.IMAGE, '3.11.2')", text)

    def test_transport_round_trip_exact_bytes(self):
        data = b'bounded synthetic archive\x00'*5000
        h = hashlib.sha256(data).hexdigest()
        text = 'DOCKER_EXPORT_BEGIN='+json.dumps({'bytes': len(data), 'sha256': h})+'\n'
        for i in range(0, len(data), 49152): text += 'DOCKER_EXPORT_DATA='+base64.b64encode(data[i:i+49152]).decode()+'\n'
        text += 'DOCKER_EXPORT_END='+h+'\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); log = root/'wire.log'; log.write_text(text)
            meta = runner.receive(log, root)
            self.assertEqual(meta['sha256'], h)
            self.assertEqual((root/'docker-artifacts.tar.gz').read_bytes(), data)

    def test_transport_truncated_duplicate_corrupt_and_overcap_fail(self):
        h = hashlib.sha256(b'x').hexdigest()
        begin = 'DOCKER_EXPORT_BEGIN='+json.dumps({'bytes': 1, 'sha256': h})+'\n'
        data = 'DOCKER_EXPORT_DATA=eA==\n'
        end = 'DOCKER_EXPORT_END='+h+'\n'
        bad = [begin+data, begin+begin+data+end, begin+'DOCKER_EXPORT_DATA=eQ==\n'+end,
               'DOCKER_EXPORT_BEGIN='+json.dumps({'bytes': runner.ARTIFACT_BYTES+1, 'sha256': h})+'\n']
        for text in bad:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); log = root/'wire.log'; log.write_text(text)
                with self.assertRaises(AssertionError): runner.receive(log, root)

    def test_baseline_requires_exact_original_public_network_failure(self):
        def report(message, name='disabled, removed and malicious optional config'):
            return {'testResults': [{'assertionResults': [{'status': 'failed', 'fullName': name, 'failureMessages': [message]}]}]}
        runner.baseline_failure(report('unexpected synthetic_project_public_app attachment'))
        for wrong in [report('module not found'), report('_public_app', 'other test'), {'testResults': []}]:
            with self.assertRaises(AssertionError): runner.baseline_failure(wrong)

    def test_no_baseline_dependency_reinstall_and_candidate_bundle_before_swap(self):
        text = (SCRIPTS/'manager-seal-docker.py').read_text()
        self.assertEqual(text.count('dependency_step(checkout,'), 1)
        self.assertLess(text.index("step('candidate-backend-bundle'"), text.index('for rel in PRODUCT_DELTA: shutil.copy2(baseline/rel'))
        self.assertIn("rel in permitted or sha(p) == manifest['files'].get(rel)", text)
        self.assertIn("assert all(sha(checkout/rel) == manifest['files'][rel]", text)

    def test_raw_dependency_http_log_never_exported_in_bundle(self):
        text = (SCRIPTS/'manager-seal-docker.py').read_text()
        self.assertIn("None if m.name == 'logs/dependencies.log' else m", text)

    def test_checkpoints_match_every_actual_fixture_assertion_group(self):
        import re
        text = (SCRIPTS.parent/'packages/backend/src/modules/docker/__tests__/mandatory-seal.test.ts').read_text()
        checks = [name for group in re.findall(r'checks\.push\((.*?)\);', text)
                  for name in re.findall(r"'([^']+)'", group)]
        self.assertEqual(set(checks), runner.REQUIRED_CHECKS)
        self.assertEqual(len(checks), len(runner.REQUIRED_CHECKS))
        self.assertIn('factory-registered-restart-synthetic-generation-real-down-up-docker', checks)
        self.assertIn('successive-distinct-revision-refused-anchor-preserved', checks)
        self.assertIn('expectedNetworkIds', text)

    def test_product_security_implementation_unchanged_by_stage(self):
        self.assertEqual(set(runner.PRODUCT_DELTA), {
            'packages/backend/src/modules/docker/docker.service.ts', 'packages/backend/src/modules/docker/wizard-storage.ts'})
        self.assertEqual(runner.RESERVE, 214748364800)
        self.assertEqual(runner.HOST_ALLOWANCE, 1073741824)
        self.assertLess(runner.WIRE_BYTES+runner.ARTIFACT_BYTES+100*1024**2+64*1024**2, runner.HOST_ALLOWANCE)


if __name__ == '__main__': unittest.main()
