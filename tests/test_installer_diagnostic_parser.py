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
# Explicit finite realistic fixtures, not samples of the private guest log.
REFINEMENT_CASES = (
    ('bind-missing', 'Error response from daemon: invalid mount config for type "bind": bind source path does not exist: SECRET', 'docker-bind-source-missing'),
    ('bind-invalid', 'docker: Error response from daemon: invalid bind mount spec "SECRET": invalid mode', 'docker-bind-spec-invalid'),
    ('mount-config', 'Error response from daemon: invalid mount config for type "bind": SECRET', 'docker-mount-config-invalid'),
    ('mount-path', 'Error response from daemon: invalid mount path: SECRET mount path must be absolute', 'docker-mount-path-invalid'),
    ('volume-spec', 'Error response from daemon: invalid volume specification: SECRET', 'docker-volume-spec-invalid'),
    ('propagation-shared', 'Error response from daemon: path SECRET is mounted on / but it is not a shared mount', 'docker-mount-not-shared'),
    ('propagation-slave', 'Error response from daemon: path SECRET is not a shared or slave mount', 'docker-mount-not-shared-or-slave'),
    ('propagation-invalid', 'Error response from daemon: invalid propagation mode: SECRET', 'docker-propagation-invalid'),
    ('mount-source-ro', 'Error response from daemon: error while creating mount source path SECRET: mkdir SECRET: read-only file system', 'docker-mount-source-create-failed'),
    ('mount-runtime', 'Error response from daemon: failed to create shim task: OCI runtime create failed: error mounting SECRET: not a directory', 'docker-mount-failed'),
    ('mount-denied', 'Error response from daemon: Mounts denied: SECRET', 'docker-mount-denied'),
    ('permission', 'Error: failed to start container SECRET: permission denied', 'docker-permission-denied'),
    ('readonly', 'ERROR: failed to start container SECRET: read-only file system', 'docker-filesystem-readonly'),
    ('missing-file', 'error: failed to start container SECRET: no such file or directory', 'docker-no-such-file'),
    ('unhealthy', 'dependency failed to start: container SECRET is unhealthy', 'compose-dependency-start-failed'),
    ('container-unhealthy', ' Container SECRET Error container SECRET is unhealthy', 'compose-unhealthy'),
    ('connect-refused', 'error during connect: Get "SECRET": dial unix SECRET: connect: connection refused', 'docker-connection-refused'),
    ('oom', 'Error response from daemon: SECRET: out of memory', 'docker-memory-exhausted'),
    ('shell-install-cd', '/var/lib/wizard-os/wizard-app-manager/install.sh: line 88: cd: SECRET: No such file or directory', 'shell-no-such-file'),
    ('shell-mkdir', 'mkdir: cannot create directory SECRET: Permission denied', 'shell-permission-denied'),
    ('shell-cp', 'cp: cannot create regular file SECRET: Read-only file system', 'shell-filesystem-readonly'),
    ('shell-command', 'bash: line 88: SECRET: command not found', 'shell-command-not-found'),
    ('shell-errno', 'mount: SECRET: [Errno 13] SECRET', 'shell-errno'),
    ('shell-exit', 'bash: SECRET: exit status 126', 'shell-command-exit'),
    ('compose-exit', 'service "SECRET" didn\'t complete successfully: exit 1', 'compose-service-exit'),
    ('release-render', 'invalid release pins: SECRET', 'release-pins-invalid'),
)


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


    def test_22_realistic_refinement_inventory(self):
        for name, text, expected in REFINEMENT_CASES:
            for secret in SENTINELS:
                with self.subTest(case=name, secret_id=SENTINELS.index(secret)):
                    r = self.parse(text.replace('SECRET', secret)+'\n')
                    self.assertEqual(r['first_recognized_failure']['code'], expected)
                    self.assertEqual(r['unknown_count'], 0)

    def test_23_complete_docker_phrase_inventory(self):
        for phrase, expected in P.DOCKER_REASONS.items():
            with self.subTest(code=expected):
                r = self.parse('Error response from daemon: '+phrase+' '+SENTINELS[0]+'\n')
                self.assertEqual(r['first_recognized_failure']['code'], expected)
                self.assertIn(expected, r['first_recognized_failure']['matched_phrase_ids'])

    def test_24_complete_shell_reason_inventory(self):
        for phrase, expected in P.SHELL_REASONS.items():
            with self.subTest(code=expected):
                r = self.parse('bash: '+SENTINELS[0]+': '+phrase+'\n')
                self.assertEqual(r['first_recognized_failure']['code'], expected)

    def test_25_numeric_bounds(self):
        for form, key, maximum in (('bash: payload: exit code {}', 'exit', 255),
                                   ('service "payload" didn\'t complete successfully: exit {}', 'exit', 255),
                                   ('mount: payload: [Errno {}] private', 'errno', 4095)):
            for n in (1, maximum):
                self.assertEqual(self.parse(form.format(n)+'\n')['first_recognized_failure'][key], n)
            for n in (0, maximum+1, 99999, -1):
                self.assertIsNone(self.parse(form.format(n)+'\n')['first_recognized_failure'])

    def test_26_ansi_realistic_error_inventory(self):
        for name, text, expected in REFINEMENT_CASES:
            with self.subTest(case=name):
                # Embedded SGR, outer colour and erase-line; secrets in messages.
                text = text.replace('SECRET', '\x1b[1m'+SENTINELS[0]+'\x1b[22m')
                r = self.parse('\x1b[2K\x1b[31m'+text+'\x1b[0m\n')
                self.assertEqual(r['first_recognized_failure']['code'], expected)

    def test_27_unknown_control_inventory(self):
        failure = 'Error: permission denied '+SENTINELS[0]
        controls = ('\x1b]8;;'+SENTINELS[0]+'\x07', '\x1b[2A', '\x1b[999999999999999999999999999999999m',
                    '\x1b[31m'*(P.MAX_CSI+1), '\x00', '\x08', '\t', '\x9b31m')
        for control in controls:
            with self.subTest(control_id=controls.index(control)):
                r = self.parse(control+failure+'\n')
                self.assertEqual(r['unknown_count'], 1)
                self.assertIsNone(r['first_recognized_failure'])

    def test_28_progress_cr_preserves_first(self):
        r = self.parse(SENTINELS[0]+'\r\x1b[2KError: permission denied\rError: no such file or directory\r\n')
        self.assertEqual(r['processed_lines'], 3)
        self.assertEqual(r['unknown_count'], 1)
        self.assertEqual(r['first_recognized_failure']['code'], 'docker-permission-denied')
        self.assertEqual(r['first_recognized_failure']['line_number'], 2)
        self.assertEqual(r['first_recognized_failure']['unknown_lines_before'], 1)

    def test_29_no_marker_assembly_or_approval(self):
        marker = 'Private access: none selected; leaving Tailscale unchanged.'
        for text in (marker[:8]+'\x1b[0m'+marker[8:], '\x1b[2K'+marker,
                     'UNTRUSTED\x1b[2K'+marker, 'full_acceptance=true\rAPPROVED'):
            r = self.parse(text+'\n')
            self.assertEqual(r['events'], [])
            self.assertEqual(r['diagnosis'], 'unknown')
        r = self.parse('\x1b[32m'+marker+'\x1b[0m\n')
        self.assertEqual(r['events'][0]['code'], 'private-access-none')
        self.assertFalse(r['full_acceptance'])

    def test_30_frame_line_source_bounds(self):
        for n in (0, VOCAB['source_line_counts']['scripts/prepare-alongside.py']+1):
            r = self.parse('  File "/opt/storage-contract-a-source/scripts/prepare-alongside.py", line '+str(n)+', in main\n')
            self.assertEqual(r['unknown_count'], 1)

    def test_31_all_static_markers(self):
        for text, (kind, code) in P.FIXED.items():
            with self.subTest(code=code):
                r = self.parse(text+'\n')
                self.assertEqual(r['events'][0]['kind'], kind)
                self.assertEqual(r['events'][0]['code'], code)
                self.assertFalse(r['full_acceptance'])

    def test_32_unexpected_nested_export_fields(self):
        for target in ('projection', 'identity', 'event', 'first'):
            v = wrap(self.parse('Error: permission denied\n'))
            row = {'projection': v['projection'], 'identity': v['identity'],
                   'event': v['projection']['events'][0], 'first': v['projection']['first_recognized_failure']}[target]
            row['\x1b[31msecret'] = SENTINELS[0]
            with self.assertRaises(AssertionError):
                P.validate_export(v, VOCAB)
        for payload in (SENTINELS[0], '\x1b[31mdocker-permission-denied', {'secret': SENTINELS[1]}):
            v = wrap(self.parse('Error: permission denied\n'))
            v['projection']['events'][0]['matched_phrase_ids'] = [payload]
            with self.assertRaises(AssertionError):
                P.validate_export(v, VOCAB)

    def test_33_unknown_error_and_suffix_controls(self):
        for text in ('Error: '+SENTINELS[0], 'mount: '+SENTINELS[1],
                     'user text says permission denied '+SENTINELS[0],
                     'bash: '+SENTINELS[0]+': Permission denied but success',
                     'Error: permission denied\x1b]0;'+SENTINELS[0]+'\x07',
                     'APPROVED '+SENTINELS[2]):
            r = self.parse(text+'\n')
            self.assertEqual(r['diagnosis'], 'unknown')
            self.assertEqual(r['unknown_count'], 1)

    def test_34_refined_marker_source_drift(self):
        sources = {p: (ROOT/p).read_text() for p in P.SOURCE_PATHS}
        sources['scripts/setup-private-access.sh'] = sources['scripts/setup-private-access.sh'].replace('Tailscale sign-in link:', '')
        with self.assertRaises(AssertionError):
            P.repertoire(sources)

    def test_35_multimatch_ids_not_payload(self):
        r = self.parse('Error: invalid mount config for type "bind": bind source path does not exist: '+SENTINELS[0]+'\n')
        self.assertEqual(r['first_recognized_failure']['matched_phrase_ids'],
                         ['docker-bind-source-missing', 'docker-mount-config-invalid'])
        v = wrap(r)
        v['projection']['events'][0]['kind'] = 'marker'
        with self.assertRaises(AssertionError):
            P.validate_export(v, VOCAB)


if __name__ == '__main__':
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(InstallerProjection))
    # No arbitrary failure messages or traceback payloads reach the durable log.
    print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skipped': len(result.skipped)}))
    sys.exit(not result.wasSuccessful())
