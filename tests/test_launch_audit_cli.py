"""Privacy and first-contact regression tests; stdlib only, no live owner state."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / 'wizard-cli' / 'wizard'
loader = importlib.machinery.SourceFileLoader('launch_wizard', str(CLI))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
wizard = importlib.util.module_from_spec(spec)
loader.exec_module(wizard)
CANARIES = ['opaque-secret-9128', 'password=topSecret', 'owner@example.invalid',
            'private.tail1111.ts.net', '/private/owner/home', 'PRIVATE ARTICLE CONTENT',
            'https://user:password@example.invalid/?token=secret', 'owner-host-name']


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'WIZARD_OS_HOME': str(self.root), 'USER': 'reader'}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def test_report_secret_projection(self):
        dirty = ' '.join(CANARIES)
        self.write('release.json', {'release': '0.3.33', 'status': 'pinned', 'private': dirty,
                   'checks': [{'name': dirty, 'expected': dirty, 'observed': dirty, 'ok': False},
                              {'expected': 'a' * 40, 'observed': 'b' * 40, 'ok': False}]})
        self.write('release-pins.json', {'components': {'wizard-ux': {'commit': 'a' * 40, 'token': dirty}},
                                        'images': {'wizard-ux': {'digest': 'sha256:' + 'c' * 64, 'ref': dirty}}, dirty: dirty})
        self.write('updates.json', {'mode': dirty, 'channel': dirty})
        for name in ['api-token', 'apps.json', 'alongside.json']:
            (self.root / name).write_text(dirty)
        with patch.object(wizard, '_audit_run', return_value=dirty + '\nERROR permission denied timeout'):
            value = wizard.cmd_report(argparse.Namespace())
        raw = json.dumps(value)
        for canary in CANARIES:
            self.assertNotIn(canary, raw)
        self.assertEqual(value['release']['mismatches'], 2)
        self.assertEqual(value['release']['identities'][0]['expected'], 'a' * 40)
        self.assertEqual(value['expected_pins']['wizard-ux']['digest'], 'sha256:' + 'c' * 64)
        self.assertEqual(value['updates'], {'mode': 'ask', 'channel': 'stable'})
        self.assertEqual(set(value), {'schema', 'cli_version', 'python_version', 'os', 'release',
                         'expected_pins', 'services', 'recent_errors', 'updates', 'privacy'})
        for row in value['recent_errors'].values():
            self.assertEqual(set(row), {'available', 'classes'})
            self.assertEqual(set(row['classes']), {'timeout', 'connection_refused', 'permission_denied', 'out_of_memory', 'error'})

    def test_report_schema_is_closed_and_validates_real_projection(self):
        schema = json.loads(CLI.with_name('report.schema.json').read_text())
        def validate(node, value):
            if node.get('type') == 'object':
                self.assertIsInstance(value, dict)
                self.assertEqual(set(value), set(node['properties']))
                for key, sub in node['properties'].items():
                    validate(sub, value[key])
            elif node.get('type') == 'array':
                self.assertIsInstance(value, list)
                self.assertLessEqual(len(value), node.get('maxItems', len(value)))
                for row in value:
                    validate(node['items'], row)
            elif node.get('type') == ['string', 'null']:
                self.assertTrue(value is None or isinstance(value, str))
            elif node.get('type') == 'string':
                self.assertIsInstance(value, str)
            elif node.get('type') in ('integer', 'boolean'):
                self.assertIs(type(value), int if node['type'] == 'integer' else bool)
                if 'minimum' in node:
                    self.assertGreaterEqual(value, node['minimum'])
                if 'maximum' in node:
                    self.assertLessEqual(value, node['maximum'])
            if 'enum' in node:
                self.assertIn(value, node['enum'])
            if 'const' in node:
                self.assertEqual(value, node['const'])
        def closed(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                for child in node.values():
                    closed(child)
            elif isinstance(node, list):
                for child in node:
                    closed(child)
        closed(schema)
        with patch.object(wizard, '_audit_run', return_value=None):
            value = wizard.cmd_report(argparse.Namespace())
        validate(schema, value)
        value['private'] = CANARIES[0]
        with self.assertRaises(AssertionError):
            validate(schema, value)

    def test_report_failures_never_emit_exceptions(self):
        for name in ('release.json', 'release-pins.json', 'updates.json'):
            (self.root / name).write_text('MALFORMED ' + CANARIES[0])
        with patch('subprocess.run', side_effect=OSError(CANARIES[0])), patch('socket.socket', side_effect=AssertionError('report opened a socket')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(wizard.main(['report', '--json']), 0)
            value = json.loads(out.getvalue())
        self.assertNotIn(CANARIES[0], out.getvalue())
        self.assertEqual(set(value['services'].values()), {'unavailable'})
        self.assertEqual(value['release']['status'], 'unavailable')

    def test_report_malformed_shapes(self):
        for value in ([], None, 'secret', 18, {'checks': [None, [], 'secret'], 'components': [], 'images': None}):
            for name in ('release.json', 'release-pins.json', 'updates.json'):
                self.write(name, value)
            with patch.object(wizard, '_audit_run', return_value=None):
                self.assertEqual(wizard.cmd_report(argparse.Namespace())['schema'], 'wizard-report-v1')

    @contextlib.contextmanager
    def shell(self, body=b'<title>Wizard OS</title>', redirect=None,
              setup_body=b'{"first_run":true}', setup_status=200, setup_redirect=None):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                setup = self.path == '/wizard/v1/setup'
                location = setup_redirect if setup else redirect
                self.send_response(302 if location else setup_status if setup else 200)
                if location:
                    self.send_header('Location', location)
                self.end_headers()
                self.wfile.write(setup_body if setup else body)
            def log_message(self, format, *args):
                pass
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.write('apps.json', {'slots': {'shell': {'default': 'wizard-ux'}},
                       'apps': [{'id': 'wizard-ux', 'provides': ['shell'], 'view':
                                 {'local': f'http://127.0.0.1:{server.server_port}/wizard-os-home/'}}]})
            try:
                yield server
            finally:
                server.shutdown()
                thread.join()

    def contact(self, **kw):
        return wizard.cmd_contact(argparse.Namespace(registry=None, timeout=1, ssh_host=kw.get('host'), ssh_port=kw.get('port')))

    def test_contact_discovery_nondefault_port(self):
        with self.shell(), patch.dict(os.environ, {'SSH_CONNECTION': '192.0.2.1 55000 192.0.2.2 2222'}):
            value = self.contact()
        self.assertIn('-p 2222', value['command'])
        self.assertIn('-l reader 192.0.2.2', value['command'])
        self.assertIn('127.0.0.1:18795:127.0.0.1:', value['command'])
        self.assertEqual(value['url'], 'http://127.0.0.1:18795/wizard-os-home/')
        self.assertNotIn('StrictHostKeyChecking=no', value['command'])

    def test_contact_sudo_stripped_context_requires_override(self):
        with self.shell(), patch.dict(os.environ, {'USER': 'root', 'SUDO_USER': 'reader'}):
            with self.assertRaises(wizard.Fail) as error:
                self.contact()
            self.assertEqual(error.exception.code, 'contact_ssh_required')
            value = self.contact(host='box.example.invalid', port=2022)
        self.assertIn('-l reader box.example.invalid', value['command'])
        self.assertIn('-p 2022', value['command'])

    def test_contact_ipv6(self):
        with self.shell(), patch.dict(os.environ, {'SSH_CONNECTION': '2001:db8::1 55000 2001:db8::2 22'}):
            self.assertIn('2001:db8::2', self.contact()['command'])

    def test_contact_refuses_wrong_service_redirect_and_injection(self):
        for body, redirect in ((b'not the shell', None), (b'Wizard', 'http://192.0.2.10/')):
            with self.shell(body, redirect):
                with self.assertRaises(wizard.Fail):
                    self.contact(host='box.invalid')
        with self.shell():
            for host in ('-oProxyCommand=anything', 'box;touch file', 'user@host'):
                with self.assertRaises(wizard.Fail):
                    self.contact(host=host)
        self.write('apps.json', {'apps': [{'id': 'x', 'provides': ['shell'], 'view': {'local': 'http://192.0.2.1:8080/'}}]})
        with self.assertRaises(wizard.Fail):
            self.contact(host='box.invalid')

    def test_contact_fresh_loopback_url_without_tailnet_links(self):
        with self.shell():
            reg = json.loads((self.root / 'apps.json').read_text())
            reg['apps'][0]['view']['url'] = reg['apps'][0]['view'].pop('local')
            self.write('apps.json', reg)
            self.assertTrue(self.contact(host='box.invalid')['ok'])
            reg['apps'][0]['view']['url'] = 123
            self.write('apps.json', reg)
            with self.assertRaises(wizard.Fail):
                self.contact(host='box.invalid')

    def test_contact_requires_same_origin_setup_not_just_shell_html(self):
        # 0.3.32's shell proxy works, but its pinned Agent Service lacks Setup.
        for setup in ({'setup_status': 404}, {'setup_status': 503},
                      {'setup_body': b'<title>Wizard</title>'},
                      {'setup_body': b'[]'}, {'setup_body': b'{}'},
                      {'setup_body': b'{"first_run":"true"}'},
                      {'setup_redirect': 'http://192.0.2.10/wizard/v1/setup'},
                      {'setup_redirect': '/wizard-os-home/'}):
            with self.subTest(setup=setup), self.shell(**setup):
                with self.assertRaises(wizard.Fail) as error:
                    self.contact(host='box.invalid')
                self.assertEqual(error.exception.code, 'contact_setup_unavailable')
        with self.shell(setup_body=b'{"first_run":false}') as server:
            value = self.contact(host='box.invalid')
            self.assertTrue(value['ok'])
            self.assertIn(f':127.0.0.1:{server.server_port}', value['command'])
            self.assertEqual(value['setup_api'], 200)

    def test_contact_does_not_create_listener(self):
        with self.shell(), patch('socket.socket.bind', side_effect=AssertionError('new listener')):
            self.assertTrue(self.contact(host='box.invalid')['ok'])


if __name__ == '__main__':
    unittest.main()
