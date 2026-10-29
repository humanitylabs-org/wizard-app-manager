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


if __name__ == '__main__': unittest.main()
