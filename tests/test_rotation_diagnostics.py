"""Metadata-only reporting regressions; no Docker, VM, network or credentials."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qa/privsep-security'))
import rotation_diagnostics as d
from wam.credential_rotation import KEYS, parse_env

SECRET = 'sentinel_NEVER_emit_nested_secret'


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.diag = d.Diagnostics(self.stream)

    def test_nested_errors_notes_args_and_attributes_are_absent(self):
        try:
            try:
                raise subprocess.CalledProcessError(7, [SECRET], output=SECRET, stderr=SECRET)
            except Exception as cause:
                error = RuntimeError({'secret': SECRET})
                error.add_note(SECRET)
                raise error from cause
        except Exception as error:
            self.diag.failure(error)
        text = self.stream.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertIn('CalledProcessError', text)
        self.assertIn('RuntimeError', text)
        self.assertNotIn('test_rotation_diagnostics.py', text)  # unbound external frame

    def test_exception_group_and_dynamic_class_names_are_not_leaked(self):
        error = type(SECRET, (RuntimeError,), {})(SECRET)
        group = ExceptionGroup(SECRET, [ValueError(SECRET), error])
        self.assertNotIn(SECRET, json.dumps(d.exception_metadata(group)))

    def test_forged_traceback_names_and_filename_are_not_leaked(self):
        try:
            exec(compile('raise ValueError("' + SECRET + '")', '/' + SECRET, 'exec'))
        except Exception as error:
            self.assertNotIn(SECRET, json.dumps(d.exception_metadata(error)))

    def test_command_failure_reports_operation_rc_not_argv_or_output(self):
        result = subprocess.CompletedProcess([SECRET], 17, SECRET.encode(), SECRET.encode())
        with patch.object(d.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                self.diag.run(['docker', 'exec', SECRET, 'node', '-e', SECRET], data=SECRET.encode())
        text = self.stream.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertIn('http-request', text)
        self.assertIn('17', text)

    def test_timeout_reports_metadata_only(self):
        with patch.object(d.subprocess, 'run', side_effect=subprocess.TimeoutExpired([SECRET], 9, output=SECRET)):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.diag.run([SECRET])
        self.diag.failure(caught.exception)
        self.assertNotIn(SECRET, self.stream.getvalue())
        self.assertIn('unavailable-or-timeout', self.stream.getvalue())

    def test_container_state_drops_env_error_and_health_log_payloads(self):
        class Fake:
            compose = ['docker', 'compose']
            def command(self, args):
                if 'ps' in args: return subprocess.CompletedProcess(args, 0, b'a' * 64)
                if 'logs' in args: return subprocess.CompletedProcess(args, 0, SECRET.encode())
                assert '--format' in args and '{{json .State}}' in args
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'Status': 'exited', 'ExitCode': 7, 'Running': False, 'OOMKilled': True,
                    'Error': SECRET, 'Config': {'Env': [SECRET]},
                    'Health': {'Status': 'unhealthy', 'Log': [{'Output': SECRET}]}}).encode())
        d.container_states(Fake(), self.diag)
        rows = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual(len([r for r in rows if r['event'] == 'container-state']), 3)
        self.assertNotIn(SECRET, self.stream.getvalue())
        self.assertEqual(rows[0]['state']['ExitCode'], 7)
        self.assertTrue(rows[0]['state']['OOMKilled'])

    def test_cleanup_cannot_replace_first_exception(self):
        class Fake:
            compose = ['docker', 'compose']
            def command(self, args): raise OSError(SECRET)
        primary = ValueError(SECRET)
        self.diag.failure(primary)
        d.teardown(Fake(), self.diag, primary)
        rows = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual(rows[0]['event'], 'failure')
        self.assertEqual(rows[0]['error']['class'], 'ValueError')
        self.assertEqual(self.diag.first_failure['error']['class'], 'ValueError')
        self.assertNotIn(SECRET, self.stream.getvalue())
        with self.assertRaises(OSError): d.teardown(Fake(), self.diag)

    def test_queue_cookie_probe_uses_service_user_and_metadata_only(self):
        class Fake:
            compose = ['docker', 'compose']
            def command(self, args):
                if 'ps' in args: return subprocess.CompletedProcess(args, 0, b'a' * 64)
                if 'inspect' in args:
                    return subprocess.CompletedProcess(args, 0, b'{"Running":true,"Status":"running"}')
                if 'logs' in args: return subprocess.CompletedProcess(args, 0, SECRET.encode())
                assert args[args.index('--user') + 1] == 'rabbitmq'
                assert 'stat -c' in args[-1] and 'cat ' not in args[-1]
                return subprocess.CompletedProcess(args, 0, b'999\n999\n999 999 600 20\n')
        d.container_states(Fake(), self.diag)
        rows = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        cookie = [r for r in rows if r['event'] == 'queue-cookie-metadata']
        self.assertEqual(len(cookie), 1)
        self.assertTrue(cookie[0]['service_owned'])
        self.assertTrue(cookie[0]['private_mode'])

    def test_all_six_credentials_still_checked_and_nonsecret_env_not_parsed(self):
        old = {key: 'before_' + key for key in KEYS}
        new = {key: 'after_' + key for key in KEYS}
        env = ''.join(key + '=' + value + '\n' for key, value in new.items()) + 'TZ=UTC\nDEMO_MODE=false\n'
        self.assertEqual(parse_env(env.encode()), new)
        d.assert_no_credentials(b'UTC false', old, new)
        for values in (old, new):
            for secret in values.values():
                with self.assertRaises(AssertionError): d.assert_no_credentials(secret.encode(), old, new)

    def test_fixture_has_production_cache_and_protected_ancestors(self):
        spec = importlib.util.spec_from_file_location('disposable', ROOT / 'qa/privsep-security/credential-rotation-disposable.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Source contract without chown/filesystem fixture execution on control host.
        text = Path(module.__file__).read_text()
        self.assertIn("str(root / 'cache') + ':/cache'", text)
        self.assertIn('BASE.mkdir(mode=0o755)', text)
        self.assertIn('folder.mkdir(mode=0o755)', text)
        self.assertIn('control.mkdir(mode=0o700)', text)
        self.assertEqual(len(module.CASES), 7)
        self.assertIn("full_acceptance=False", text)
        self.assertIn("CASES[:1] if args.diagnostic_first_case else CASES", text)

    def test_exact_public_image_contract_requires_known_privilege_drop(self):
        passwd = 'rabbitmq:x:999:998:RabbitMQ:/var/lib/rabbitmq:/bin/sh\n'
        for launcher in ('su-exec', 'gosu'):
            contract = d.queue_health_contract('', ['docker-entrypoint.sh'], passwd,
                                               'exec ' + launcher + ' rabbitmq "$BASH_SOURCE" "$@"')
            test = contract['health_test'][1]
            self.assertTrue(contract['default_root'])
            self.assertIn('exec ' + launcher + ' rabbitmq rabbitmq-diagnostics -q ping', test)
            self.assertIn('999:998:400|999:998:600', test)
            self.assertIn('$$cookie', test)  # Compose must not consume shell variables.
            self.assertIn('$$(stat', test)
            self.assertNotIn('cat ', test)
            subprocess.run(['sh', '-n', '-c', test.replace('$$', '$')], check=True)
        with self.assertRaises(AssertionError):
            d.queue_health_contract('', ['docker-entrypoint.sh'], passwd, 'unknown wrapper')

    def test_rootless_queue_contract_and_malformed_uid_refuse(self):
        passwd = 'rabbitmq:x:999:998:RabbitMQ:/var/lib/rabbitmq:/bin/sh\n'
        contract = d.queue_health_contract('rabbitmq', ['docker-entrypoint.sh'], passwd, '')
        self.assertFalse(contract['default_root'])
        self.assertIn('exec rabbitmq-diagnostics -q ping', contract['health_test'][1])
        with self.assertRaises(AssertionError):
            d.queue_health_contract('unknown', ['docker-entrypoint.sh'], passwd, '')
        with self.assertRaises(AssertionError):
            d.queue_health_contract('rabbitmq', ['docker-entrypoint.sh'], passwd.replace(':999:', ':0:'), '')


if __name__ == '__main__':
    unittest.main()
