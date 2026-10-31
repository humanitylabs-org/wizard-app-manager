"""Local protocol tests only. The counting upstream is NOT installed Docker QA."""
import copy
import http.server
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_forward_guard as guard

OWN = 'a'*64
OTHER = 'b'*64
NET = 'c'*64
IMAGE = 'sha256:'+'d'*64
NEW = 'e'*64


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def do_GET(self): self.call()
    def do_POST(self): self.call()
    def do_DELETE(self): self.call()
    def call(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.command, self.path, data))
        path = self.path.split('?')[0]
        if path == '/events' and hasattr(self.server, 'event_queue'):
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Transfer-Encoding', 'chunked'); self.end_headers()
            events = self.server.event_queue
            try:
                while True:
                    block = events.get(timeout=5)
                    if block is None:
                        self.wfile.write(b'0\r\n\r\n'); self.wfile.flush(); return
                    self.wfile.write(f'{len(block):x}\r\n'.encode()+block+b'\r\n'); self.wfile.flush()
            except (OSError, __import__('queue').Empty): return
        value = self.server.replies.get(path, {'real_upstream_marker': True})
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(200 if not path.endswith('/create') else 201)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close'); self.end_headers(); self.wfile.write(payload)


class FakeUnix(guard.Server):
    def __init__(self, path):
        self.calls = []; self.replies = {}
        self.slots = threading.BoundedSemaphore(16)
        super(guard.Server, self).__init__(path, Upstream)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.root = Path(self.tmp.name)
        self.config = {'owner': 'qa', 'project': 'qa_synthetic', 'services': ['main'], 'user': '1000:1000',
                       'containers': {OWN: 'owned'}, 'networks': {NET: 'owned-net'},
                       'images': [IMAGE], 'container_names': ['qa-main'], 'network_names': ['qa-net'],
                       'binds': {}}
        self.rows = []; self.policy = guard.Policy(self.config, self.rows.append)
        self.policy.activated = True
        self.raw = FakeUnix(str(self.root/'raw.sock'))
        self.proxy = guard.Server(str(self.root/'proxy.sock'), str(self.root/'raw.sock'), self.policy)
        for server in (self.raw, self.proxy):
            threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()

    def tearDown(self):
        for server in (self.proxy, self.raw): server.shutdown(); server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = guard.UnixHTTP(str(self.root/'proxy.sock'))
        conn.request(method, path, body=json.dumps(body) if body is not None else None)
        response = conn.getresponse(); data = response.read(); conn.close()
        return response.status, json.loads(data) if data else None

    def denied(self, method, path, body=None):
        before = len(self.raw.calls)
        self.assertEqual(self.request(method, path, body)[0], 403)
        self.assertEqual(len(self.raw.calls), before, 'denied request reached upstream')

    def container(self):
        return {'Image': IMAGE, 'User': '1000:1000',
                'Labels': {'wizard.qa.manager-seal': 'qa', 'com.docker.compose.project': 'qa_synthetic', 'com.docker.compose.service': 'main'},
                'HostConfig': {'ReadonlyRootfs': True, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'],
                               'Memory': 128*2**20, 'MemorySwap': 128*2**20, 'PidsLimit': 32,
                               'NanoCpus': 500000000, 'ShmSize': 64*2**20, 'NetworkMode': NET,
                               'LogConfig': {'Type': 'none', 'Config': {}}, 'RestartPolicy': {'Name': 'no'}},
                'NetworkingConfig': {'EndpointsConfig': {NET: {'Aliases': ['main']}}}}

    def test_unknown_all_read_and_mutation_routes_prevent_forward(self):
        for token in (OTHER, OTHER[:12], 'unknown-name', 'owned-prefix', 'qa-main'):
            for method, suffix in [('GET', '/json'), ('GET', '/logs'), ('GET', '/top'),
                                    ('POST', '/exec'), ('POST', '/start'), ('DELETE', '')]:
                with self.subTest(token=token, suffix=suffix): self.denied(method, '/containers/'+token+suffix)
        self.denied('GET', '/networks/'+OTHER)
        self.denied('POST', '/networks/'+NET+'/connect', {'Container': OTHER})
        self.denied('POST', '/exec/'+OTHER+'/start', {})

    def test_global_and_unknown_authority_denied(self):
        for method, path in [('POST', '/networks/prune'), ('POST', '/containers/prune'), ('POST', '/images/prune'),
                             ('POST', '/volumes/prune'), ('POST', '/build'), ('POST', '/images/create?fromImage=python'),
                             ('GET', '/volumes'), ('GET', '/services'), ('GET', '/containers/'+OWN+'/archive?path=/'),
                             ('POST', '/containers/'+OWN+'/update'), ('POST', '/commit'), ('PUT', '/containers/'+OWN+'/archive')]:
            with self.subTest(path=path): self.denied(method, path)

    def test_install_prune_exact_synthetic_label_is_typed_but_never_forwarded(self):
        from urllib.parse import urlencode
        self.policy.cfg['app_urn'] = 'agent-synthetic:migrated'
        label = 'runtipi.appurn=agent-synthetic:migrated'
        for values in ([label], {label: True}):
            self.rows.clear()
            self.denied('POST', '/v1.47/containers/prune?'+urlencode({'filters': json.dumps({'label': values})}))
            self.assertEqual(len(self.rows), 1)
            self.assertEqual({k: v for k, v in self.rows[0].items() if k != 'sequence'},
                {'operation': 'deny', 'reason': 'prune_refused', 'route': '/containers/prune',
                 'filters': {'label': {label: True}}})

    def test_other_prunes_are_generic_and_never_log_caller_data(self):
        from urllib.parse import urlencode
        self.policy.cfg['app_urn'] = 'agent-synthetic:migrated'
        label = 'runtipi.appurn=agent-synthetic:migrated'
        for filters in (None, {}, {'label': ['private-caller-sentinel']},
                        {'label': [label, 'private-caller-sentinel']},
                        {'label': [label], 'until': ['private-caller-sentinel']},
                        {'label': {label: False}}, {'label': [label], 'unknown': []}):
            self.rows.clear()
            path = '/containers/prune'
            if filters is not None: path += '?'+urlencode({'filters': json.dumps(filters)})
            self.denied('POST', path)
            self.assertEqual(len(self.rows), 1)
            row = self.rows[0]
            self.assertEqual(set(row), {'sequence', 'operation', 'reason', 'rule_line'})
            self.assertEqual(row['reason'], 'rule_refused')
            self.assertNotIn('private-caller-sentinel', json.dumps(row))
        self.rows.clear()
        self.denied('POST', '/containers/prune?'+urlencode({'filters': json.dumps({'label': [label]})}),
                    {'private-caller-sentinel': True})
        self.assertEqual(set(self.rows[0]), {'sequence', 'operation', 'reason', 'rule_line'})
        self.policy.cfg.pop('app_urn')
        self.rows.clear()
        self.denied('POST', '/containers/prune?'+urlencode({'filters': json.dumps({'label': [label]})}))
        self.assertEqual(self.rows[0]['reason'], 'rule_refused')

    def test_owned_exact_name_rewrites_to_id_and_preserves_response(self):
        self.assertEqual(self.request('GET', '/v1.47/containers/owned/json'), (200, {'real_upstream_marker': True}))
        self.assertEqual(self.raw.calls[-1][1], '/containers/'+OWN+'/json')
        self.denied('GET', '/containers/'+OWN[:12]+'/json')
        self.denied('POST', '/containers/'+OWN+'/exec', {})

    def test_real_lists_intersect_ids_even_spoofed_labels(self):
        for path, own in [('/containers/json', OWN), ('/networks', NET), ('/images/json', IMAGE)]:
            self.raw.replies[path] = [{'Id': own, 'marker': 'from upstream'}, {'Id': OTHER, 'Labels': self.container()['Labels']}]
            status, value = self.request('GET', path)
            self.assertEqual(status, 200); self.assertEqual(value, [{'Id': own, 'marker': 'from upstream'}])
            self.assertIn('filters=', self.raw.calls[-1][1])
        self.raw.replies['/networks/'+NET] = {'Id': NET, 'Containers': {OWN: {'Name': 'owned'}, OTHER: {'Name': 'never expose'}}}
        self.assertEqual(self.request('GET', '/networks/'+NET)[1]['Containers'], {OWN: {'Name': 'owned'}})

    def test_create_actual_response_registers_before_client_observes(self):
        self.raw.replies['/containers/create'] = {'Id': NEW, 'Warnings': ['upstream warning']}
        status, result = self.request('POST', '/containers/create?name=qa-main', self.container())
        self.assertEqual(status, 201); self.assertEqual(result['Warnings'], ['upstream warning'])
        self.assertEqual(self.policy.containers[NEW], 'qa-main')
        self.assertTrue(any(row.get('id') == NEW and row['operation'] == 'created' for row in self.rows))
        self.assertEqual(self.request('GET', '/containers/qa-main/json')[0], 200)
        self.denied('POST', '/containers/create?name=qa-main', self.container())

    def test_created_id_deleted_name_not_authority_for_reused_object(self):
        self.request('DELETE', '/containers/'+OWN)
        self.denied('GET', '/containers/owned/json')
        self.assertIn(OWN, self.policy.history)

    def test_activation_barrier_blocks_all_create_start(self):
        self.policy.activated = False
        self.denied('POST', '/containers/create?name=qa-main', self.container())
        self.denied('POST', '/containers/'+OWN+'/start')
        self.denied('POST', '/networks/create', {'Name': 'qa-net'})

    def test_privilege_hostpath_limits_and_image_bypasses_denied(self):
        mutations = [('Privileged', True), ('NetworkMode', 'host'), ('PidMode', 'host'), ('IpcMode', 'host'),
                     ('Devices', [{'PathOnHost': '/dev/sda'}]), ('Mounts', [{'Type': 'bind', 'Source': '/'}]),
                     ('Binds', ['/:/host:ro']), ('VolumesFrom', ['other']), ('SecurityOpt', ['seccomp=unconfined']),
                     ('CapAdd', ['SYS_ADMIN']), ('CapDrop', []), ('ReadonlyRootfs', False),
                     ('Memory', 0), ('MemorySwap', -1), ('MemorySwap', 0), ('PidsLimit', 0),
                     ('PortBindings', {'80/tcp': [{'HostPort': '8080'}]}), ('CgroupParent', '/'),
                     ('UsernsMode', 'host'), ('DeviceRequests', [{'Count': -1}]), ('Runtime', 'nvidia'),
                     ('Sysctls', {'kernel.domainname': 'escape'}), ('OomKillDisable', True),
                     ('Tmpfs', {'/host': 'size=99999999999'}), ('LogConfig', {'Type': 'json-file'})]
        for key, value in mutations:
            body = self.container(); body['HostConfig'][key] = value
            with self.subTest(key=key): self.denied('POST', '/containers/create?name=qa-main', body)
        for key, value in [('Image', 'python:latest'), ('User', '0:0'), ('Volumes', {'/host': {}}), ('Bogus', 'value')]:
            body = self.container(); body[key] = value
            with self.subTest(key=key): self.denied('POST', '/containers/create?name=qa-main', body)

    def test_allowed_bind_rejects_symlink_and_prefix_trick(self):
        leaf = self.root/'synthetic'; leaf.mkdir()
        self.policy.cfg['binds'] = {str(leaf): {'target': '/synthetic', 'mode': 'rw'}}
        good = self.container(); good['HostConfig']['Binds'] = [str(leaf)+':/synthetic:rw']
        self.policy.authorize('POST', '/containers/create?name=qa-main', json.dumps(good).encode())
        leaf.rmdir(); leaf.symlink_to(self.root)
        self.denied('POST', '/containers/create?name=qa-main', good)
        good['HostConfig']['Binds'] = [str(leaf)+'-other:/synthetic:rw']
        self.denied('POST', '/containers/create?name=qa-main', good)

    def test_long_bind_api_same_exact_authority_no_propagation_or_named_volumes(self):
        leaf = self.root/'long-bind'; leaf.mkdir()
        self.policy.cfg['binds'] = {str(leaf): {'target': '/synthetic', 'mode': 'ro'}}
        body = self.container()
        mount = {'Type': 'bind', 'Source': str(leaf), 'Target': '/synthetic', 'ReadOnly': True, 'BindOptions': {'CreateMountpoint': True}}
        body['HostConfig']['Mounts'] = [mount]
        self.policy.authorize('POST', '/containers/create?name=qa-main', json.dumps(body).encode())
        for key, value in [('Type', 'volume'), ('Source', '/'), ('ReadOnly', False), ('Target', '/host'),
                           ('BindOptions', {'Propagation': 'rshared'}), ('VolumeOptions', {'DriverConfig': {'Name': 'local'}})]:
            changed = copy.deepcopy(body); changed['HostConfig']['Mounts'][0][key] = value
            self.denied('POST', '/containers/create?name=qa-main', changed)

    def test_network_connect_requires_both_owned_ids(self):
        status, _ = self.request('POST', '/networks/owned-net/connect', {'Container': 'owned', 'EndpointConfig': {'Aliases': ['main']}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.raw.calls[-1][2])['Container'], OWN)
        self.denied('POST', '/networks/'+NET+'/connect', {'Container': OWN, 'EndpointConfig': {'IPAMConfig': {'IPv4Address': '1.1.1.1'}}})

    def test_events_forward_real_owned_only(self):
        def row(ident, kind='container', attrs=None):
            return {'Type': kind, 'Action': 'create', 'Actor': {'ID': ident, 'Attributes': attrs or {}}, 'timeNano': 42}
        lines = [row(OTHER), row(OWN), row(NET, 'network', {'container': OTHER}), row(NET, 'network', {'container': OWN})]
        self.raw.replies['/events'] = b''.join(json.dumps(line).encode()+b'\n' for line in lines)
        conn = guard.UnixHTTP(str(self.root/'proxy.sock')); conn.request('GET', '/events?since=1&until=2')
        response = conn.getresponse(); result = [json.loads(line) for line in response.read().splitlines()]; conn.close()
        self.assertEqual(result, [lines[1], lines[3]])
        self.assertNotIn(OTHER, json.dumps(self.rows))

    def live_stream(self, **kw):
        import queue
        from manager_seal_runtime_events import RuntimeEventStream
        self.raw.event_queue = queue.Queue()
        return RuntimeEventStream(self.root/'proxy.sock', self.root/'runtime-events.json',
                                  lambda: set(self.policy.history), **kw).start()

    def emit(self, row):
        self.raw.event_queue.put(json.dumps(row).encode()+b'\n')

    def test_live_chunked_without_until_dynamic_exact_ids_and_join(self):
        stream = self.live_stream()
        owned = {'Type': 'container', 'Action': 'destroy', 'Actor': {'ID': OWN}, 'timeNano': 42}
        self.emit(dict(owned, Actor={'ID': OTHER})); self.emit(owned)
        stream.barrier(lambda rows: owned in rows)
        # Ordinary requests still progress while /events is open. The create
        # response registers identity before queued events take the policy lock.
        self.raw.replies['/containers/create'] = {'Id': NEW}
        self.assertEqual(self.request('POST', '/containers/create?name=qa-main', self.container())[0], 201)
        created = dict(owned, Action='create', Actor={'ID': NEW})
        attached = {'Type': 'network', 'Action': 'connect', 'Actor': {'ID': NET, 'Attributes': {'container': NEW}}, 'timeNano': 43}
        self.emit(created); self.emit(dict(attached, Actor={'ID': NET, 'Attributes': {'container': OTHER}})); self.emit(attached)
        stream.drain(lambda rows: created in rows and attached in rows, timeout=2, settle=.1)
        rows = stream.finish()
        self.assertEqual(rows, [owned, created, attached]); self.assertFalse(stream.thread.is_alive())
        self.assertEqual(json.loads(stream.output.read_text()), rows)
        self.assertNotIn('until=', self.raw.calls[0][1]); self.assertIn('/events?since=', self.raw.calls[0][1])
        self.assertNotIn(OTHER, json.dumps(self.rows))
        self.raw.event_queue.put(None)

    def test_live_full_synthetic_two_cycle_sequence_uses_actual_reader(self):
        from test_manager_seal_installed_path import retained_event_sequences
        import manager_seal_installed_path as rig
        ledger, ids, old, synthetic = retained_event_sequences()
        self.policy.history.update(r['id'] for r in ledger if r['operation'] == 'created')
        stream = self.live_stream()
        for e in synthetic: self.emit(e)
        stream.drain(lambda rows: len(rows) == len(synthetic), timeout=2, settle=.1)
        rows = stream.finish()
        self.assertEqual(rig.reconcile_runtime_events(rows, ledger, ids, 'msi-5ae46c549e29')['container_create_coverage'], 10)
        self.raw.event_queue.put(None)

    def test_live_absolute_deadline_interrupts_idle_reader(self):
        stream = self.live_stream(seconds=.2)
        stream.thread.join(timeout=1)
        self.assertFalse(stream.thread.is_alive())
        with self.assertRaises(RuntimeError): stream.finish()
        self.assertEqual(json.loads(stream.output.read_text()), [])
        self.raw.event_queue.put(None)

    def test_live_eof_cap_missing_sentinel_and_partial_fail_closed_persist(self):
        import time
        row = {'Type': 'container', 'Action': 'create', 'Actor': {'ID': OWN}, 'timeNano': 42}
        for variant in ('eof', 'cap', 'missing', 'partial'):
            with self.subTest(variant=variant):
                stream = self.live_stream(**({'row_cap': 1} if variant == 'cap' else {}))
                self.emit(row)
                stream.barrier(lambda rows: len(rows) == 1)
                if variant == 'eof': self.raw.event_queue.put(None)
                elif variant == 'cap': self.emit(row)
                elif variant == 'partial': self.raw.event_queue.put(b'{"broken":') ; self.raw.event_queue.put(None)
                stream.drain(lambda rows: False, timeout=.2)
                with self.assertRaises(RuntimeError): stream.finish()
                self.assertFalse(stream.thread.is_alive())
                self.assertEqual(json.loads(stream.output.read_text()), [row])
                self.raw.event_queue.put(None)

    def test_protocol_smuggling_denied_before_upstream(self):
        cases = [b'POST /containers/create?name=qa-main HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nContent-Length: 0\r\n\r\n{}',
                 b'POST /containers/create HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n',
                 b'GET /_ping HTTP/1.1\r\nHost: x\r\nUpgrade: tcp\r\n\r\n',
                 b'POST /containers/create HTTP/1.1\r\nHost: x\r\nContent-Length: 1048577\r\n\r\n']
        for wire in cases:
            before = len(self.raw.calls)
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(2); sock.connect(str(self.root/'proxy.sock')); sock.sendall(wire)
                self.assertIn(b'403', sock.recv(4096))
            self.assertEqual(len(self.raw.calls), before)
        for path in ['/v1.47/containers/%2e%2e/json', '/containers/'+OWN+'%2fjson', '/_ping?a=1&a=2',
                     '/_ping?filters={}', 'http://host/_ping', '/_ping%00', '/_ping/../version']:
            self.denied('GET', path)

    def test_duplicate_json_and_unknown_filters_denied(self):
        with self.assertRaises(guard.Denied):
            self.policy.authorize('POST', '/containers/create?name=qa-main', b'{"Image":"a","Image":"b"}')
        self.denied('GET', '/containers/json?filters=%7B%22unknown%22%3A%5B%22x%22%5D%7D')

    def test_ledger_cap_refuses_instead_of_untracked_forward(self):
        self.policy.audit_bytes = 16*2**20
        before = len(self.raw.calls)
        conn = guard.UnixHTTP(str(self.root/'proxy.sock')); conn.request('GET', '/version')
        with self.assertRaises(Exception): conn.getresponse()
        conn.close(); self.assertEqual(len(self.raw.calls), before)


if __name__ == '__main__': unittest.main()
