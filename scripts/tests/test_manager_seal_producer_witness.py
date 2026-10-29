"""Witness harness unit/protocol tests, explicitly NOT actual Compose proof."""
import ast
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
            with patch.object(witness.subprocess, 'run') as run, patch.object(witness, 'Peer') as peer:
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
        self.assertNotIn('subprocess.Popen', witness_source)
        self.assertIn("'DOCKER_HOST': 'unix://'+str(root/'guard.sock')", witness_source)


if __name__ == '__main__': unittest.main()
