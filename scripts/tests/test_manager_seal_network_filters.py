"""Offline regression only; never claims fake peers prove Docker compatibility."""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_forward_guard as guard
import manager_seal_installed_path as rig
import manager_seal_installed_recovery as recovery
import test_manager_seal_forward_guard as base

FIX = Path(__file__).parent/'fixtures/manager_seal_wire'


def query(filters, route='/networks'):
    return route+'?'+urllib.parse.urlencode({'filters': json.dumps(filters)})


def from_json_shape(raw):
    """Schema-only model of the two whole-object FromJSON unmarshals.

    This is NOT execution of Go. Native test below uses the complete pinned
    Go implementation; absent Go is an explicit skip, not a substitute pass.
    """
    value = json.loads(raw)
    if not value:
        return value
    canonical = all(type(v) is dict and all(type(b) is bool for b in v.values()) for v in value.values())
    legacy = all(type(v) is list and all(type(s) is str for s in v) for v in value.values())
    if not (canonical or legacy):
        raise ValueError('invalid filter')
    return value


class FilterSerialization(unittest.TestCase):
    def policy(self, networks=None):
        return guard.Policy({'networks': networks or {}, 'containers': {}, 'images': []})

    def test_retained_producer_old_wire_fails_both_complete_decoder_shapes(self):
        vector = json.loads((FIX/'network-list-retained.json').read_text())
        self.assertFalse(vector['actual_daemon_query_captured'])
        old = self.policy().path(vector['old_forwarded_target'])[1]['filters']
        with self.assertRaisesRegex(ValueError, 'invalid filter'): from_json_shape(old)
        op, target, _, _ = self.policy().authorize('GET', vector['incoming_target'])
        fixed = from_json_shape(self.policy().path(target)[1]['filters'])
        self.assertEqual(op, 'list-networks')
        self.assertEqual(fixed['id'], {'0'*64: True})
        self.assertEqual(set(fixed['name']), set(json.loads(old)['name']))
        self.assertTrue(all(type(v) is dict for v in fixed.values()))

    def test_canonical_and_legacy_inputs_have_one_identical_canonical_wire(self):
        p = self.policy({'a'*64: 'owned-a', 'b'*64: 'owned-b'})
        targets = [p.authorize('GET', query(f))[1] for f in
                   ({'name': {'synthetic': True}}, {'name': ['synthetic']})]
        self.assertEqual(*targets)
        f = from_json_shape(p.path(targets[0])[1]['filters'])
        self.assertEqual(f['id'], {'a'*64: True, 'b'*64: True})
        self.assertEqual(f['name'], {'synthetic': True})

    def test_id_authority_is_intersection_not_overwrite_for_every_list_route(self):
        for route, key, authority, requested in (
            ('/networks', 'id', 'a'*64, 'b'*64),
            ('/containers/json', 'id', 'a'*64, 'b'*64),
            ('/images/json', 'reference', 'sha256:'+'a'*64, 'sha256:'+'b'*64)):
            p = self.policy({'a'*64: 'owned'})
            p.containers = {'a'*64: 'owned'}; p.cfg['images'] = [authority]
            for selector in ([requested], {requested: True}):
                with self.subTest(route=route, selector=selector):
                    _, target, _, _ = p.authorize('GET', query({key: selector}, route))
                    f = json.loads(p.path(target)[1]['filters'])
                    self.assertEqual(f[key], {'0'*64 if key == 'id' else 'sha256:'+'0'*64: True})
        p = self.policy({'a'*64: 'owned-a', 'b'*64: 'owned-b'})
        _, target, _, _ = p.authorize('GET', query({'id': ['aaa'], 'name': ['synthetic']}))
        self.assertEqual(json.loads(p.path(target)[1]['filters'])['id'], {'a'*64: True})

    def test_strict_filters_unknown_types_casing_duplicates_and_mixed_refuse(self):
        p = self.policy()
        bad = [{'Name': ['x']}, {'ID': ['a']}, {'name': 'x'}, {'name': None}, {'name': 1},
               {'name': []}, {'name': {}}, {'name': [True]}, {'name': ['']}, {'name': ['x','x']},
               {'name': {'x': False}}, {'name': {'x': 1}}, {'name': ['x\n']},
               {'name': ['x'], 'id': {'a'*64: True}}, {'unknown': ['x']},
               {'id': ['SHA256:xxx']}, {'id': ['A'*64]}, {'status': ['running']}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(guard.Denied):
                p.authorize('GET', query(value))
        duplicate = '/networks?'+urllib.parse.urlencode({'filters':'{"name":{"x":true,"x":true}}'})
        with self.assertRaises(guard.Denied): p.authorize('GET', duplicate)
        for route in ('/networks', '/containers/json'):
            with self.assertRaises(guard.Denied): p.authorize('GET', query({'reference':['x']}, route))

    def test_public_source_identity_and_decoder_path(self):
        pins = json.loads((FIX/'public-sources.json').read_text())
        for name, pin in pins.items():
            raw = (FIX/name).read_bytes()
            self.assertEqual(len(raw), pin['bytes']); self.assertEqual(hashlib.sha256(raw).hexdigest(), pin['sha256'])
        self.assertIn('filters.ToParamWithVersion(cli.version, options.Filters)', (FIX/'sdk-network-list.go').read_text())
        version_source = (FIX/'sdk-filter-version.go').read_text()
        self.assertIn('out, err := ToJSON(a)', version_source)
        self.assertIn('versions.LessThan(version, "1.22")', version_source)
        source = (FIX/'sdk-filter-parse.go').read_text()
        self.assertIn('map[string]map[string]bool', source)
        self.assertIn('map[string][]string', source)
        self.assertIn('return args, &invalidFilter{}', source)
        self.assertIn('filters.FromJSON(r.Form.Get("filters"))', (FIX/'daemon-network-router.go').read_text())

    def test_native_complete_pinned_sdk_decoder(self):
        go = shutil.which('go')
        if not go: self.skipTest('Go unavailable: native pinned Moby execution NOT verified here')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            env = dict(os.environ, GO111MODULE='off', GOPROXY='off', GOSUMDB='off', GOTOOLCHAIN='local',
                       GOMAXPROCS='2', GOCACHE=str(Path(tmp)/'cache'))
            r = subprocess.run([go, 'test', '-v', str(FIX/'sdk-filter-parse.go'), str(FIX/'sdk-filter-errors.go'),
                                str(FIX/'network_filters_test.go')], env=env, capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0, (r.stdout+r.stderr).decode()[:8192])


class WireDiagnostics(unittest.TestCase):
    setUp = base.Protocol.setUp
    tearDown = base.Protocol.tearDown
    request = base.Protocol.request
    denied = base.Protocol.denied

    def test_invalid_types_never_reach_counting_peer(self):
        for f in ({'name': {'x': False}}, {'Name': ['x']}, {'name': 4}, {'name':['x'], 'id': {'a':True}}):
            self.denied('GET', query(f))

    def test_canary_empty_means_upstream_empty_not_guard_postfilter(self):
        self.policy.networks = {}
        for payload, expected in (([], True), ([{'Id':'b'*64}], False)):
            self.raw.replies['/networks'] = payload
            self.assertEqual(self.request('GET', '/networks'), (200, []))
            row = [r for r in self.rows if r['operation'] == 'zero-authority-network-result'][-1]
            self.assertIs(row['upstream_empty'], expected)
            self.assertNotIn('b'*64, json.dumps(self.rows))

    def test_network_synthetic_selectors_only_and_fixed_error_class(self):
        self.policy.cfg['network_names'] = ['owned-synthetic']
        self.raw.replies['/networks'] = {'message': 'invalid filter'}
        send_response = base.Upstream.send_response
        with patch.object(base.Upstream, 'send_response', lambda handler, status: send_response(handler, 400)):
            status, _ = self.request('GET', query({'name': {'owned-synthetic': True}}))
        self.assertEqual(status, 400)
        selectors = [r for r in self.rows if r['operation'] == 'synthetic-network-query']
        self.assertEqual(len(selectors), 1)
        self.assertEqual(selectors[0]['filters']['name'], {'owned-synthetic': True})
        self.assertEqual(self.rows[-1]['error_class'], 'invalid_filter')
        self.raw.replies['/networks'] = {'message': 'token=PRIVATE'}
        with patch.object(base.Upstream, 'send_response', lambda handler, status: send_response(handler, 400)):
            self.request('GET', query({'label': {'token=PRIVATE': True}}))
        self.assertEqual(len([r for r in self.rows if r['operation'] == 'synthetic-network-query']), 1)
        self.assertNotIn('PRIVATE', json.dumps(self.rows)); self.assertNotIn('headers', json.dumps(self.rows))


class Continuation(unittest.TestCase):
    def test_smaller_capacity_estimate_is_not_a_proven_write_quota(self):
        assessment = rig.capacity.smaller_envelope_assessment(rig.FOOTPRINT)
        self.assertFalse(assessment['smaller_peak_proven'])
        self.assertGreater(assessment['declared_components_without_margin_bytes'], 512*rig.MIB)
        retained = 2251644928  # exact pinned metadata snapshot, not observed output-as-bound
        decision = rig.capacity.admission(retained, 299713110016)
        self.assertFalse(decision['admitted'])
        self.assertEqual(decision['next_run_additional_peak_bytes'], rig.capacity.ALLOWANCE)
        self.assertEqual(rig.capacity.ALLOWANCE, rig.GIB)
        self.assertEqual(rig.capacity.RETAINED_PEAK, 3*rig.GIB)
        self.assertEqual(rig.capacity.RESERVE, 200*rig.GIB)

    def test_real_canary_precedes_donor_broker_and_ephemeral_assembly(self):
        text = inspect.getsource(rig.Rig.run)
        for later in ('donor =', 'self.rabbit_limit_canary(', 'volumes = {}', 'build_command('):
            self.assertLess(text.index('network_filter_canary('), text.index(later))
        canary = inspect.getsource(rig.network_filter_canary)
        self.assertIn("filters['id'] = ['0'*64]", canary)
        self.assertIn('forward_guard.Server(', canary)
        self.assertIn('target == incoming', canary)
        self.assertIn("'zero_authority': True", canary)
        self.assertIn("(400, 'invalid_filter')", canary)
        self.assertIn("(200, 'empty_list')", canary)
        self.assertNotIn('subprocess.', canary)

    def test_stopped_start_aborts_first_poll_with_one_post_and_readonly_snapshot(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out = Path(tmp); r.receipt = {}
            request_id = '00000000-0000-4000-8000-000000000001'
            r.http = Mock(side_effect=[(201, {'requestId': request_id}), (200, {'status': 'stopped'})])
            r.sql = Mock(return_value='stopped'); r.owned_fixture_ids = Mock(return_value=[])
            r.wait = lambda label, predicate, timeout: predicate()
            with self.assertRaisesRegex(RuntimeError, 'terminal lifecycle failure'):
                r.transaction('start', {'skipPull':True}, 'running')
            self.assertEqual([c.args[0] for c in r.http.call_args_list], ['POST','GET'])
            r.sql.assert_called_once(); r.owned_fixture_ids.assert_called_once()
            f = json.loads((r.out/'terminal-failure.json').read_text())
            self.assertEqual(f['database_status'], 'stopped'); self.assertEqual(f['owned_fixture_ids'], [])
            self.assertTrue(f['read_only']); self.assertNotIn('transactions', r.receipt)

    def test_restart_preserved_running_error_log_aborts_without_new_post(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out = Path(tmp); r.receipt = {}
            (r.out/'manager.sanitized.log').write_text('App '+rig.URN+' restart failed: public diagnostic\n')
            r.http = Mock(return_value=(200, {})); r.sql = Mock(return_value='running')
            r.owned_fixture_ids = Mock(return_value=[])
            with self.assertRaisesRegex(RuntimeError, 'terminal lifecycle failure'):
                r.transaction_ready('restart', 'running', set(), 0, 'id')
            self.assertEqual(r.http.call_args.args[0], 'GET')
            self.assertTrue(r.receipt['terminal_failure']['synthetic_failure_log_seen'])

    def test_terminal_db_snapshot_survives_diagnostic_get_error_without_retry(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out = Path(tmp); r.receipt = {}
            r.sql = Mock(return_value='stopped'); r.http = Mock(side_effect=TimeoutError('read failed'))
            r.owned_fixture_ids = Mock()
            with self.assertRaises(TimeoutError):
                r.transaction_ready('start', 'running', set(), 0, 'id')
            state = json.loads((r.out/'terminal-failure.json').read_text())
            self.assertEqual(state['database_status'], 'stopped')
            self.assertIsNone(state['manager_http_status']); self.assertIsNone(state['owned_fixture_ids'])
            self.assertEqual(r.http.call_count, 1); self.assertEqual(r.http.call_args.args[0], 'GET')
            r.owned_fixture_ids.assert_not_called()

    def test_install_stopped_remains_success_not_a_failure_and_no_read_retry(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out = Path(tmp); r.receipt = {}
            r.http = Mock(); r.sql = Mock(return_value='stopped')
            self.assertTrue(r.transaction_ready('install', 'stopped', set(), 0, 'id'))
            r.http.assert_not_called()


if __name__ == '__main__': unittest.main()
