"""Actual pinned SDK wire replay. Local Unix peers only; no Docker acceptance."""
import ast
import base64
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import textwrap
import types
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_forward_guard as guard
import manager_seal_installed_path as rig
import test_manager_seal_forward_guard as protocol_tests

FIX = Path(__file__).with_name('fixtures')
WIRE_SHA = '32fb827e053c0c056d7ad151e432ba3bb9e8618f8ff51abcfed96897dbaaf197'
LEDGER_SHA = 'a69edf1f4bf3b7f38463f52d13620824f2f59754e825d13e5971c902aceb3b4f'
LABEL = 'runtipi.appurn='+rig.URN
FILTERS = {'label': {LABEL: True}}


class CapturedPrune(unittest.TestCase):
    setUp = protocol_tests.Protocol.setUp
    tearDown = protocol_tests.Protocol.tearDown

    def fixture(self):
        raw = (FIX/'manager-seal-prune-sdk.http').read_bytes()
        meta = json.loads((FIX/'manager-seal-prune-sdk.json').read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), WIRE_SHA)
        self.assertEqual(meta['wire_sha256'], WIRE_SHA)
        head, body = raw.split(b'\r\n\r\n', 1)
        self.assertEqual(head.split(b'\r\n')[0].decode(), meta['request_line'])
        self.assertEqual(body, base64.b64decode(meta['body_base64']))
        self.assertEqual(len(body), int(meta['headers']['content-length']))
        self.assertEqual(meta['headers']['content-type'], 'application/json')
        for name, version in [('dockerode', '4.0.9'), ('docker-modem', '5.0.6')]:
            pin = meta['sdk_pins'][name]
            self.assertEqual(pin['version'], version)
            self.assertIn(pin['tarball_sha512'], (Path(__file__).resolve().parents[2]/'bun.lock').read_text())
        self.policy.cfg['app_urn'] = rig.URN
        return raw, meta

    def replay(self, raw):
        before = len(self.raw.calls)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2); sock.connect(str(self.root/'proxy.sock')); sock.sendall(raw)
            response = bytearray()
            while True:
                block = sock.recv(4096)
                if not block: break
                response.extend(block)
        self.assertEqual(bytes(response).split(b'\r\n', 1)[0], b'HTTP/1.1 403 Forbidden')
        self.assertEqual(len(self.raw.calls), before, 'prune reached upstream')
        self.assertEqual(len(self.rows), 1)
        return self.rows[0]

    def typed(self, row):
        self.assertEqual(row, {'sequence': 1, 'operation': 'deny', 'reason': 'prune_refused',
                               'route': '/containers/prune', 'filters': FILTERS})

    def wire(self, path, body):
        return ('POST '+path+' HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body

    def test_actual_captured_bytes_are_typed_403_without_forwarding(self):
        raw, meta = self.fixture()
        self.typed(self.replay(raw))
        op, path, data, filters = self.policy.authorize('POST', meta['path'], base64.b64decode(meta['body_base64']))
        self.assertEqual((op, path, data, filters), ('prune-refused', '/containers/prune', b'', FILTERS))

    def test_exact_historical_branch_replays_as_generic_body_refusal(self):
        raw, meta = self.fixture()
        tree = ast.parse(Path(guard.__file__).read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'authorize')
        branches = [n for n in ast.walk(node) if isinstance(n, ast.If)
                    and ast.unparse(n.test) == "method == 'POST' and path == '/containers/prune'"
                    and any(isinstance(v, ast.Return) for v in n.body)]
        self.assertEqual(len(branches), 1)
        historical = ast.parse(textwrap.dedent(meta['legacy_branch'])).body
        ast.increment_lineno(ast.Module(body=historical, type_ignores=[]), meta['legacy_branch_first_line']-1)
        branches[0].body = historical
        # Restore the original orelse parser too; unrelated policy is unchanged.
        bound = next(n for n in node.body if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "method == 'POST' and path == '/containers/prune'")
        node.body[node.body.index(bound):node.body.index(bound)+1] = bound.orelse
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        scope = dict(vars(guard)); exec(compile(module, guard.__file__, 'exec'), scope)
        self.policy.authorize = types.MethodType(scope['authorize'], self.policy)
        row = self.replay(raw)
        self.assertEqual(row, {'sequence': 1, 'operation': 'deny', 'reason': 'rule_refused', 'rule_line': 358})

    def test_empty_body_and_equivalent_array_map_encodings_remain_typed(self):
        _, meta = self.fixture()
        for query in ({'label': [LABEL]}, FILTERS):
            for body in (b'', json.dumps({'filters': {'label': [LABEL]}}).encode(), json.dumps({'filters': FILTERS}).encode()):
                with self.subTest(query=query, body=body):
                    self.rows.clear(); self.policy.sequence = 0
                    path = '/containers/prune?'+urlencode({'filters': json.dumps(query)})
                    self.typed(self.replay(self.wire(path, body)))

    def test_body_query_selector_and_json_negatives_are_generic_without_data(self):
        _, meta = self.fixture()
        body = base64.b64decode(meta['body_base64'])
        foreign = 'private-caller-sentinel'
        negatives = [
            (meta['path'], json.dumps({'filters': {'label': [foreign]}}).encode()),
            (meta['path'], json.dumps({'filters': FILTERS, foreign: True}).encode()),
            ('/containers/prune?'+urlencode({'filters': json.dumps({'label': [foreign]})}), body),
            (meta['path'], b'not-json '+foreign.encode()),
            (meta['path'], body+b' '*4097),
            (meta['path']+'&extra='+foreign, body),
            # Mixed representations inside a value or across filter keys fail closed.
            (meta['path'], json.dumps({'filters': {'label': [FILTERS['label']]}}).encode()),
            (meta['path'], json.dumps({'filters': {'label': [LABEL], 'until': {foreign: True}}}).encode()),
            ('/containers/prune?'+urlencode({'filters': json.dumps({'label': {LABEL: True}, 'until': [foreign]})}), body),
            (meta['path'], b'{}'),
            (meta['path'], b'{"filters":{},"filters":{}}'),
            (meta['path'], ('{"filters":{"label":{"'+LABEL+'":true,"'+LABEL+'":true}}}').encode()),
            (meta['path'], json.dumps({'filters': {'label': {LABEL: False}}}).encode()),
            (meta['path'], json.dumps({'filters': {'label': {LABEL: 1}}}).encode()),
            (meta['path'], b'{"filters":NaN}'),
            (meta['path'], b'{"filters":Infinity}'),
            (meta['path'], body.decode().encode('utf-16')),
            (meta['path'], b'\xef\xbb\xbf'+body),
            (meta['path'], body+b' trailing'),
            (meta['path'], b'[]'),
            (meta['path'], json.dumps({'filters': {'label': [LABEL, LABEL]}}).encode()),
            (meta['path']+'&filters=%7B%7D', body),
            (meta['path'], b'['*1200+b'0'+b']'*1200),
            ('/containers/prune?filters='+('['*1200+'0'+']'*1200), body),
        ]
        for path, data in negatives:
            with self.subTest(path=path, body_length=len(data)):
                self.rows.clear()
                row = self.replay(self.wire(path, data))
                self.assertEqual(set(row), {'sequence', 'operation', 'reason', 'rule_line'})
                self.assertEqual(row['reason'], 'rule_refused')
                self.assertNotIn(foreign, json.dumps(row))


class LatestRetainedAccounting(unittest.TestCase):
    def setUp(self):
        raw = (FIX/'manager-seal-guard-ledger-18e32355313b.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), LEDGER_SHA)
        self.actual = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(self.actual[18], {'sequence': 19, 'operation': 'deny', 'reason': 'rule_refused', 'rule_line': 358})
        self.corrected = copy.deepcopy(self.actual)
        self.corrected[18] = {'sequence': 19, 'operation': 'deny', 'reason': 'prune_refused', 'route': '/containers/prune', 'filters': FILTERS}

    def reconcile(self, rows):
        return rig.reconcile_guard_accounting(rows, 9, [rig.PROJECT+'_'+n for n in rig.NETS], expected_cycles=2,
            install_prune_window=(18, 44), expected_probe_rows=self.actual[2:11])

    def test_latest_actual_ledger_fails_exactly_unexpected_guard_refusal(self):
        with self.assertRaisesRegex(AssertionError, '^unexpected guard refusal$'):
            self.reconcile(self.actual)

    def test_only_seq19_replacement_passes_strict_full_accounting(self):
        self.assertEqual([i for i, (a, b) in enumerate(zip(self.actual, self.corrected)) if a != b], [18])
        self.assertEqual(self.reconcile(self.corrected), {'expected_refusals': 10, 'probe_refusals': 9,
            'install_prune_refusals': 1, 'start_restart_prune_refusals': 0,
            'network_discovery_misses': 8, 'paired_network_creations': 8})

    def test_no_refusal_probe_window_or_network_pair_waiver(self):
        variants = []
        rows = copy.deepcopy(self.corrected); rows[2]['rule_line'] = 999; variants.append(rows)
        rows = copy.deepcopy(self.corrected); rows[18]['filters'] = {}; variants.append(rows)
        rows = copy.deepcopy(self.corrected); rows.append(dict(rows[18], sequence=len(rows)+1)); variants.append(rows)
        rows = copy.deepcopy(self.corrected); rows[18]['sequence'] = 45; variants.append(rows)
        rows = copy.deepcopy(self.corrected)
        next(r for r in rows if r['operation'] == 'created' and r.get('kind') == 'network')['name'] = 'not-owned'; variants.append(rows)
        for rows in variants:
            with self.assertRaises((AssertionError, KeyError)): self.reconcile(rows)


if __name__ == '__main__': unittest.main()
