#!/usr/bin/env python3
"""Synthetic fixtures for the finite manager-start discriminator and its v2 trace.

No Docker, VM, network, sudo or real home. Runs in the native job BEFORE the VM
boots (stage layout: tests.py beside collector.py/hook.mjs/source.tar) or from
the repo. Any failure (or skip) aborts the native job. The /diag preload is
exercised with a local `node` against a fake /app/main.mjs in a temp dir.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
if (HERE / 'collector.py').is_file():
    COLLECTOR, GUEST, HOOK, LOADER = (HERE / n for n in ('collector.py', 'guest.py', 'hook.mjs', 'loader.mjs'))
    SOURCE_TAR = HERE / 'source.tar'; REPO = None
else:
    Q = HERE.parent / 'qa/privsep-storage'
    COLLECTOR, GUEST, HOOK, LOADER = (Q / n for n in ('manager_start_collector.py', 'manager_start_guest.py',
                                                      'manager_start_hook.mjs', 'manager_start_loader.mjs'))
    SOURCE_TAR = None; REPO = HERE.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


C = load('ms_collector_under_test', COLLECTOR)
HC = C.HC

FAKE_MAIN = r'''
import fs from 'node:fs'; import http from 'node:http';
const mode = process.env.MSQ_FAKE_MODE;
async function boot() {
  await fs.promises.writeFile(process.env.MSQ_DIAG_ENV_FOR_TESTS, 'SYNTHETIC=1\n');
  if (mode === 'exit_before_nest') { process.exit(0); }
  Reflect.getMetadata('imports', {});
  if (mode === 'drain') { await new Promise(() => {}); }
  if (mode === 'exit0') { process.exit(0); }
  if (mode === 'throw') { setTimeout(() => { const e = new TypeError('synthetic'); e.code = 'not-a-code'; throw e; }, 1); return; }
  if (mode === 'reject') { const e = new Error('synthetic'); e.code = 'ECONNREFUSED'; Promise.reject(e); return; }
  const app = function (req, res) { res.end('ok'); }; app.set = function () { return this; };
  const srv = http.createServer(app); app.set('trust proxy', true);
  srv.listen(Number(process.env.MSQ_FAKE_PORT), () => srv.close());
}
boot().catch((e) => { console.error(e); process.exit(1); });
'''
REFLECT_SHIM = "Reflect.getMetadata = function getMetadata(k) { return undefined; };\n"


def run_hook(mode, port=3000, main=None, shim=True):
    """Fake app under the real preload. Reflect.getMetadata exists BEFORE main evaluates (like reflect-metadata)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        for d in ('diag', 'out', 'data'):
            (t / d).mkdir()
        shutil.copyfile(HOOK, t / 'diag/hook.mjs'); shutil.copyfile(LOADER, t / 'diag/loader.mjs')
        (t / 'shim.mjs').write_text(REFLECT_SHIM if shim else '')
        (t / 'main.mjs').write_text(FAKE_MAIN if main is None else main)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'MSQ_FAKE_MODE': mode, 'MSQ_FAKE_PORT': str(port),
               'MSQ_DIAG_OUT_FOR_TESTS': str(t / 'out'), 'MSQ_DIAG_ENV_FOR_TESTS': str(t / 'data/.env'),
               'MSQ_DIAG_MAIN_FOR_TESTS': (t / 'main.mjs').as_uri(),
               'NODE_OPTIONS': '--import=' + str(t / 'shim.mjs') + ' --import=' + str(t / 'diag/hook.mjs')}
        p = subprocess.run(['node', str(t / 'main.mjs')], env=env, capture_output=True, timeout=60, cwd=tmp)
        files = sorted((t / 'out').iterdir())
        rows = [C.validate_diag(f.read_bytes()) for f in files]
        return p.returncode, p.stdout, p.stderr, rows, [f.name for f in files]


def free_port():
    import socket
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close(); return port


