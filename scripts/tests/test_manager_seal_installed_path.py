"""Fast offline runner/payload tests. No Docker, Nest or queue acceptance claim."""
import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_installed_path as rig
import manager_seal_installed_recovery as recovery


class PinsAndBounds(unittest.TestCase):
    def test_aggregate_caps_include_control_tools_and_shmem(self):
        self.assertLessEqual(sum(rig.MEMORY.values()), 12*rig.GIB)
        self.assertLessEqual(sum(rig.TMPFS.values()), 8*rig.GIB)
        self.assertEqual(rig.TMPFS['fixture'], 64*rig.MIB)
        self.assertEqual(sum(rig.FOOTPRINT.values()), rig.GIB)
        self.assertGreaterEqual(rig.FOOTPRINT['daemon_overlay_and_metadata_estimate'], 128*rig.MIB)
        self.assertIn('controller', rig.MEMORY)
        self.assertIn('shmem', rig.TMPFS)

    def test_source_manifest_refuses_content_mode_and_source_drift(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp); p = root/'source.py'; p.write_text('synthetic\n'); p.chmod(0o644)
            data = {'source': 'a'*40, 'files': {'source.py': rig.sha(p)}, 'modes': {'source.py': 0o644}}
            (root/'SOURCE.json').write_text(json.dumps(data)); digest = rig.sha(root/'SOURCE.json')
            self.assertEqual(rig.manifest(root, 'a'*40, digest), data)
            p.chmod(0o755)
            with self.assertRaises(AssertionError): rig.manifest(root, 'a'*40, digest)
            p.chmod(0o644); p.write_text('changed')
            with self.assertRaises(AssertionError): rig.manifest(root, 'a'*40, digest)
            with self.assertRaises(AssertionError): rig.manifest(root, 'b'*40, digest)

    def test_real_inherited_build_argv_no_socket_and_lower_shared_limits(self):
        path = Path(rig.__file__).with_name('manager-seal-early.py')
        spec = importlib.util.spec_from_file_location('early_payload_test', path)
        early = importlib.util.module_from_spec(spec); spec.loader.exec_module(early)
        argv = rig.build_command(early, Path('/srv/work/manager-seal-installed-test'), Path('/srv/work/manager-seal-installed-test/installed-results'),
                                 'owner', 1000, 1000, 'a'*40, 'b'*64, 'owned-work', '/var/lib/docker/volumes/owned-fixture/_data')
        self.assertEqual(argv[argv.index('--memory')+1], str(6*rig.GIB))
        self.assertEqual(argv[argv.index('--memory-swap')+1], str(6*rig.GIB))
        self.assertNotIn('--tmpfs', argv)
        self.assertNotIn('docker.sock', ' '.join(argv))
        self.assertIn('type=volume,src=owned-work,dst=/work,volume-nocopy', argv)
        self.assertIn('--pull=never', argv)
        self.assertIn('--inside-build', argv)
        self.assertIn('type=bind,src=/srv/work/manager-seal-installed-test/installed-results/bootstrap-tmp,dst=/tmp,readonly', argv)

    def test_fixture_selector_metadata_literal_identity_and_no_image_tags(self):
        compiled, recipe = rig.fixture('/var/lib/docker/volumes/owned/_data', 'owner', 1000, 1000)
        self.assertEqual(recipe['x-runtipi'], {'schema_version': 2})
        self.assertTrue(recipe['services'][rig.MAIN]['x-runtipi']['is_main'])
        self.assertNotIn('x-runtipi', compiled)
        self.assertEqual(set(compiled['services']), set(rig.SERVICES))
        self.assertEqual(len(compiled['networks']), 4)
        for service in compiled['services'].values():
            self.assertEqual(service['image'], rig.PYTHON)
            self.assertEqual(service['user'], '1000:1000')
            self.assertEqual(service['memswap_limit'], service['mem_limit'])
            self.assertEqual(service['pull_policy'], 'never')
            self.assertFalse(service.get('privileged', False))

    def test_ambiguous_creation_cleanup_only_exact_new_names_metadata_no_recreate(self):
        rows = [{'operation': 'creation-intent', 'kind': kind, 'name': 'owned-new'} for kind in ('container', 'network', 'volume')]
        responses = [types.SimpleNamespace(returncode=0, stdout=v) for v in (b'a'*64+b'\n', b'b'*64+b'\n', b'owned-new\n')]
        with patch.object(rig.subprocess, 'run', side_effect=responses) as run:
            found = rig.reconcile_creation_intents(rows, 'owner')
        self.assertEqual(len(found), 3)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIn('label=wizard.qa.manager-seal=owner', argv)
            self.assertTrue(any(v.startswith('name=^') and v.endswith('owned-new$') for v in argv))
            self.assertNotIn('create', argv); self.assertNotIn('inspect', argv)
        with patch.object(rig.subprocess, 'run') as run:
            with self.assertRaises(AssertionError): rig.reconcile_creation_intents([dict(rows[0], name='/')], 'owner')
            run.assert_not_called()

    def test_http_timeout_is_one_post_no_retry(self):
        r = rig.Rig.__new__(rig.Rig)
        calls = []
        def http(*a): calls.append(a); raise TimeoutError('synthetic ambiguity')
        r.http = http
        with self.assertRaises(TimeoutError): r.transaction('install', {'skipRun': True}, 'stopped')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'POST')


