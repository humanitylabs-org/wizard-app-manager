#!/usr/bin/env python3
"""Synthetic collector-only fixtures for the finite installer-health capture.

No Docker, VM, network, sudo or real home. Runs in the native job BEFORE the
VM boots (stage layout: tests.py beside collector.py/guest.py) or from the repo.
Any failure aborts the native job. Existing 105 tests are not rerun here.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
if (HERE / 'collector.py').is_file():
    COLLECTOR, GUEST, RUNNER = HERE / 'collector.py', HERE / 'guest.py', HERE / 'runner.py'
else:
    Q = HERE.parent / 'qa/privsep-storage'
    COLLECTOR, GUEST, RUNNER = Q / 'health_capture_collector.py', Q / 'health_capture_guest.py', Q / 'health_capture_run.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


C = load('hc_collector_fixture', COLLECTOR)
G = load('hc_guest_fixture', GUEST)
C = G.C                                  # one shared module instance (Violation identity)

PIN = 'sha256:' + '1' * 64
UPSTREAM_MGR = 'sha256:' + '9' * 64
IMG = {'runtipi-db': 'sha256:' + 'a' * 64, 'runtipi-queue': 'sha256:' + 'b' * 64,
       'runtipi': PIN, 'runtipi-reverse-proxy': 'sha256:' + 'c' * 64}
CID = {s: format(i + 1, 'x') * 64 for i, s in enumerate(C.SERVICES)}
CID['runtipi'] = 'd' * 64
OLD_MGR = 'e' * 64
TS0 = '2026-10-31T00:00:00.000000000Z'
TS1 = '2026-10-31T00:00:01.000000000Z'
TS2 = '2026-10-31T00:00:02.000000000Z'
DB_HC = {'Test': ['CMD-SHELL', 'pg_isready -d tipi -U tipi'], 'Interval': 5 * 10 ** 9, 'Timeout': 10 * 10 ** 9,
         'StartPeriod': 5 * 10 ** 9, 'Retries': 20}
MGR_HC = {'Test': ['CMD', 'curl', '-f', 'http://localhost:3000/api/health'], 'Interval': 5 * 10 ** 9,
          'Timeout': 10 * 10 ** 9, 'StartPeriod': 10 * 10 ** 9, 'Retries': 20}
EVIL_HC = {'Test': ['CMD-SHELL', 'touch /tmp/pwned; cat /etc/shadow'], 'Interval': 5 * 10 ** 9,
           'Timeout': 10 * 10 ** 9, 'StartPeriod': 10 * 10 ** 9, 'Retries': 20}


def config(**over):
    svc = {'runtipi-db': {'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -d tipi -U tipi'], 'interval': '5s',
                                          'timeout': '10s', 'retries': 20, 'start_period': '5s'}},
           'runtipi-queue': {}, 'runtipi-reverse-proxy': {},
           'runtipi': {'healthcheck': {'test': ['CMD', 'curl', '-f', 'http://localhost:3000/api/health'],
                                       'interval': '5s', 'timeout': '10s', 'retries': 20, 'start_period': '10s'}}}
    svc.update(over)
    return {'services': svc}


def cdoc(service, *, cid=None, image=None, status='running', health='healthy', started=TS1, restart=0,
         exit_code=0, oom=False, records=None, streak=0, hc='default', created=TS0):
    cid = cid or CID[service]; image = image or IMG[service]
    if hc == 'default':
        hc = DB_HC if service == 'runtipi-db' else MGR_HC if service == 'runtipi' else None
    state = {'Status': status, 'Running': status == 'running', 'Paused': False, 'Restarting': False,
             'OOMKilled': oom, 'Dead': status == 'dead', 'Pid': 1, 'ExitCode': exit_code,
             'Error': 'PRIVATE-ERROR-password=hunter2', 'StartedAt': started, 'FinishedAt': '0001-01-01T00:00:00Z'}
    if health != 'none':
        state['Health'] = {'Status': health, 'FailingStreak': streak,
                           'Log': records if records is not None else [
                               {'Start': TS1, 'End': TS2, 'ExitCode': 0, 'Output': 'PRIVATE-OUTPUT secret=x'}]}
    return {'Id': cid, 'Image': image, 'Created': created, 'RestartCount': restart, 'State': state,
            'Config': {'Healthcheck': hc, 'Env': ['POSTGRES_PASSWORD=never'], 'Labels': {
                'com.docker.compose.project': 'runtipi', 'com.docker.compose.service': service}},
            'Name': '/' + service}


class FakeDocker:
    def __init__(self, services=C.SERVICES):
        self.docs = {CID[s]: cdoc(s) for s in services}
        self.extra = {}      # service -> extra raw rows
        self.calls = []; self.engine = 'ENGINE:ABCD:1234'; self.logs = b''; self.log_calls = 0
        self.lock = threading.Lock()

    def rows(self, service):
        out = []
        for d in self.docs.values():
            labels = d['Config']['Labels']
            if labels.get('com.docker.compose.service') == service:
                out.append({'Id': d['Id'], 'ImageID': d['Image'], 'Labels': dict(labels), 'Names': [d['Name']]})
        return out + self.extra.get(service, [])

    def __call__(self, path, cap):
        with self.lock:
            self.calls.append(path)
        if path.startswith('/containers/json?all=1&filters='):
            f = json.loads(unquote(path.split('filters=', 1)[1]))
            assert f == {'label': ['com.docker.compose.project=runtipi', f['label'][1]]}
            service = f['label'][1].split('=', 1)[1]
            return 200, self.rows(service)
        if path == '/info':
            return 200, {'ID': self.engine}
        if path.startswith('/containers/') and path.endswith('/json'):
            cid = path.split('/')[2]
            return (200, json.loads(json.dumps(self.docs[cid]))) if cid in self.docs else (404, {'message': 'no'})
        if path.startswith('/images/'):
            iid = path.split('/')[2]
            return 200, {'Id': iid, 'RepoDigests': ['ghcr.io/x/y@sha256:' + '2' * 64, 'bad digest'],
                         'RepoTags': ['privsep-wam:595523fb'], 'Config': {'Env': ['SECRET=1']}}
        raise AssertionError('unexpected API path ' + path)

    def raw(self, path, cap, method='GET'):
        assert '/logs?' in path and 'follow=0' in path and 'tail=64' in path
        self.log_calls += 1
        body = self.logs
        return 200, body[:cap], len(body) > cap


def ident(engine='ENGINE:ABCD:1234', boot='12345678-1234-1234-1234-123456789abc', sock=(10, 20)):
    return {'engine': engine, 'boot': boot, 'socket': list(sock)}


def frame(text, stream=1):
    raw = text.encode() if isinstance(text, str) else text
    return bytes([stream, 0, 0, 0]) + len(raw).to_bytes(4, 'big') + raw


def observer(docker, idref=None):
    idref = idref if idref is not None else {'v': ident()}
    obs = C.Observer(docker, lambda: idref['v'], PIN)
    obs.basis, _ = C.deadline_basis(config(), set())
    return obs, idref


class SelectionAndIdentity(unittest.TestCase):
    def test_exact_four_service_selection_only(self):
        d = FakeDocker()
        d.docs['f' * 64] = cdoc('runtipi-db', cid='f' * 64)
        d.docs['f' * 64]['Config']['Labels'] = {'com.docker.compose.project': 'notes_wizardapps',
                                                'com.docker.compose.service': 'notes'}
        obs, _ = observer(d)
        obs.entry(0); self.assertEqual(obs.tick(1000, ['GATE88']), 'continue')
        lists = [unquote(p) for p in d.calls if p.startswith('/containers/json')]
        services = sorted({json.loads(p.split('filters=', 1)[1])['label'][1] for p in lists})
        self.assertEqual(services, sorted('com.docker.compose.service=' + s for s in C.SERVICES))
        self.assertNotIn('f' * 64, json.dumps(obs.bound))
        for p in d.calls:
            self.assertFalse('redis' in p or 'release-index' in p or 'notes' in p)

    def test_foreign_duplicate_short_id_refusal(self):
        good = {'Id': 'a' * 64, 'ImageID': IMG['runtipi-db'], 'Labels': {
            'com.docker.compose.project': 'runtipi', 'com.docker.compose.service': 'runtipi-db'}}
        for bad, reason in (
                (dict(good, Id='a' * 12), 'identity_invalid_id'),
                (dict(good, ImageID='a' * 64), 'identity_invalid_id'),
                (dict(good, Labels={'com.docker.compose.project': 'other', 'com.docker.compose.service': 'runtipi-db'}),
                 'identity_foreign_label'),
                (dict(good, Labels={'com.docker.compose.project': 'runtipi', 'com.docker.compose.service': 'runtipi'}),
                 'identity_foreign_label'),
                (dict(good, Labels=dict(good['Labels'], **{'com.docker.compose.container-number': '2'})),
                 'identity_duplicate_container')):
            with self.assertRaises(C.Violation) as e:
                C.list_rows([bad], 'runtipi-db')
            self.assertEqual(e.exception.reason, reason)
        with self.assertRaises(C.Violation) as e:
            C.list_rows([good, good], 'runtipi-db')
        self.assertEqual(e.exception.reason, 'identity_duplicate_container')
        with self.assertRaises(C.Violation):
            C.list_rows([good] * 5, 'runtipi-db')
        # name-only match is not authority: a container named runtipi-db without labels is never listed.
        d = FakeDocker(); d.extra['runtipi-db'] = [dict(good, Id='b' * 64, Labels={})]
        obs, _ = observer(d)
        with self.assertRaises(C.Violation):
            obs.entry(0)

    def test_engine_boot_socket_swaps(self):
        for key, val, reason in (('engine', 'ENGINE:OTHER:9999', 'identity_engine_changed'),
                                 ('boot', '87654321-1234-1234-1234-123456789abc', 'identity_boot_changed'),
                                 ('socket', [10, 21], 'identity_socket_changed')):
            obs, idref = observer(FakeDocker()); obs.entry(0)
            idref['v'] = dict(ident(), **{key: val})
            self.assertEqual(obs.tick(1000, ['GATE88']), 'stop')
            self.assertEqual(obs.violation, reason)

    def test_container_and_image_swap_after_binding(self):
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        doc = d.docs.pop(CID['runtipi-db']); doc['Id'] = '7' * 64; d.docs[doc['Id']] = doc
        obs.tick(1000, ['GATE88']); self.assertEqual(obs.violation, 'identity_container_swap')
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        d.docs[CID['runtipi-queue']]['Image'] = 'sha256:' + '8' * 64
        obs.tick(1000, ['GATE88']); self.assertEqual(obs.violation, 'identity_image_swap')
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        del d.docs[CID['runtipi-reverse-proxy']]
        obs.tick(1000, ['GATE88']); self.assertEqual(obs.violation, 'identity_container_vanished')

    def test_new_manager_must_be_pinned_and_upstream_manager_recorded(self):
        d = FakeDocker(); del d.docs[CID['runtipi']]
        d.docs[OLD_MGR] = cdoc('runtipi', cid=OLD_MGR, image=UPSTREAM_MGR, status='exited', health='healthy')
        obs, _ = observer(d); obs.entry(0)
        self.assertEqual(obs.pre_manager_id, OLD_MGR); self.assertIsNone(obs.bound['runtipi'])
        d.docs[CID['runtipi']] = cdoc('runtipi', image='sha256:' + '5' * 64)
        obs.tick(1000, ['GATE116']); self.assertEqual(obs.violation, 'identity_manager_image_not_pinned')
        ran, row = C.upstream_manager(obs, PIN)
        self.assertTrue(ran); self.assertFalse(row['image_is_pinned_backend']); self.assertEqual(row['id'], OLD_MGR)

    def test_lifecycle_and_restart_regression(self):
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        d.docs[CID['runtipi-db']]['RestartCount'] = 2; obs.tick(1000, ['GATE88'])
        d.docs[CID['runtipi-db']]['RestartCount'] = 1; obs.tick(2000, ['GATE88'])
        self.assertEqual(obs.violation, 'identity_lifecycle_regression')
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        d.docs[CID['runtipi-db']]['State']['StartedAt'] = TS0; obs.tick(1000, ['GATE88'])
        self.assertEqual(obs.violation, 'identity_lifecycle_regression')
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        d.docs[CID['runtipi-db']]['Created'] = TS1; obs.tick(1000, ['GATE88'])
        self.assertEqual(obs.violation, 'identity_lifecycle_regression')
        with self.assertRaises(C.Violation):
            C.ts_ns('2026-10-31 00:00:00')


class IncidentsAndCauses(unittest.TestCase):
    def run_to_incident(self, service, mutate, gates=('GATE88', 'GATE116')):
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        mutate(d.docs[CID[service]])
        self.assertEqual(obs.tick(1000, list(gates)), 'stop')
        return d, obs

    def unhealthy(self, code, streak=20):
        def m(doc):
            doc['State']['Health'] = {'Status': 'unhealthy', 'FailingStreak': streak, 'Log': [
                {'Start': TS1, 'End': TS2, 'ExitCode': code, 'Output': 'PRIVATE'} for _ in range(20)]}
        return m

    def test_db_unhealthy_is_gate88(self):
        d, obs = self.run_to_incident('runtipi-db', self.unhealthy(1))
        self.assertEqual(obs.incident['gate'], 'GATE88')
        self.assertEqual(C.cause_class(obs.incident, None), 'H1_DB_HEALTH_FAILING')
        self.assertEqual(len(obs.incident['snapshot']['health_log']), 5)

    def test_manager_unhealthy_http_503_vs_connect(self):
        d, obs = self.run_to_incident('runtipi', self.unhealthy(22))
        self.assertTrue(C.needs_probe(obs.incident))
        self.assertEqual(C.cause_class(obs.incident, C.probe_result(22, b'503')), 'H3_MANAGER_HTTP_503')
        self.assertFalse(C.needs_tail('H3_MANAGER_HTTP_503'))
        self.assertEqual(C.cause_class(obs.incident, C.probe_result(22, b'500')), 'H3_MANAGER_HTTP_ERROR')
        d, obs = self.run_to_incident('runtipi', self.unhealthy(7))
        self.assertFalse(C.needs_probe(obs.incident))
        self.assertEqual(C.cause_class(obs.incident, None), 'H2_MANAGER_CONNECT_FAILED')
        d, obs = self.run_to_incident('runtipi', self.unhealthy(28))
        self.assertEqual(C.cause_class(obs.incident, None), 'H2_MANAGER_CHECK_TIMEOUT')
        d, obs = self.run_to_incident('runtipi', lambda doc: doc['State'].update(Status='exited', Running=False, ExitCode=137))
        self.assertEqual(C.cause_class(obs.incident, None), 'H2_MANAGER_EXITED')
        self.assertEqual(obs.incident['snapshot']['signal_inferred'], 'SIGKILL')
        self.assertEqual(C.probe_result(7, b'000'), {'curl_exit': 7, 'http_status': None})
        self.assertEqual(C.probe_result(0, b'200 <html>secret')['http_status'], None)

    def test_oom_and_unarmed_pre_gate_state(self):
        d, obs = self.run_to_incident('runtipi-db', lambda doc: doc['State'].update(OOMKilled=True, Status='exited'))
        self.assertEqual(C.cause_class(obs.incident, None), 'H1_DB_OOM')
        d = FakeDocker(); obs, _ = observer(d); obs.entry(0)
        self.unhealthy(1)(d.docs[CID['runtipi-db']])
        self.assertEqual(obs.tick(1000, []), 'continue')        # gate 88 not yet running
        self.assertEqual(obs.unarmed_states['runtipi-db'], 1)
        self.assertEqual(obs.tick(2000, ['GATE88']), 'stop')

    def test_first_incident_frozen(self):
        d, obs = self.run_to_incident('runtipi-db', self.unhealthy(1))
        first = json.dumps(obs.incident, sort_keys=True)
        d.docs[CID['runtipi-db']] = cdoc('runtipi-db', restart=1, started=TS2)
        self.unhealthy(7)(d.docs[CID['runtipi']])
        self.assertEqual(obs.tick(2000, ['GATE116']), 'stop')
        self.assertEqual(json.dumps(obs.incident, sort_keys=True), first)
        final, changed = obs.final(3000)
        self.assertEqual(final['runtipi-db']['health'], 'healthy'); self.assertFalse(changed)
        self.assertEqual(obs.incident['snapshot']['health'], 'unhealthy')

    def test_hypotheses_and_classification(self):
        d, obs = self.run_to_incident('runtipi-db', self.unhealthy(1))
        name, reason = C.classify({'incident': obs.incident, 'install': {'return': 'exception'}})
        self.assertEqual((name, reason), ('REPRODUCED_GATE88_RUNTIPI_DB', 'first_incident_frozen'))
        h = C.hypotheses(name, obs.incident, 'H1_DB_HEALTH_FAILING', {}, None, None, obs.basis)
        self.assertEqual(h['H1'], 'supported'); self.assertEqual(h['H5'], 'persistent_within_window')
        self.assertEqual(C.classify({'install': {'return': 'healthy'}, 'basis_mismatch': [False]}),
                         ('NOT_REPRODUCED', 'line291_returned_healthy'))
        self.assertEqual(C.classify({'install': {'return': 'healthy'}, 'basis_mismatch': [True]})[0], 'INSUFFICIENT')
        self.assertEqual(C.classify({'install': {'return': 'exception'}, 'deadline_hit': True}),
                         ('DEADLINE', 'deadline_reached'))
        self.assertNotIn('FIXED', ' '.join(C.RESULTS))


class DeadlineBasis(unittest.TestCase):
    def test_comparator_675_and_refusals(self):
        basis, refusal = C.deadline_basis(config(), set())
        self.assertIsNone(refusal)
        self.assertEqual((basis['gate88_s'], basis['gate116_s'], basis['total_s']), (305, 310, 675))
        self.assertEqual(C.deadline_basis(config(**{'runtipi-db': {}}), set())[1], 'basis_unparseable')
        bad = config(); bad['services']['runtipi']['healthcheck']['interval'] = '${INTERVAL}'
        self.assertEqual(C.deadline_basis(bad, set())[1], 'basis_unparseable')
        slow = config(); slow['services']['runtipi-db']['healthcheck']['retries'] = 120
        self.assertEqual(C.deadline_basis(slow, set())[1], 'basis_over_cap')
        self.assertEqual(C.deadline_basis(config(**{'wizard-release-index': {}}), set())[1], 'scope_release_index_present')
        self.assertEqual(C.deadline_basis(config(), {'WIZARD_RELEASE_PINS'})[1], 'scope_release_index_present')
        miss = config(); del miss['services']['runtipi-queue']
        self.assertEqual(C.deadline_basis(miss, set())[1], 'scope_services_missing')
        self.assertEqual(C.deadline_basis(None, set())[1], 'basis_render_failed')
        self.assertEqual(C.parse_duration('1m30s'), 90); self.assertEqual(C.parse_duration('500ms'), 0.5)
        self.assertIsNone(C.parse_duration('5 s')); self.assertIsNone(C.parse_duration(True))
        self.assertEqual(C.inspect_schedule(DB_HC)['budget_s'], 305)
        self.assertEqual(C.inspect_schedule({'Test': ['CMD', 'x']})['budget_s'], 3 * 60)
        self.assertEqual(C.inspect_schedule(dict(DB_HC, Interval=1)), 'bad')
        self.assertIsNone(C.inspect_schedule({'Test': ['NONE']}))

    def test_inherited_image_healthcheck_is_a_mismatch(self):
        d = FakeDocker(); d.docs[CID['runtipi-queue']] = cdoc('runtipi-queue', hc=DB_HC)
        obs, _ = observer(d); obs.entry(0); obs.tick(1000, ['GATE88'])
        rows = obs.compare_healthchecks()
        self.assertFalse(rows['runtipi-queue']['timing_matches_rendered'])
        self.assertTrue(obs.basis_mismatch['runtipi-queue'])
        self.assertTrue(rows['runtipi-db']['test_matches_comparator'])
        self.assertNotIn('pg_isready', json.dumps(rows))


class LogParser(unittest.TestCase):
    def test_codes_and_unknowns(self):
        raw = b''.join(frame(t) for t in (
            "Error: connect ECONNREFUSED 172.18.0.3:5672\n",
            "error: relation \"app\" does not exist { code: '42P01' }\n",
            "2026-10-31 00:00:00.000 UTC [1] FATAL:  database \"tipi\" does not exist\n",
            "Error migrating database: x\n",
            "{\"level\":\"error\",\"code\":\"ZZ999\",\"msg\":\"weird\"}\n",
            "plain line without codes\n"))
        out = C.parse_log(raw, truncated=False)
        self.assertEqual(out['codes']['ERRNO_ECONNREFUSED'], 1)
        self.assertEqual(out['codes']['SQLSTATE_42P01'], 1)
        self.assertEqual(out['codes']['PG_FATAL'], 1)
        self.assertEqual(out['codes']['BACKEND_MIGRATION_ERROR'], 1)
        self.assertEqual(out['unknown_code_count'], 1); self.assertTrue(out['insufficient'])
        self.assertEqual(out['unmatched_lines'], 1)
        self.assertNotIn('tipi', json.dumps(out)); self.assertNotIn('172.18', json.dumps(out))

    def test_secret_control_forged_nested_rejection(self):
        raw = b''.join(frame(t) for t in (
            "password=hunter2 ECONNREFUSED\n", "postgres://tipi:pw@db/tipi EACCES\n",
            "Authorization: Bearer abcdef\n", "ok \x07 bell EACCES\n", "\x1b]0;title\x07 EPERM\n",
            '{"result":"NOT_REPRODUCED","code":"EACCES"}\n', '{"a":{"b":{"code":"EACCES"}}}\n',
            '["EACCES"]\n', "A" * 9000 + "\n", "token: " + "Z" * 50 + "\n"))
        raw += frame(b'\xff\xfe invalid utf8 EACCES\n')
        out = C.parse_log(raw, truncated=False)
        self.assertEqual(out['codes'], {})
        self.assertEqual(out['rejected']['secret'], 4)
        self.assertEqual(out['rejected']['control'], 2)
        self.assertEqual(out['rejected']['forged'], 3)
        self.assertEqual(out['rejected']['overlong'], 1); self.assertEqual(out['rejected']['undecodable'], 1)
        self.assertEqual(C._depth({'a': 1}), 1); self.assertEqual(C._depth({'a': {'b': 1}}), 2)

    def test_byte_and_line_caps_and_partial_frames(self):
        many = b''.join(frame('Error: connect ECONNREFUSED x\n') for _ in range(200))
        out = C.parse_log(many, truncated=False)
        self.assertEqual(out['lines'], 64); self.assertEqual(out['codes']['ERRNO_ECONNREFUSED'], 64)
        big = frame('x' * 60000 + '\n') + frame('EACCES tail cut')
        out = C.parse_log(big + b'Z' * 70000, truncated=True)
        self.assertLessEqual(out['bytes'], 65536); self.assertTrue(out['truncated'])
        out = C.parse_log(b'not a docker stream at all\n', truncated=False)
        self.assertFalse(out['stream_valid']); self.assertTrue(out['insufficient'])


class Migrations(unittest.TestCase):
    def allow(self):
        pinned = [{'tag': '0000_a', 'sha256': '0' * 64, 'when': 1}, {'tag': '0031_editable_apps', 'sha256': '3' * 64, 'when': 31}]
        upstream = pinned + [{'tag': '0032_app_migrations', 'sha256': '4' * 64, 'when': 32}]
        return C.migration_allowlist(pinned, upstream)

    def test_membership_and_unknown_count_only(self):
        a = json.loads(json.dumps(self.allow()))       # sealed JSON form: lists, not tuples
        rows = ('0' * 64 + ' 1\n' + '3' * 64 + ' 31\n' + '4' * 64 + ' 32\n' + 'f' * 64 + ' 99\n').encode()
        out = C.migration_membership('read', 0, rows, a)
        self.assertEqual(out['membership']['0032_app_migrations'], {'pinned': None, 'upstream_v4_11_0': True})
        self.assertEqual(out['unknown_count'], 1); self.assertTrue(out['order_consistent'])
        self.assertNotIn('f' * 64, json.dumps(out))
        out['allowlist_sha256'] = '0' * 64
        C._migrations(out, 'm')
        h = C.hypotheses('NOT_REPRODUCED', None, None, {}, out, None, None)
        self.assertEqual(h['H4'], 'precondition_present')
        self.assertEqual(C.migration_membership('read', 0, b'DROP TABLE x;\n', a)['state'], 'unreadable')
        self.assertEqual(C.migration_membership('read', 0, b'x' * 70000, a)['state'], 'unreadable')

    def test_fixed_query_argv_only(self):
        self.assertNotIn('{', G.MIGRATION_ROWS); self.assertIn('LIMIT 257', G.MIGRATION_ROWS)
        self.assertTrue(G.PSQL.startswith('exec psql -X -q'))


class GuestHarness(unittest.TestCase):
    """Runs the REAL guest install() profile path over a synthetic helper (no Docker)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hc-fixture-'); t = Path(self.tmp.name)
        self.saved = {k: getattr(G, k) for k in ('ROOT', 'TREE', 'CHECKOUT', 'RUNTIPI', 'API', 'identity', 'run',
                                                  'PRE291_SECONDS', 'report')}
        self.saved_c = {k: getattr(C, k) for k in ('ALLOWANCE_SAMPLING', 'ALLOWANCE_POST', 'INCIDENT_GRACE')}
        self.env = dict(os.environ); self.argv = sys.argv[:]
        G.ROOT = t / 'root'; G.ROOT.mkdir(); G.TREE = t / 'tree'; (G.TREE / 'scripts').mkdir(parents=True)
        G.CHECKOUT = t / 'checkout'; G.CHECKOUT.mkdir(); G.RUNTIPI = t / 'runtipi'
        (G.RUNTIPI / 'user-config').mkdir(parents=True)
        (G.RUNTIPI / 'user-config/tipi-compose.yml').write_text('services: {}\n')
        (G.RUNTIPI / 'docker-compose.yml').write_text('services: {}\n')
        self.docker = FakeDocker(); G.API = self.docker; G.API.raw = self.docker.raw
        G.identity = lambda: ident()
        self.commands = []
        def fake_run(label, args, timeout=60, required=True, cap=4194304):
            self.commands.append([str(a) for a in args])
            if label == 'render-merged-config':
                return 0, json.dumps(self.config).encode()
            if label == 'fixed-health-probe':
                return 22, b'503'
            if label == 'migration-table-exists':
                return 0, b't\n'
            if label == 'migration-rows':
                return 0, ('0' * 64 + ' 1\n').encode()
            raise AssertionError('unexpected guest command ' + label)
        G.run = fake_run
        G.report = {'schema': 'x', 'checks': [], 'first_error': None, 'commands': [], 'cleanup_errors': [], 'phases': []}
        self.config = config()
        allow = C.migration_allowlist([{'tag': '0000_a', 'sha256': '0' * 64, 'when': 1}], [])
        allow = json.loads(json.dumps(allow))
        (G.ROOT / 'comparator.json').write_text(json.dumps({
            'migration_allowlist': allow, 'setup_sha256': '0' * 64, 'cli_v4_11_0_binary_sha256': '0' * 64,
            'cli_template_sha256': '0' * 64,
            'migration_allowlist_sha256': C.sha(json.dumps(allow, sort_keys=True, separators=(',', ':')).encode())}))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(G, k, v)
        for k, v in self.saved_c.items():
            setattr(C, k, v)
        os.environ.clear(); os.environ.update(self.env); sys.argv = self.argv
        self.tmp.cleanup()

    def helper(self, install_cmd, post_marker=True):
        sysenv = {'PATH': '/usr/bin', 'HOME': '/var/lib/wizard-os', 'WIZARD_PRIVATE_ACCESS': 'none', 'WAM_IMAGE': PIN,
                  'WIZARD_AGENT_SERVICE_IMAGE': 'sha256:' + '6' * 64, 'USER': 'wizard-system', 'LOGNAME': 'wizard-system',
                  'RUNTIPI_ROOT': str(G.RUNTIPI), 'WIZARD_OS_HOME': '/s', 'WIZARD_MODE': 'alongside',
                  'WIZARD_SEPARATED': '1', 'WIZARD_TAILSCALE_WAIT': '1'}
        marker = G.ROOT.parent / 'post-291-ran'
        src = ('import subprocess, sys\nfrom pathlib import Path\n'
               'def run(argv, **kwargs):\n'
               '    if argv[-1:] == [' + repr(str(G.CHECKOUT / 'install.sh')) + ']:\n'
               '        return subprocess.run(' + repr(install_cmd) + ', check=True)\n'
               '    return subprocess.run([str(v) for v in argv], check=True, **kwargs)\n'
               'def main():\n'
               '    run(["true"])\n'
               '    run(["runuser", "-u", "wizard-system", "--", "env", "-i", *' + repr([k + '=' + v for k, v in sysenv.items()]) +
               ', "bash", ' + repr(str(G.CHECKOUT / 'install.sh')) + '])\n'
               '    Path(' + repr(str(marker)) + ').write_text("post")\n'
               'if __name__ == "__main__":\n    main()\n')
        (G.TREE / 'scripts/prepare-alongside.py').write_text(src)
        return marker

    def install(self):
        doc = G.install('0' * 40, PIN, 'sha256:' + '6' * 64)
        C.validate_export(doc)
        exported = json.dumps(doc)
        for private in ('PRIVATE', 'hunter2', 'POSTGRES_PASSWORD', 'never', 'pg_isready', 'SECRET=1', 'localhost'):
            self.assertNotIn(private, exported)
        return doc

    def test_healthy_not_reproduced_stops_before_post_291(self):
        marker = self.helper([sys.executable, '-c', 'pass'])
        doc = self.install()
        self.assertEqual(doc['result'], 'NOT_REPRODUCED'); self.assertEqual(doc['install']['return'], 'healthy')
        self.assertEqual(doc['install']['exception_type'], 'HealthyStop')
        self.assertFalse(marker.exists(), 'post-line-291 helper action ran')
        self.assertEqual(doc['migrations']['state'], 'read')
        self.assertEqual(doc['deadline']['basis']['total_s'], 675)

    def test_original_exception_survives_collector_failure(self):
        self.helper([sys.executable, '-c', 'raise SystemExit(3)'])
        def broken(self_):
            raise RuntimeError('collector exploded')
        orig = G.Capture.post; G.Capture.post = broken
        try:
            doc = self.install()
        finally:
            G.Capture.post = orig
        self.assertEqual(doc['install']['exception_type'], 'CalledProcessError')
        self.assertEqual(doc['install']['returncode'], 3)
        self.assertIn('collector_internal_error', doc['collector_errors'])
        self.assertEqual(G.report['first_error'], {'type': 'CalledProcessError', 'phase': 'install'})
        self.assertEqual(G.report['original_install']['exception_type'], 'CalledProcessError')

    def test_manager_incident_one_probe_no_arbitrary_health_command(self):
        self.helper([sys.executable, '-c', 'import time; time.sleep(0.8); raise SystemExit(1)'])
        self.docker.docs[CID['runtipi']]['Config']['Healthcheck'] = EVIL_HC
        self.docker.docs[CID['runtipi']]['State']['Health'] = {'Status': 'unhealthy', 'FailingStreak': 20, 'Log': [
            {'Start': TS1, 'End': TS2, 'ExitCode': 22, 'Output': 'PRIVATE'}]}
        C.INCIDENT_GRACE = 5
        doc = self.install()
        self.assertEqual(doc['result'], 'REPRODUCED_GATE116_RUNTIPI')
        self.assertEqual(doc['cause_class'], 'H3_MANAGER_HTTP_503')
        self.assertEqual(doc['probe'], {'curl_exit': 22, 'http_status': 503})
        self.assertIsNone(doc['log_tail']); self.assertEqual(self.docker.log_calls, 0)
        flat = [' '.join(c) for c in self.commands]
        self.assertFalse(any('pwned' in c or 'shadow' in c for c in flat))
        self.assertEqual(sum(1 for c in self.commands if c[-1] == 'http://localhost:3000/api/health'), 1)
        self.assertFalse(doc['basis_crosscheck']['runtipi']['test_matches_rendered'])
        self.assertEqual(doc['install']['exception_type'], 'CalledProcessError')

    def test_db_incident_one_tail_identity_rechecked(self):
        self.helper([sys.executable, '-c', 'import time; time.sleep(0.8); raise SystemExit(1)'])
        self.docker.docs[CID['runtipi-db']]['State'].update(Status='exited', Running=False, ExitCode=1)
        self.docker.logs = frame('FATAL:  password authentication failed for user "tipi"\n') + \
            frame("Error: connect ECONNREFUSED 1.2.3.4:5432\n")
        doc = self.install()
        self.assertEqual(doc['result'], 'REPRODUCED_GATE88_RUNTIPI_DB')
        self.assertEqual(self.docker.log_calls, 1)
        self.assertTrue(doc['log_tail']['identity_rechecked'])
        self.assertEqual(doc['log_tail']['codes'], {'ERRNO_ECONNREFUSED': 1, 'PG_AUTH_FAILED': 1, 'PG_FATAL': 1})

    def test_unknown_output_stops_further_reads(self):
        self.helper([sys.executable, '-c', 'import time; time.sleep(0.8); raise SystemExit(1)'])
        self.docker.docs[CID['runtipi-db']]['State'].update(Status='exited', Running=False, ExitCode=1)
        self.docker.logs = frame('{"code":"QQ123","x":1}\n') + frame('mystery output\n')
        doc = self.install()
        self.assertEqual(self.docker.log_calls, 1)
        self.assertTrue(doc['log_tail']['insufficient']); self.assertEqual(doc['log_tail']['unknown_code_count'], 1)
        self.assertEqual(doc['log_tail']['codes'], {}); self.assertIsNone(doc['probe'])
        self.assertEqual(doc['cause_class'], 'H1_DB_EXITED')       # structural class only, never a guess
        self.assertNotIn('QQ123', json.dumps(doc)); self.assertNotIn('mystery', json.dumps(doc))

    def test_cleanup_failure_never_displaces_original_install_error(self):
        G.report = {'schema': 'x', 'checks': [{'name': 'fresh-synthetic-inputs', 'passed': True}], 'commands': [],
                    'first_error': {'type': 'CalledProcessError', 'phase': 'install'}, 'cleanup_errors': [],
                    'phases': ['fresh', 'install']}
        (G.ROOT / 'native.json').write_text(json.dumps(G.report))
        saved = G.cleanup
        def failing():
            raise RuntimeError('cleanup exploded')
        G.cleanup = failing; sys.argv = ['guest.py', 'cleanup']
        try:
            with self.assertRaises(RuntimeError):
                G.main()
        finally:
            G.cleanup = saved
        final = json.loads((G.ROOT / 'native.json').read_text())
        self.assertEqual(final['first_error'], {'type': 'CalledProcessError', 'phase': 'install'})
        self.assertEqual(final['cleanup_errors'], ['RuntimeError'])
        # a healthy install followed by failed cleanup still keeps first_error empty
        G.report = dict(final, first_error=None, cleanup_errors=[]); (G.ROOT / 'native.json').write_text(json.dumps(G.report))
        G.cleanup = failing
        try:
            with self.assertRaises(RuntimeError):
                G.main()
        finally:
            G.cleanup = saved
        final = json.loads((G.ROOT / 'native.json').read_text())
        self.assertIsNone(final['first_error']); self.assertEqual(final['cleanup_errors'], ['RuntimeError'])

    def test_hard_deadline_quiesces_and_no_second_attempt(self):
        C.ALLOWANCE_SAMPLING = 1; C.ALLOWANCE_POST = 1
        self.config = config(**{'runtipi-db': {'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -d tipi -U tipi'],
                         'interval': '1s', 'timeout': '1s', 'retries': 1, 'start_period': '0s'}},
                         'runtipi': {'healthcheck': {'test': ['CMD', 'curl', '-f', 'http://localhost:3000/api/health'],
                         'interval': '1s', 'timeout': '1s', 'retries': 1, 'start_period': '0s'}}})
        self.helper([sys.executable, '-c', 'import time; time.sleep(60)', 'compose', 'up', '-d', '--no-recreate',
                     '--wait', 'runtipi-db', 'runtipi-queue'])
        self.docker.docs[CID['runtipi-db']]['State']['Health']['Status'] = 'starting'
        for s, test in (('runtipi-db', DB_HC['Test']), ('runtipi', MGR_HC['Test'])):
            self.docker.docs[CID[s]]['Config']['Healthcheck'] = {'Test': test, 'Interval': 10 ** 9, 'Timeout': 10 ** 9,
                                                                 'StartPeriod': 0, 'Retries': 1}
        t = time.monotonic(); doc = self.install()
        self.assertLess(time.monotonic() - t, 30)
        self.assertEqual(doc['result'], 'DEADLINE'); self.assertTrue(doc['install']['quiesced'])
        self.assertIsNotNone(doc['gates_seen_ms']['GATE88'])
        self.assertLessEqual(doc['ticks'], doc['deadline']['max_ticks'])
        # main(): a second installer attempt in the same guest is refused.
        G.report['phases'] = ['fresh', 'install']; G.report['checks'] = [{'name': 'fresh-synthetic-inputs', 'passed': True}]
        self.assertIn('install', G.report['phases'][:-1] + ['install'])
        src = GUEST.read_text()
        self.assertIn("assert 'install' not in report['phases'][:-1], 'second installer attempt refused'", src)

    def test_entry_refusals_happen_before_install_starts(self):
        marker = G.ROOT.parent / 'install-started'
        self.helper([sys.executable, '-c', 'open(%r, "w").write("x")' % str(marker)])
        self.config = config(**{'wizard-release-index': {}})
        doc = self.install()
        self.assertEqual((doc['result'], doc['result_reason']), ('INSUFFICIENT', 'scope_release_index_present'))
        self.assertFalse(marker.exists())
        self.assertIsNone(G.entry_env(['runuser', '-u', 'wizard-system', '--', 'env', '-i', 'X=1', 'bash',
                                       str(G.CHECKOUT / 'install.sh')], PIN))

    def test_api_byte_cap(self):
        api = G.Api(); api.raw = lambda path, cap, method='GET': (200, b'{}', True)
        with self.assertRaises(C.Violation) as e:
            api('/info', 10)
        self.assertEqual(e.exception.reason, 'identity_response_bound')


class ExactCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hc-cleanup-'); self.saved = {k: getattr(G, k) for k in
                                                                                     ('ROOT', 'TREE', 'module', 'run', 'report', 'orphan_installers')}
        G.ROOT = Path(self.tmp.name); G.report = {'checks': [], 'first_error': None, 'commands': [], 'cleanup_errors': [], 'phases': []}
        (G.ROOT / 'service-attestation-before.json').write_text(json.dumps({'engine_image_id': 'sha256:' + '6' * 64}))
        G.orphan_installers = lambda: []
        self.containers = {}; self.neg = '9' * 64; self.owned = ['a' * 64, 'b' * 64, 'c' * 64]
        for c in self.owned:
            self.containers[c] = {'Id': c, 'owned': True}
        def fake_run(label, args, timeout=60, required=True, cap=4194304):
            if label == 'create-unowned-control':
                self.containers[self.neg] = {'Id': self.neg, 'owned': False}
                return 0, (self.neg + '\n').encode()
            return 0, b'not-found\n'
        G.run = fake_run
        test = self
        class FakeProduction:
            ROOT = None
            @staticmethod
            def daemon_request(method, path):
                if path == '/info':
                    return 200, {'ID': 'ENGINE'}
                cid = path.split('/')[2].split('?')[0]
                if method == 'DELETE':
                    test.containers.pop(cid); return 204, None
                return (200, dict(test.containers[cid])) if cid in test.containers else (404, {'message': 'gone'})
            @staticmethod
            def cleanup_containers():
                attempted = [c for c, v in test.containers.items() if v['owned']]
                unowned = [c for c, v in test.containers.items() if not v['owned']]
                if test.remove_negative:
                    unowned = []; test.containers.pop(test.neg, None)
                for c in attempted:
                    test.containers.pop(c)
                rep = {'attempted_exact_ids': attempted, 'absent_exact_ids': attempted, 'unowned_exact_ids': unowned,
                       'passed': not unowned, 'first_error_type': 'AssertionError' if unowned else None,
                       'readback_absent': True}
                (G.ROOT / 'container-cleanup.json').write_text(json.dumps(rep))
                if unowned:
                    raise AssertionError('unowned container present')
        G.module = lambda name, path: FakeProduction
        self.remove_negative = False

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(G, k, v)
        self.tmp.cleanup()

    def test_unowned_negative_survives_then_exact_absence(self):
        G.cleanup()
        self.assertEqual(self.containers, {})
        r = G.report['exact_cleanup']
        self.assertTrue(r['negative_survived_owned_cleanup'] and r['negative_removed_by_full_id_absent'])
        self.assertEqual(r['owned_attempted'], 3)

    def test_cleanup_that_removes_unowned_negative_fails(self):
        self.remove_negative = True
        with self.assertRaises(AssertionError):
            G.cleanup()