class Preload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=30).stdout.strip()
        assert out.startswith('v') and int(out[1:].split('.')[0]) >= 20, 'node >= 20 required for module.register'

    def test_drain_is_event_loop_drained_exit0_silent(self):
        rc, out, err, rows, names = run_hook('drain')
        self.assertEqual((rc, out, err, names), (0, b'', b'', ['diag-0.json']))
        r = rows[0]
        self.assertEqual(r['milestones'], ['preload_loaded', 'main_module_evaluated', 'env_file_generated',
                                           'nest_create_started'])
        self.assertTrue(r['before_exit_fired'] and r['exit_fired'] and r['exit_code'] == 0)
        self.assertEqual((r['process_exit_calls'], r['uncaught_count'], r['hook_errors']), ([], 0, 0))
        self.assertEqual((r['loader_hook'], r['reflect_hook']), ('registered', 'armed'))
        self.assertEqual(C.diag_mechanism(rows), 'EVENT_LOOP_DRAINED')

    def test_explicit_exit0_is_recorded_and_unchanged(self):
        rc, out, err, rows, _ = run_hook('exit0')
        self.assertEqual((rc, out, err), (0, b'', b''))
        self.assertEqual(rows[0]['process_exit_calls'], [0]); self.assertFalse(rows[0]['before_exit_fired'])
        self.assertEqual(C.diag_mechanism(rows), 'PROCESS_EXIT_CALLED')
        rc, _, _, rows, _ = run_hook('exit_before_nest')
        self.assertEqual(rc, 0); self.assertEqual(rows[0]['last_milestone'], 'env_file_generated')

    def test_uncaught_exception_keeps_node_semantics(self):
        rc, out, err, rows, _ = run_hook('throw')
        self.assertNotEqual(rc, 0); self.assertTrue(err)       # Node's own report, unchanged by the preload
        u = rows[0]['uncaught_first']
        self.assertEqual((u['ctor'], u['code'], u['origin']), ('TypeError', 'OTHER', 'uncaughtException'))
        self.assertEqual(C.diag_mechanism(rows), 'UNCAUGHT')
        self.assertNotIn(b'synthetic', json.dumps(rows).encode())

    def test_unhandled_rejection_observed_without_listener(self):
        rc, _, err, rows, _ = run_hook('reject')
        self.assertEqual(rc, 1); self.assertTrue(err)
        u = rows[0]['uncaught_first']
        self.assertEqual((u['ctor'], u['code']), ('Error', 'ECONNREFUSED'))
        self.assertIn(u['origin'], ('unhandledRejection_event', 'unhandledRejection'))

    def test_listen_full_milestones(self):
        port = free_port()
        orig = HOOK.read_text()
        self.assertIn('const PORT = 3000', orig)
        rc, _, _, rows, _ = run_hook('listen', port)
        self.assertEqual(rc, 0)
        self.assertEqual(rows[0]['milestones'][:7], list(C.MILESTONES[:7]))
        self.assertNotIn('listening_3000', rows[0]['milestones'])      # other port: no false 3000 milestone

    def test_hook_records_no_free_text(self):
        src = HOOK.read_text()
        for banned in ('process.env)', 'process.argv', '.message', "on('uncaughtException'",
                       "on('unhandledRejection'"):
            self.assertNotIn(banned, src)
        # v2: exactly one stack read, inside frames(), reduced to MAIN line:col / 'node' / 'other'
        self.assertEqual(len(__import__('re').findall(r'\.stack\b', src)), 1)
        self.assertEqual(src.count('String(holder.stack)'), 1)
        self.assertIn("out.push(m[2] + ':' + m[3]);", src)
        self.assertIn("'uncaughtExceptionMonitor'", src)

    def test_loader_rewrites_only_main(self):
        src = LOADER.read_text()
        self.assertIn("if (url !== MAIN || r.format !== 'module' || r.source == null) return r;", src)

    def test_trace_identities_and_pending_stacks(self):
        rc, out, err, rows, _ = run_hook('drain')
        t = rows[0]['trace']
        want = __import__('hashlib').sha256(FAKE_MAIN.encode()).hexdigest()
        self.assertEqual(t['main_file'], {'size': len(FAKE_MAIN.encode()), 'sha256': want, 'error': None})
        self.assertEqual(t['main_source'], {'bytes': len(FAKE_MAIN.encode()), 'sha256': want})
        self.assertTrue(t['cwd_is_main_dir'] is True and t['pending']['armed'])
        self.assertGreaterEqual(t['pending']['count'], 1)
        # the never-settling `await new Promise(() => {})` is FAKE_MAIN line 7 (line 1 is the leading newline)
        line = FAKE_MAIN.split('\n').index("  if (mode === 'drain') { await new Promise(() => {}); }") + 1
        self.assertTrue(any(f.split(':')[0] == str(line) for st in t['pending']['stacks'] for f in st if ':' in f), t)
        self.assertNotIn(b'synthetic', json.dumps(rows).encode())

    def test_empty_main_signature_matches_discriminator(self):
        # The bfff7a39 native signature: main_module_evaluated, reflect absent, drained, only stdio PipeWraps.
        rc, out, err, rows, _ = run_hook('drain', main='', shim=False)
        r = rows[0]
        self.assertEqual((rc, out, err), (0, b'', b''))
        self.assertEqual(r['milestones'], ['preload_loaded', 'main_module_evaluated'])
        self.assertEqual((r['reflect_hook'], C.diag_mechanism(rows)), ('absent', 'EVENT_LOOP_DRAINED'))
        self.assertEqual(r['trace']['main_file']['size'], 0)
        self.assertEqual(C.trace_class(rows, C.BUNDLE_SHA256), 'MAIN_FILE_DIFFERS_FROM_BUNDLE')