class EventProof(unittest.TestCase):
    def setUp(self):
        self.cid = 'a'*64; self.net = 'b'*64
        self.rows = [{'operation': 'created', 'kind': 'container', 'id': self.cid, 'name': 'qa-'+rig.MAIN},
                     {'operation': 'created', 'kind': 'network', 'id': self.net, 'name': rig.PROJECT+'_'+rig.MAIN+'-seal'}]
        self.events = [{'Type': 'container', 'Action': 'create', 'Actor': {'ID': self.cid}},
                       {'Type': 'network', 'Action': 'connect', 'Actor': {'ID': self.net, 'Attributes': {'container': self.cid}}}]

    def test_unlabeled_network_actor_has_exact_id_coverage(self):
        result = rig.reconcile_runtime_events(self.events, self.rows, {self.cid}, 'qa')
        self.assertEqual(result['connect_pair_coverage'], 1)

    def test_missing_create_or_connect_is_not_a_pass(self):
        for events in (self.events[:1], self.events[1:], []):
            with self.assertRaises(AssertionError): rig.reconcile_runtime_events(events, self.rows, {self.cid}, 'qa')

    def test_transient_unrelated_or_public_attachment_refuses(self):
        for net in ('unknown', 'c'*64):
            events = copy.deepcopy(self.events)
            events.append({'Type': 'network', 'Action': 'connect', 'Actor': {'ID': net, 'Attributes': {'container': self.cid}}})
            with self.assertRaises(AssertionError): rig.reconcile_runtime_events(events, self.rows, {self.cid}, 'qa')


class FakeProcess:
    def __init__(self, payload): self.stdout = io.BytesIO(payload); self.stderr = io.BytesIO(); self.code = 0
    def wait(self, timeout=None): return self.code
    def poll(self): return self.code
    def kill(self): self.code = -9


def archive(name='file', size=7, kind=tarfile.REGTYPE):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tf:
        m = tarfile.TarInfo(name); m.size = size if kind == tarfile.REGTYPE else 0; m.type = kind
        if kind != tarfile.REGTYPE: m.linkname = '/unrelated'
        tf.addfile(m, io.BytesIO(b'example') if m.size else None)
    return out.getvalue()