class ExportSchema(unittest.TestCase):
    def test_closed_schema_rejects_extra_and_raw(self):
        obs, _ = observer(FakeDocker()); obs.entry(0); obs.tick(1000, ['GATE88'])
        st = {'install': {'return': 'healthy', 'exception_type': 'HealthyStop', 'returncode': None, 'line291_reached': True,
                          'pre291_command': 'install-approval-custody.py', 'quiesced': False},
              'allowlist_sha256': '0' * 64, 'collector_errors': [], 'final': obs.final(2000)[0]}
        doc = C.assemble(obs, st)
        self.assertEqual(doc['result'], 'NOT_REPRODUCED')
        for mutate in (lambda d: d.update(extra=1), lambda d: d['final_snapshot']['runtipi-db'].update(error='x'),
                       lambda d: d.update(collector_errors=['free text']), lambda d: d.update(result='FIXED'),
                       lambda d: d['final_snapshot']['runtipi'].update(id='abc')):
            bad = json.loads(json.dumps(doc)); mutate(bad)
            with self.assertRaises(C.SchemaError):
                C.validate_export(bad)


class CapacityEnforcement(unittest.TestCase):
    def test_monitor_kills_vm_when_stage_ceiling_exceeded(self):
        R = load('hc_runner_fixture', RUNNER)
        class Base:
            class Runner:
                def __init__(self, stage):
                    self.stage = stage; self.receipt = {}; self.proc = None
                def save(self):
                    pass
        with tempfile.TemporaryDirectory(prefix='hc-cap-') as t:
            stage = Path(t)
            saved = (R.MAX_NEW, R.MONITOR_SECONDS); R.MAX_NEW = 4096; R.MONITOR_SECONDS = 0.05
            try:
                r = make = R.make_runner(Base, None)(stage)
                r.abort = None; r.monitor_state = {}
                r.proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
                (stage / 'blob').write_bytes(os.urandom(65536))
                stop = threading.Event(); th = threading.Thread(target=r.monitor, args=(stop,)); th.start()
                th.join(timeout=10); stop.set()
                self.assertEqual(r.abort, 'stage-ceiling'); self.assertIsNotNone(r.proc.wait(timeout=10))
                with self.assertRaises(AssertionError):
                    r.capacity()
            finally:
                R.MAX_NEW, R.MONITOR_SECONDS = saved
        self.assertEqual(R.START_FLOOR, 285781630976); self.assertEqual(R.RUN_FLOOR, 277191696384)


if __name__ == '__main__':
    result = unittest.main(argv=[sys.argv[0], '-v'], exit=False).result
    print(json.dumps({'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                      'skipped': len(result.skipped)}))
    sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