def manager(**kw):
    m = C.empty_manager(310)
    m.update(image_ref_is_pinned=True, elapsed_ms=900, stop_reason='exits_observed', final_status='exited',
             final_exit_code=0, stopped_by_qa=True, restart_count=7,
             events={'starts': 8, 'dies': [{'exit_code': 0, 'lifetime_ms': 250}] * 8, 'die_count': 8, 'oom_events': 0})
    m.update(kw); return m


def healthy_manager():
    return manager(stop_reason='healthy', health_reached=True, health_ms=12000, final_status='exited',
                   restart_count=0, events={'starts': 1, 'dies': [{'exit_code': 0, 'lifetime_ms': 13000}],
                                            'die_count': 1, 'oom_events': 0})


SCHED = {'interval_s': 5, 'timeout_s': 10, 'start_period_s': 10, 'retries': 20, 'budget_s': 310, 'test_sha256': 'a' * 64}
BASIS = {'source': 'rendered-compose-config-no-interpolate', 'runtipi': SCHED, 'runtipi-db': dict(SCHED, budget_s=305),
         'manager_window_s': 310, 'upstream_window_s': 310, 'base_wait_s': 335}


def arm(k, m, reason='none'):
    a = C.empty_arm(k); a.update(insufficient_reason=reason, manager=m, basis=BASIS, db_healthy=True, queue_running=True,
                                 cleanup_absent=True, container_ids={'runtipi-db': '1' * 64, 'runtipi': '2' * 64})
    if k == 'B':
        a['upstream'] = {'image_is_recorded_upstream': True, 'healthy': True, 'elapsed_ms': 9000, 'stopped_by_id': True,
                         'removed_by_id': True, 'exit_code': 0}
    return a


def export(ma, mb, mc):
    imgs = {n: {'id': 'sha256:' + c * 64, 'id_kind': 'manifest', 'chain_verified': True}
            for n, c in (('postgres', 'a'), ('rabbitmq', 'b'), ('upstream', 'c'))}
    return C.finish({'A': arm('A', ma), 'B': arm('B', mb), 'C': arm('C', mc)}, imgs,
                    {'engine_image_id': 'sha256:' + '9' * 64, 'matches_capture': True,
                     'image_env_node_options_present': False,
                     'engine_main': {'state': 'read', 'sha256': C.BUNDLE_SHA256, 'size': C.BUNDLE_SIZE,
                                     'matches_bundle': True}, 'storage_driver': 'overlayfs'},
                    [], 'd' * 64, 'e' * 64, True)


