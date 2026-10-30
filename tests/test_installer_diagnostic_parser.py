#!/usr/bin/python3
"""Fresh finite synthetic-only tests; deliberately NOT the retained 105 suite."""

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('finite_installer_parser', ROOT/'qa/privsep-storage/installer_diagnostic_parser.py')
assert SPEC is not None and SPEC.loader is not None
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
VOCAB = P.repertoire({p: (ROOT/p).read_text() for p in P.SOURCE_PATHS})
SENTINELS = ('https://owner.example.ts.net/?token=secret-123', 'tskey-auth-DO-NOT-EXPORT', 'Bearer real-looking-token_abc')


def wrap(projection):
    return {'schema': 'installer-forensic-guest-v1', 'review_required': True,
            'full_acceptance': False, 'diagnosis': projection['diagnosis'],
            'identity': {'pid1': True, 'no_os_daemons': True, 'no_nic': True,
                'maintenance_root_ro_noload': True, 'both_devices_ro': True,
                'mnt_namespace_inode': 5, 'pid_namespace_inode': 6, 'failed_mount_ro_noload': True},
            'projection': projection}


class InstallerProjection(unittest.TestCase):
    def parse(self, text, eof=True):
        out = P.project(text.encode(), VOCAB, eof=eof)
        encoded = json.dumps(out)
        for s in SENTINELS:
            self.assertNotIn(s, encoded)
        P.validate_export(wrap(out), VOCAB)
        return out

    def test_01_actual_source_repertoire(self):
        self.assertEqual(set(VOCAB['source_sha256']), set(P.SOURCE_PATHS))
        self.assertIn('storage mapping mismatch', VOCAB['reasons'])

    def test_02_unknown_url_tokens(self):
        r = self.parse('\n'.join(SENTINELS)+'\n')
        self.assertEqual(r['unknown_count'], 3)
        self.assertEqual(r['diagnosis'], 'unknown')

    def test_03_fixed_phase(self):
        r = self.parse('Private access: none selected; leaving Tailscale unchanged.\n')
        self.assertEqual(r['events'][0]['code'], 'private-access-none')
        self.assertEqual(r['diagnosis'], 'unknown')

    def test_04_refusal_first_not_last(self):
        r = self.parse('maintenance-needed: storage mapping mismatch\nPermissionError: '+SENTINELS[0]+'\n')
        self.assertEqual(r['first_recognized_failure']['code'], 'storage-refusal')
        self.assertEqual(r['first_recognized_failure']['line_number'], 1)

    def test_05_errno_not_payload(self):
        r = self.parse("FileNotFoundError: [Errno 2] No such file: '"+SENTINELS[0]+"'\n")
        self.assertEqual(r['first_recognized_failure']['errno'], 2)

    def test_06_unknown_refusal_not_exported(self):
        r = self.parse('maintenance-needed: '+SENTINELS[1]+'\n')
        self.assertEqual(r['unknown_count'], 1)

    def test_07_frames_known_only(self):
        r = self.parse('  File "/opt/storage-contract-a-source/scripts/prepare-alongside.py", line 291, in main\n  File "'+SENTINELS[0]+'", line 1, in main\n')
        self.assertEqual(len(r['events']), 1)
        self.assertEqual(r['events'][0]['line'], 291)
        self.assertEqual(r['unknown_count'], 1)

    def test_08_frame_forged_function(self):
        r = self.parse('  File "/opt/storage-contract-a-source/scripts/prepare-alongside.py", line 291, in malicious_function\n')
        self.assertEqual(r['unknown_count'], 1)

    def test_09_compose_reason(self):
        r = self.parse('Error response from daemon: permission denied while trying to connect to the Docker daemon socket '+SENTINELS[0]+'\n')
        self.assertEqual(r['first_recognized_failure']['code'], 'docker-socket-permission')

    def test_10_unknown_precedes_failure(self):
        r = self.parse(SENTINELS[2]+'\nPermissionError: '+SENTINELS[0]+'\n')
        self.assertEqual(r['first_recognized_failure']['unknown_lines_before'], 1)

    def test_11_partial_line_is_unknown(self):
        r = self.parse('PermissionError: '+SENTINELS[0], eof=False)
        self.assertIsNone(r['first_recognized_failure'])
        self.assertTrue(r['bounded_incomplete'])

    def test_12_line_cap(self):
        r = self.parse('x\n'*(P.MAX_LINES+1))
        self.assertEqual(r['processed_lines'], P.MAX_LINES)
        self.assertTrue(r['bounded_incomplete'])

    def test_13_events_cap(self):
        r = self.parse('PermissionError: hidden\n'*(P.MAX_EVENTS+1))
        self.assertEqual(len(r['events']), P.MAX_EVENTS)
        self.assertTrue(r['events_capped'])
        self.assertEqual(r['first_recognized_failure']['line_number'], 1)

    def test_14_unknown_hash_sensitive(self):
        a = self.parse(SENTINELS[0]+'\n')
        b = self.parse(SENTINELS[1]+'\n')
        self.assertNotEqual(a['unknown_sha256'], b['unknown_sha256'])

    def test_15_host_gate_forged_fields(self):
        v = wrap(self.parse('x\n'))
        v['secret'] = SENTINELS[0]
        with self.assertRaises(AssertionError):
            P.validate_export(v, VOCAB)

    def test_16_host_gate_forged_enum(self):
        v = wrap(self.parse('PermissionError: hidden\n'))
        v['projection']['events'][0]['code'] = SENTINELS[1]
        with self.assertRaises(AssertionError):
            P.validate_export(v, VOCAB)

    def test_17_missing_source_refused(self):
        sources = {p: (ROOT/p).read_text() for p in P.SOURCE_PATHS}
        del sources[P.SOURCE_PATHS[-1]]
        with self.assertRaises(ValueError):
            P.repertoire(sources)

    def test_18_command_numeric_only(self):
        r = self.parse("subprocess.CalledProcessError: Command ['runuser', '-u', 'wizard-system', 'TOKEN="+SENTINELS[0]+"'] returned non-zero exit status 2.\n"+"CalledProcessError: Command ['forged-secret-command'] returned non-zero exit status 999.\n")
        self.assertEqual(r['first_recognized_failure']['command'], 'runuser')
        self.assertEqual(r['first_recognized_failure']['exit'], 2)
        self.assertNotIn('command', r['events'][1])

    def test_19_source_marker_drift_refused(self):
        sources = {p: (ROOT/p).read_text() for p in P.SOURCE_PATHS}
        sources['scripts/setup-private-access.sh'] = ''
        with self.assertRaises(AssertionError):
            P.repertoire(sources)

    def test_20_binary_longline_bounded(self):
        r = P.project(b'\xff\n'+b'x'*(P.MAX_LINE+1)+b'\n', VOCAB, eof=True)
        self.assertEqual(r['unknown_count'], 2)
        self.assertEqual(r['diagnosis'], 'unknown')
        P.validate_export(wrap(r), VOCAB)

    def test_21_source_inventory_lexical_order(self):
        spec = importlib.util.spec_from_file_location('finite_installer_runner', ROOT/'qa/privsep-storage/installer_diagnostic_run.py')
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory(prefix='finite-inventory-') as name:
            root = Path(name)
            (root/'a').mkdir()
            (root/'a/z').write_text('synthetic')
            (root/'a.txt').write_text('synthetic')
            old_path_order = [str(p.relative_to(root)) for p in sorted(root.rglob('*')) if p.is_file()]
            expected = ['a.txt', 'a/z']
            self.assertNotEqual(old_path_order, expected)
            self.assertEqual([r['path'] for r in runner.inventory(root)], expected)


if __name__ == '__main__':
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(InstallerProjection))
    # No arbitrary failure messages or traceback payloads reach the durable log.
    print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skipped': len(result.skipped)}))
    sys.exit(not result.wasSuccessful())