class ArtifactCopy(unittest.TestCase):
    def test_bounded_single_owned_file_streams_real_bytes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            target = Path(tmp)/'out'
            with patch.object(rig.subprocess, 'Popen', return_value=FakeProcess(archive())) as call:
                rig.copy_owned_file('owned-id', '/work/file', target, 7)
            self.assertEqual(target.read_bytes(), b'example')
            self.assertEqual(call.call_args.args[0], ['docker', 'cp', 'owned-id:/work/file', '-'])

    def test_reject_size_links_traversal_before_host_write(self):
        for payload, cap in [(archive(), 6), (archive('../file'), 7), (archive(kind=tarfile.SYMTYPE), 7), (archive(kind=tarfile.LNKTYPE), 7)]:
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                target = Path(tmp)/'out'
                with patch.object(rig.subprocess, 'Popen', return_value=FakeProcess(payload)):
                    with self.assertRaises(AssertionError): rig.copy_owned_file('owned', '/work/file', target, cap)
                self.assertFalse(target.exists())


class Recovery(unittest.TestCase):
    def test_lowercase_missing_volume_is_exact_daemon_absence(self):
        for message in ('Error response from daemon: get owned-volume: no such volume\n',
                        'Error response from daemon: get owned-volume: No such volume\n'):
            result = types.SimpleNamespace(returncode=1, stdout=b'[]\n', stderr=message.encode())
            self.assertTrue(recovery.exact_missing(result, 'volume', 'owned-volume'))

    def test_missing_does_not_hide_real_errors_or_wrong_object(self):
        good = b'Error response from daemon: get owned-volume: no such volume\n'
        for code, stdout, stderr in [
            (0, b'[]', good), (2, b'[]', good), (1, b'[{"Name":"owned-volume"}]', good),
            (1, b'[]', good.replace(b'owned-volume', b'unrelated-volume')),
            (1, b'[]', good+b'permission denied\n'), (1, b'', b'permission denied: no such volume'),
            (1, b'', b'Cannot connect to the Docker daemon'), (1, b'', b'context not found'),
            (1, b'', b'Error response from daemon: remove owned-volume: volume is in use')]:
            with self.subTest(code=code, stderr=stderr):
                result = types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
                self.assertFalse(recovery.exact_missing(result, 'volume', 'owned-volume'))

    def test_parent_cleanup_lowercase_absence_and_daemon_failures_are_not_conflated(self):
        for missing in (True, False):
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                root = Path(tmp); (root/'installed-results').mkdir()
                (root/'installed-results/controller-ledger.jsonl').write_text(json.dumps({'kind': 'volume', 'id': 'owned-volume'})+'\n')
                def command(argv, **kw):
                    if argv[1:3] == ['volume', 'rm']:
                        return types.SimpleNamespace(returncode=0, stdout=b'owned-volume\n', stderr=b'')
                    self.assertEqual(argv, ['docker', 'volume', 'inspect', 'owned-volume'])
                    error = b'Error response from daemon: get owned-volume: no such volume\n' if missing else b'permission denied\n'
                    return types.SimpleNamespace(returncode=1, stdout=b'[]\n', stderr=error)
                with patch.object(rig.subprocess, 'run', side_effect=command), patch.object(rig.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=rig.capacity.RESERVE)):
                    if missing: rig.emergency_cleanup(root, 'a'*40)
                    else:
                        with self.assertRaises(AssertionError): rig.emergency_cleanup(root, 'a'*40)
                proof = json.loads((root/'parent-cleanup.json').read_text())
                self.assertEqual(proof['parent_cleanup_residual'], [] if missing else [{'kind': 'volume', 'id': 'owned-volume'}])

    def test_container_and_network_missing_require_target_and_kind(self):
        for kind, message in [
            ('container', 'Error: No such object: owned'),
            ('container', 'Error response from daemon: No such container: owned'),
            ('network', 'Error response from daemon: network owned not found'),
            ('network', 'Error: No such network: owned')]:
            r = types.SimpleNamespace(returncode=1, stdout=b'', stderr=message.encode())
            self.assertTrue(recovery.exact_missing(r, kind, 'owned'))
            self.assertFalse(recovery.exact_missing(r, kind, 'other'))
            self.assertFalse(recovery.exact_missing(r, 'volume', 'owned'))

    def test_all_erlang_cli_actions_have_service_user_scheduler_and_rpc_deadline(self):
        for action in ('ping', 'check_running', 'list_queues'):
            argv = recovery.broker_argv('a'*64, '999:999', action)
            self.assertEqual(argv[:7], ['docker', 'exec', '--user', '999:999', '--env', recovery.CLI_ENV, 'a'*64])
            self.assertEqual(argv[8:11], ['--timeout', '10', '-q'])
            self.assertIn('+S 2:2', recovery.CLI_ENV)
            self.assertIn('+SDcpu 1 +SDio 1 +A 4', recovery.CLI_ENV)
            self.assertNotIn('RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS', ' '.join(argv))
        cookie = recovery.broker_argv('a'*64, '999:999', 'cookie_metadata')
        self.assertIn('stat -c', cookie[-1]); self.assertNotIn('cat ', cookie[-1])

    def test_readiness_requires_metadata_ping_and_check_running_not_tcp(self):
        r = rig.Rig.__new__(rig.Rig); r.receipt = {}; r.state = lambda cid: {'Running': True}
        for failed in ('cookie_metadata', 'ping', 'check_running', None):
            calls = []
            def cli(cid, user, action):
                calls.append(action)
                if action == failed: raise RuntimeError('synthetic original exit153')
                return '999 999 600 20' if action == 'cookie_metadata' else ''
            r.broker_cli = cli
            self.assertEqual(r.rabbit_ready('a'*64, '999:999'), failed is None)
            if failed is None: self.assertEqual(calls, ['cookie_metadata', 'ping', 'check_running'])
            else: self.assertEqual(calls[-1], failed)
        r.state = lambda cid: {'Running': False}
        with self.assertRaises(RuntimeError): r.rabbit_ready('a'*64, '999:999')

    def test_cli_failure_preserves_exit_action_and_sanitized_bounded_first_latest(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out = Path(tmp); r.receipt = {}; r.secrets = ['synthetic-pass']
            outcome = {'exit_code': 153, 'termination_reason': None, 'stdout': b'',
                       'stderr': b'Failed to create scheduler thread 98\nErlang cookie hash: dont-export\npassword=synthetic-pass\n'}
            with patch.object(recovery, 'bounded_cli', return_value=outcome):
                for _ in range(100):
                    with self.assertRaises(RuntimeError): r.broker_cli('a'*64, '999:999', 'ping')
            raw = (r.out/'rabbit-cli.sanitized.json').read_text(); saved = json.loads(raw)['ping']
            self.assertEqual(saved['attempts'], 100); self.assertEqual(saved['failures'], 100)
            self.assertEqual(saved['first_failure']['exit_code'], 153)
            self.assertIn('Failed to create scheduler thread', raw)
            self.assertNotIn('dont-export', raw); self.assertNotIn('synthetic-pass', raw)
            self.assertLess(len(raw), 4096)
            for reason in ('timeout', 'output_cap'):
                with patch.object(recovery, 'bounded_cli', return_value=dict(outcome, exit_code=0, termination_reason=reason)):
                    with self.assertRaises(RuntimeError): r.broker_cli('a'*64, '999:999', 'ping')

    def test_bounded_pipe_collector_preserves_both_streams_and_exit(self):
        result = recovery.bounded_cli([sys.executable, '-c', 'import sys; print("synthetic stdout"); print("synthetic stderr", file=sys.stderr); sys.exit(153)'])
        self.assertEqual(result['exit_code'], 153); self.assertIsNone(result['termination_reason'])
        self.assertEqual(result['stdout'], b'synthetic stdout\n'); self.assertEqual(result['stderr'], b'synthetic stderr\n')

    def test_bounded_pipe_collector_rejects_output_overflow_and_timeout(self):
        for program, reason in [('import os; os.write(2, b"x"*65536)', 'output_cap'),
                                ('import time; time.sleep(5)', 'timeout')]:
            result = recovery.bounded_cli([sys.executable, '-c', program], timeout=.15)
            self.assertEqual(result['termination_reason'], reason)
            self.assertLessEqual(len(result['stdout']), recovery.CLI_OUTPUT_CAP)
            self.assertLessEqual(len(result['stderr']), recovery.CLI_OUTPUT_CAP)

    def test_reuse_compares_complete_non_qa_inventory_bytes_and_modes(self):
        before = {'source': recovery.PRIOR_SOURCE, 'files': {'packages/backend/build.ts': 'a'*64},
                  'modes': {'packages/backend/build.ts': 0o644}}
        raw = json.dumps(before).encode()
        with patch.object(recovery, 'PRIOR_MANIFEST_SHA', recovery.digest(raw)):
            after = copy.deepcopy(before); after['source'] = 'b'*40
            self.assertFalse(recovery.verify_reuse(raw, after)['backend_rebuilt'])
            for field, value in [('files', 'b'*64), ('modes', 0o755)]:
                changed = copy.deepcopy(after); changed[field]['packages/backend/build.ts'] = value
                with self.assertRaises(AssertionError): recovery.verify_reuse(raw, changed)
            for action in ('add', 'delete'):
                changed = copy.deepcopy(after)
                if action == 'add': changed['files']['packages/backend/extra.ts'] = 'c'*64
                else: del changed['files']['packages/backend/build.ts']
                with self.assertRaises(AssertionError): recovery.verify_reuse(raw, changed)

    def test_bundle_restore_only_exact_pinned_four_files_and_rejects_hostile_members(self):
        names = ['main.js', 'main.js.map', 'cpufeatures-e8x7s2f1.node', 'sshcrypto-a3khcfhj.node']
        meta = {'source': recovery.PRIOR_SOURCE, 'built': True,
                'bundle': {'files': {n: {'bytes': 7, 'sha256': recovery.digest(b'example')} for n in names}}}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            for hostile in (None, '../escape', 'link', 'duplicate', 'missing'):
                payload = io.BytesIO()
                with tarfile.open(fileobj=payload, mode='w:gz') as tf:
                    data = json.dumps(meta).encode(); member = tarfile.TarInfo('build-result.json'); member.size = len(data)
                    tf.addfile(member, io.BytesIO(data))
                    for n in names[:-1] if hostile == 'missing' else names:
                        member = tarfile.TarInfo('source/packages/backend/dist/'+n); member.size = 7
                        tf.addfile(member, io.BytesIO(b'example'))
                    if hostile in ('../escape', 'link', 'duplicate'):
                        member = tarfile.TarInfo('build-result.json' if hostile == 'duplicate' else hostile)
                        if hostile == 'link': member.type = tarfile.SYMTYPE; member.linkname = '/unrelated'
                        tf.addfile(member)
                p = root/'archive.tar.gz'; p.write_bytes(payload.getvalue()); dist = root/str(hostile)
                with patch.object(recovery, 'PRIOR_ARCHIVE_SHA', recovery.digest(payload.getvalue())):
                    if hostile is None:
                        self.assertEqual(recovery.restore_bundle(p, dist), meta)
                        self.assertEqual({n.name for n in dist.iterdir()}, set(names))
                    else:
                        with self.assertRaises((AssertionError, KeyError)): recovery.restore_bundle(p, dist)
                        self.assertFalse(dist.exists())
                with patch.object(recovery, 'PRIOR_ARCHIVE_SHA', '0'*64):
                    with self.assertRaises(AssertionError): recovery.restore_bundle(p, root/'wrong-hash')


if __name__ == '__main__': unittest.main()