class Classification(unittest.TestCase):
    def test_verdict_table(self):
        H, S = healthy_manager, manager
        self.assertEqual(export(S(), S(), S())['verdict'], 'IMAGE_NEVER_STARTS')
        self.assertEqual(export(H(), S(), S())['verdict'], 'UPSTREAM_SCHEMA_BREAKS')
        self.assertEqual(export(H(), H(), S())['verdict'], 'PRODUCT_WIRING_BREAKS')
        self.assertEqual(export(H(), H(), H())['verdict'], 'NOT_REPRODUCED')
        err = manager(events={'starts': 2, 'dies': [{'exit_code': 1, 'lifetime_ms': 100}], 'die_count': 1, 'oom_events': 0})
        self.assertEqual(export(err, H(), H())['verdict'], 'MIXED')
        dl = manager(stop_reason='deadline', events={'starts': 1, 'dies': [], 'die_count': 0, 'oom_events': 0},
                     final_status='exited', restart_count=0)
        doc = export(H(), dl, H())
        self.assertEqual((doc['results']['B'], doc['verdict']), ('DEADLINE', 'UPSTREAM_SCHEMA_BREAKS'))

    def test_silent_requires_zero_log_bytes_and_all_zero_exits(self):
        logged = manager(log=HC.parse_log(b'\x01\x00\x00\x00\x00\x00\x00\x05hello', truncated=False))
        self.assertEqual(C.arm_result(arm('A', logged)), 'EXITS_ERROR')
        oom = manager(oom_killed=True)
        self.assertEqual(C.arm_result(arm('A', oom)), 'EXITS_ERROR')
        self.assertEqual(C.arm_result(arm('A', manager())), 'EXITS_CLEAN_SILENT')

    def test_insufficient_dominates(self):
        a = arm('A', healthy_manager(), 'db_not_healthy')
        self.assertEqual(C.arm_result(a), 'INSUFFICIENT')
        self.assertEqual(C.verdict({'A': 'INSUFFICIENT', 'B': 'STARTS_HEALTHY', 'C': 'STARTS_HEALTHY'}), 'INSUFFICIENT')
        self.assertEqual(C.verdict({'A': 'STARTS_HEALTHY', 'B': 'INSUFFICIENT', 'C': 'EXITS_ERROR'}), 'INSUFFICIENT')

    def test_schema_rejects_drift(self):
        doc = export(manager(), manager(), manager())
        for mutate in (lambda d: d.update(extra=1), lambda d: d.update(verdict='NOT_REPRODUCED'),
                       lambda d: d['arms']['A'].update(name='x'), lambda d: d['arms']['A']['manager'].update(stop_reason='x'),
                       lambda d: d['arms']['A'].update(wiring={'override_sha256': 'f' * 64}),
                       lambda d: d['backend'].update(engine_image_id='privsep-wam:595523fb'),
                       lambda d: d['collector_errors'].append('free text')):
            bad = copy.deepcopy(doc); mutate(bad)
            with self.assertRaises(C.Violation):
                C.validate_export(bad)

    def test_basis_from_template_healthchecks(self):
        cfg = {'services': {
            'runtipi': {'healthcheck': {'test': ['CMD', 'curl', '-f', 'http://localhost:3000/api/health'],
                                        'interval': '5s', 'timeout': '10s', 'retries': 20, 'start_period': '10s'}},
            'runtipi-db': {'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -d tipi -U tipi'], 'interval': '5s',
                                           'timeout': '10s', 'retries': 20, 'start_period': '5s'}}}}
        b, refused = C.basis(cfg)
        self.assertIsNone(refused)
        self.assertEqual((b['manager_window_s'], b['upstream_window_s'], b['base_wait_s']), (310, 310, 335))
        self.assertEqual(C.basis({'services': {'runtipi': {}}})[1], 'basis_refused')


