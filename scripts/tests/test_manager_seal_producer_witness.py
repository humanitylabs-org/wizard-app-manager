"""Witness harness unit/protocol tests, explicitly NOT actual Compose proof."""
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_producer_witness as witness
import manager_seal_forward_guard as guard
import manager_seal_installed_path as rig


class Witness(unittest.TestCase):
    def test_wrong_pinned_binary_refuses_before_process_or_server(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp); binary = root/'compose'; binary.write_bytes(b'not Compose')
            with patch.object(witness.subprocess, 'Popen') as run, patch.object(witness, 'Peer') as peer:
                with self.assertRaisesRegex(AssertionError, 'digest mismatch'): witness.witness(binary, root/'output.json')
                run.assert_not_called(); peer.assert_not_called(); self.assertFalse((root/'output.json').exists())

    def test_scripted_peer_real_unix_forward_counts_exact_bytes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp); peer = witness.Peer(root)
            cfg = {'owner': 'qa', 'project': 'qa', 'network_names': ['qa-net'], 'networks': {}, 'containers': {}, 'images': [rig.PYTHON], 'binds': {}}
            rows = []; policy = guard.Policy(cfg, rows.append); policy.activated = True
            server = guard.Server(str(root/'guard.sock'), str(root/'peer.sock'), policy)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True); thread.start()
            def request(method, path, data=None):
                conn = guard.UnixHTTP(str(root/'guard.sock'))
                conn.request(method, path, data); response = conn.getresponse(); payload = response.read(); conn.close()
                return response.status, json.loads(payload)
            try:
                self.assertEqual(request('GET', '/networks/qa-net')[0], 404); self.assertEqual(peer.calls, [])
                body = json.dumps({'Name': 'qa-net', 'Driver': 'bridge', 'Internal': True, 'Labels': {'wizard.qa.manager-seal': 'qa'}})
                status, reply = request('POST', '/networks/create', body)
                self.assertEqual(status, 201)
                self.assertEqual(peer.calls[-1]['body_utf8'], body)
                self.assertEqual(peer.calls[-1]['body_sha256'], hashlib.sha256(body.encode()).hexdigest())
                self.assertEqual(request('GET', '/networks/qa-net')[1]['Id'], reply['Id'])
                count = len(peer.calls); self.assertEqual(request('GET', '/networks/unrelated')[0], 403)
                self.assertEqual(count, len(peer.calls))
                self.assertEqual(rig.reconcile_guard_accounting(rows, 1, ['qa-net'])['network_discovery_misses'], 1)
                with self.assertRaises(AssertionError): peer.respond('POST', '/images/create', {}, {})
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2); peer.close()

    def test_readiness_phase_is_before_build_infra_or_manager_and_not_installed_pass(self):
        source = Path(rig.__file__).read_text(); tree = ast.parse(source)
        run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'run' and any(isinstance(x, ast.Attribute) and x.attr == 'producer_readiness_only' for x in ast.walk(n)))
        text = ast.get_source_segment(source, run)
        self.assertLess(text.index("'producer-protocol-prebootstrap'"), text.index("'new-backend-bundle-build'"))
        self.assertLess(text.index('if self.args.producer_readiness_only:'), text.index("self.owner+'-infra'"))
        self.assertLess(text.index('if self.args.producer_readiness_only:'), text.index("self.owner+'-manager'"))
        block = next(n for n in ast.walk(run) if isinstance(n, ast.If) and isinstance(n.test, ast.Attribute) and n.test.attr == 'producer_readiness_only')
        self.assertIsInstance(block.body[-1], ast.Return)
        self.assertNotIn('first_installed_synthetic_path_pass', ast.get_source_segment(source, block))
        witness_source = Path(witness.__file__).read_text()
        self.assertNotIn('/var/run/docker.sock', witness_source)
        self.assertIn('MAX_OUTPUT = 64*1024', witness_source)
        self.assertIn("'DOCKER_HOST': 'unix://'+str(root/'guard.sock')", witness_source)

    def test_denied_incoming_bytes_headers_and_stderr_saved_before_failure(self):
        # Inject only the producer launcher, not the guard/peer/Unix transport.
        # This is a witness failure-harness test, never actual Compose proof.
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp); binary = root/'not-executed'; binary.write_bytes(b'synthetic')
            output = root/'producer-witness.json'
            data = b'{"Scope":"swarm","Name":"unrelated","Unknown":null}\n'
            def producer(command, env, cwd):
                connection = guard.UnixHTTP(env['DOCKER_HOST'].removeprefix('unix://'))
                try:
                    connection.request('POST', '/v1.51/networks/create', data,
                                       headers={'X-Synthetic-Only': 'header-sentinel'})
                    response = connection.getresponse(); response.read()
                    self.assertEqual(response.status, 403)
                finally: connection.close()
                return {'exit_code': 1, 'timed_out': False, 'output_overflow': False,
                        'streams': {'stdout': {'data': b'', 'observed_bytes': 0},
                                    'stderr': {'data': b'synthetic-refusal\xff\n', 'observed_bytes': len(b'synthetic-refusal\xff\n')}}}
            actual_sha = rig.sha
            with patch.object(rig, 'sha', side_effect=lambda p: rig.COMPOSE_SHA if p == binary else actual_sha(p)), patch.object(witness, 'run_bounded', side_effect=producer):
                with self.assertRaisesRegex(AssertionError, 'Compose protocol create failed'):
                    witness.witness(binary, output)
            doc = json.loads(output.read_bytes()); self.assertIs(doc['protocol_pass'], False)
            self.assertIs(doc['installed_proof'], False); self.assertEqual(doc['upstream_trace'], [])
            row = doc['incoming_trace'][0]
            self.assertEqual(base64.b64decode(row['body_base64']), data)
            self.assertEqual(row['body_sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(row['authorization'], 'refused'); self.assertTrue(row['refusal_frames'])
            self.assertIn(['X-Synthetic-Only', 'header-sentinel'], row['headers'])
            self.assertEqual(row['http_version'], 'HTTP/1.1')
            persisted = json.loads((root/'producer-incoming.jsonl').read_bytes())
            self.assertEqual(base64.b64decode(persisted['body_base64']), data)
            self.assertEqual((root/'producer-stderr.bin').read_bytes(), b'synthetic-refusal\xff\n')
            self.assertEqual(doc['producer_streams']['stderr']['sha256'], hashlib.sha256(b'synthetic-refusal\xff\n').hexdigest())
            self.assertEqual(doc['guard_ledger'][-1]['operation'], 'deny')
            self.assertIs(doc['incoming_trace_file']['complete'], True)
            self.assertEqual(doc['incoming_trace_file']['sha256'], actual_sha(root/'producer-incoming.jsonl'))

    def test_trace_cap_fails_closed_no_upstream_and_general_guard_uninstrumented(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            path = Path(tmp)/'trace.jsonl'
            with path.open('x') as stream:
                policy = witness.SyntheticPolicy({}, lambda row: None, [], stream)
                with patch.object(witness, 'MAX_REQUESTS', 1):
                    policy.authorize('HEAD', '/_ping')
                    with self.assertRaises(guard.Denied): policy.authorize('HEAD', '/_ping')
                self.assertEqual(policy.trace_error, 'incoming_trace_cap')
                self.assertEqual(len(policy.incoming), 1)
                with patch.object(witness, 'MAX_TRACE', 1):
                    other = witness.SyntheticPolicy({}, lambda row: None, [], stream)
                    with self.assertRaises(guard.Denied): other.authorize('HEAD', '/_ping')
                    self.assertEqual(other.trace_error, 'incoming_trace_cap')
                with patch.object(stream, 'write', side_effect=OSError('synthetic disk failure')):
                    other = witness.SyntheticPolicy({}, lambda row: None, [], stream)
                    with self.assertRaises(guard.Denied): other.authorize('HEAD', '/_ping')
                    self.assertEqual(other.trace_error, 'incoming_trace_write')
            self.assertFalse(hasattr(guard.Policy({}), 'incoming'))
            self.assertNotIn('body_base64', Path(guard.__file__).read_text())

    def test_bounded_launcher_retains_separate_exact_streams_timeout_and_overflow(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            env = {'PATH': '/usr/bin:/bin'}
            result = witness.run_bounded([sys.executable, '-c', "import os;os.write(1,b'out');os.write(2,b'err\\xff')"], env, tmp)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['streams']['stdout']['data'], b'out')
            self.assertEqual(result['streams']['stderr']['data'], b'err\xff')
            self.assertFalse(result['output_overflow'])
            result = witness.run_bounded([sys.executable, '-c', "import os;os.write(2,b'x'*200000)"], env, tmp)
            self.assertTrue(result['output_overflow'])
            self.assertEqual(len(result['streams']['stderr']['data']), witness.MAX_OUTPUT)
            result = witness.run_bounded([sys.executable, '-c', 'import time;time.sleep(5)'], env, tmp, timeout=.1)
            self.assertTrue(result['timed_out']); self.assertNotEqual(result['exit_code'], 0)


if __name__ == '__main__': unittest.main()