TRACE = {'main_file': {'size': 3, 'sha256': 'a' * 64, 'error': None}, 'main_source': {'bytes': 3, 'sha256': 'a' * 64},
         'prefix_chars': 110, 'cwd_is_main_dir': True, 'app_mounts': 0, 'root_fstype': 'overlay',
         'pending': {'armed': True, 'count': 2, 'overflow': 0, 'stacks': [['12:345', 'node'], ['other', '1:200']]}}


class Diag(unittest.TestCase):
    GOOD = {'schema': 'msq-diag-v2', 'slot': 0, 'node_major': 22, 'milestones': ['preload_loaded'],
            't_ms': {'preload_loaded': 5}, 'loader_hook': 'registered', 'reflect_hook': 'not_armed',
            'exit': {'fired': True, 'code': 0}, 'before_exit': {'fired': True, 'count': 1, 'resources': {'TTYWrap': 2}},
            'uncaught': {'count': 0, 'first': None}, 'exit_calls': [], 'hook_errors': 0, 'trace': TRACE}

    def test_trace_projection_is_strict(self):
        self.assertEqual(C.validate_diag(json.dumps(self.GOOD).encode())['trace'], TRACE)
        for mutate in (lambda t: t['pending']['stacks'].append(['/app/main.mjs:1:2']),
                       lambda t: t['pending']['stacks'][0].append('Error: synthetic'),
                       lambda t: t['main_file'].update(sha256='A' * 64),
                       lambda t: t['main_file'].update(error='ENOENT'),
                       lambda t: t.update(root_fstype='ext4'), lambda t: t.update(extra=1),
                       lambda t: t.update(main_source=None),            # prefix_chars must then be None
                       lambda t: t['pending'].update(stacks=[['1:1']] * 9)):
            bad = copy.deepcopy(self.GOOD); mutate(bad['trace'])
            with self.assertRaises(C.Violation):
                C.validate_diag(json.dumps(bad).encode())
        old = copy.deepcopy(self.GOOD); old['schema'] = 'msq-diag-v1'; del old['trace']
        with self.assertRaises(C.Violation):
            C.validate_diag(json.dumps(old).encode())

    def test_trace_class(self):
        row = C.validate_diag(json.dumps(self.GOOD).encode())
        self.assertEqual(C.trace_class([row], 'a' * 64), 'BUNDLE_EVALUATED')
        self.assertEqual(C.trace_class([row], 'b' * 64), 'MAIN_FILE_DIFFERS_FROM_BUNDLE')
        r2 = copy.deepcopy(row); r2['milestones'] = ['preload_loaded', 'main_module_evaluated']; r2['reflect_hook'] = 'absent'
        self.assertEqual(C.trace_class([r2], 'a' * 64), 'BUNDLE_EVALUATED_WITHOUT_REFLECT')
        self.assertEqual(C.trace_class([row, r2], 'a' * 64), 'MIXED')
        self.assertEqual(C.trace_class([], 'a' * 64), 'NO_DIAG')

    def test_strict_projection(self):
        self.assertEqual(C.validate_diag(json.dumps(self.GOOD).encode())['last_milestone'], 'preload_loaded')
        for mutate in (lambda d: d.update(env={'A': 'b'}), lambda d: d['milestones'].append('free'),
                       lambda d: d['before_exit']['resources'].update(SecretWrap=1),
                       lambda d: d.update(uncaught={'count': 1, 'first': {'ctor': 'Error', 'code': 'bad code',
                                                                         'origin': 'uncaughtException'}}),
                       lambda d: d['exit'].update(code='0'), lambda d: d.update(exit_calls=[0] * 5)):
            bad = copy.deepcopy(self.GOOD); mutate(bad)
            with self.assertRaises(C.Violation):
                C.validate_diag(json.dumps(bad).encode())
        with self.assertRaises(C.Violation):
            C.validate_diag(b'x' * (C.DIAG_BYTES + 1))


class Events(unittest.TestCase):
    def test_die_rows_lifetimes_and_cut(self):
        cid = 'a' * 64
        ev = [{'Action': 'start', 'timeNano': 1_000_000_000, 'id': cid},
              {'Action': 'die', 'timeNano': 1_250_000_000, 'Actor': {'Attributes': {'exitCode': '0'}}},
              {'Action': 'start', 'timeNano': 1_400_000_000},
              {'Action': 'die', 'timeNano': 1_700_000_000, 'Actor': {'Attributes': {'exitCode': '0'}}},
              {'Action': 'die', 'timeNano': 9_000_000_000, 'Actor': {'Attributes': {'exitCode': '137'}}}]
        raw = b''.join(json.dumps(e).encode() + b'\n' for e in ev)
        out = C.parse_events(raw, 2_000_000_000)
        self.assertEqual(out, {'starts': 2, 'dies': [{'exit_code': 0, 'lifetime_ms': 250},
                                                     {'exit_code': 0, 'lifetime_ms': 300}], 'die_count': 2, 'oom_events': 0})
        with self.assertRaises(C.Violation):
            C.parse_events(b'x' * 300000, 1)


class Migrations(unittest.TestCase):
    ALLOW = {'names': ['0031_a', '0032_app_migrations'], 'pinned': {'0031_a': ('1' * 64, 31)},
             'upstream_v4_11_0': {'0031_a': ('1' * 64, 31), '0032_app_migrations': ('2' * 64, 32)}}

    def test_classes(self):
        pinned = HC.migration_membership('read', 0, ('1' * 64 + ' 31\n').encode(), self.ALLOW)
        both = HC.migration_membership('read', 0, ('1' * 64 + ' 31\n' + '2' * 64 + ' 32\n').encode(), self.ALLOW)
        self.assertEqual(C.migration_row(pinned, self.ALLOW)['class'], 'pinned_only')
        row = C.migration_row(both, self.ALLOW)
        self.assertEqual((row['class'], row['upstream_only_applied']), ('includes_upstream_only', ['0032_app_migrations']))
        self.assertEqual(C.migration_row(HC.migration_membership('table_absent', 0, None, self.ALLOW), self.ALLOW)['class'],
                         'empty')


def tree_file(rel):
    if REPO is not None:
        return (REPO / rel).read_bytes()
    with tarfile.open(SOURCE_TAR) as t:
        return t.extractfile(rel).read()


class GuestStatic(unittest.TestCase):
    def test_box_only_override_is_named_user_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp); (t / 'scripts').mkdir()
            (t / 'scripts/prepare-named-user.sh').write_bytes(tree_file('scripts/prepare-named-user.sh'))
            G = load('ms_guest_under_test', GUEST); G.TREE = t
            text = G.named_user_box_only()
        self.assertEqual(text, "services:\n  runtipi-reverse-proxy:\n    ports: !override\n"
                               "      - 127.0.0.1:8480:80\n      - 127.0.0.1:8443:443\n")

    def test_guest_never_exports_env_values(self):
        src = GUEST.read_text()
        self.assertIn("'POSTGRES_PASSWORD': secrets.token_hex(24)", src)
        self.assertNotIn("read_text().split('=')", src)
        self.assertIn("ARM_PROJECT = {'A': 'msq-a', 'B': 'msq-b', 'C': 'runtipi'}", src)
        G = load('ms_guest_projects', GUEST)
        self.assertEqual(G.ARM_PROJECT, C.PROJECTS); self.assertEqual(set(G.CLEANUP_PROJECTS), set(C.PROJECTS.values()))

    def test_release_pins_entry_matches_product(self):
        src = tree_file('install.sh').decode()
        self.assertIn('(cd "$HERE" && "$PY" -m wam.release_pins "$OVR" "$IMAGE")', src)
        self.assertIn('up -d "${RECREATE[@]}" --no-deps --wait runtipi)', src)
        self.assertIn('(cd "$HERE" && "$PY" -m wam host-approvals sync >/dev/null)', src)


if __name__ == '__main__':
    result = unittest.main(argv=[sys.argv[0], '-v'], exit=False).result
    print(json.dumps({'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                      'skipped': len(result.skipped)}))
    sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
