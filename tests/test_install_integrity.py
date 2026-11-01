#!/usr/bin/env python3
"""Synthetic fixtures for the QA-only install-integrity gate. No Docker, no VM, no network.

Runs as `python3 -I -B tests/test_install_integrity.py` (also under pytest) both in the repo and
in the sealed native stage (where qa/ is extracted from source.tar). A fake `docker` executable on
PATH models an image store; the real probe-r3 guest code (eviction/load/measure) drives it.
"""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
NEEDED = ('install_integrity_guest.py', 'manager_probe_guest.py', 'manager_probe_collector.py')
RUNNER_NAMES = ('install_integrity_run.py',)
P610_GUEST = Path('/srv/work/privsep/storage-contract-a/610716410029f2f70fe4233102584c30e9b9022f-source-closure-repair/guest.py')
_TMP = []


def qa_dir():
    d = HERE.parent / 'qa' / 'privsep-storage'
    if d.is_dir():
        return d
    out = Path(tempfile.mkdtemp(prefix='ii-qa-')); _TMP.append(out)
    with tarfile.open(Path.cwd() / 'source.tar') as t:
        for n in NEEDED + RUNNER_NAMES + ('contract_a_guest.py',):
            (out / n).write_bytes(t.extractfile('qa/privsep-storage/' + n).read())
    return out


QA = qa_dir()


def load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


G = load('ii_gate_under_test', QA / 'install_integrity_guest.py')
R = load('ii_runner_under_test', QA / 'install_integrity_run.py')
BOUND = G.BOUND_ID
SERVICE_ID = 'sha256:1c7465e670c2c4bf976b2463b811e0692fe1304660b331e7b6e48040938498c3'
FOREIGN_ID = 'sha256:' + 'ab' * 32
GOOD = {'/app/main.mjs': G.MANAGER, '/app/libexec/wizard-bind-metadata': G.HELPER}
BAD = {'/app/main.mjs': G.EMPTY, '/app/libexec/wizard-bind-metadata': G.HELPER}
SHIM = r'''#!%(py)s
import hashlib, json, re, sys
from pathlib import Path
S = Path(%(state)r)
st = json.loads(S.read_text())
a = sys.argv[1:]
st['calls'].append(a); 
def done(code=0, out=''):
    S.write_text(json.dumps(st)); sys.stdout.write(out); sys.exit(code)
def resolve(x):
    if x in st['images']: return x
    for i, row in st['images'].items():
        if x in row['tags']: return i
    return None
if a[:3] == ['image', 'ls', '--no-trunc']: done(0, ''.join(i + '\n' for i in sorted(st['images'])))
if a[:2] == ['ps', '-aq']: done(0, ''.join(c + '\n' for c in st['containers']))
if a[:2] == ['image', 'inspect']:
    ref = a[-1]; i = resolve(ref)
    if i is None: done(1)
    done(0, json.dumps(i) + '\n' if '--format' in a else '[{}]\n')
if a[:2] == ['image', 'rm']:
    ref = a[-1]; i = resolve(ref)
    if i is None: done(1)
    if ref == i:
        del st['images'][i]
    else:
        st['images'][i]['tags'].remove(ref)
    done(0)
if a[0] == 'load':
    st['loads'] += 1
    st['images'][%(bound)r] = {'tags': [], 'content': st['load_content']}
    done(0, 'Loaded image: archive\n')
if a[0] == 'tag':
    i = resolve(a[1])
    if i is None: done(1)
    st['images'][i]['tags'].append(a[2]); done(0)
if a[0] == 'run':
    assert '--pull=never' in a and a[a.index('--entrypoint') + 1] == 'sh'
    i = resolve(a[a.index('--entrypoint') + 2])
    if i is None: done(125)
    m = re.fullmatch(r'wc -c < (\S+); sha256sum (\S+)', a[a.index('-c') + 1]); assert m and m[1] == m[2]
    row = st['images'][i]['content'].get(m[1])
    if row is None: done(1, '')
    if st.get('mangle'): done(0, 'garbled\n')
    done(0, '%%d\n%%s  %%s\n' %% (row['bytes'], row['sha256'], m[1]))
done(99, 'unmodelled docker verb ' + ' '.join(a))
'''


class Env:
    """Fake image store + guest root; the shim records every docker call."""

    def __init__(self, content=None, load_content=None, containers=(), service=True, mangle=False):
        self.dir = Path(tempfile.mkdtemp(prefix='ii-env-')); _TMP.append(self.dir)
        self.root = self.dir / 'root'; self.root.mkdir(mode=0o700)
        self.bin = self.dir / 'bin'; self.bin.mkdir()
        self.state = self.dir / 'state.json'
        archive = self.root / 'backend.tar.gz'; archive.write_bytes(b'synthetic archive bytes, not an image')
        self.archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        backend = {'archive_sha256': self.archive_sha, 'rootfs': {'diff_ids': ['sha256:' + '0' * 64, load(
            'ii_c', QA / 'manager_probe_collector.py').LAST_DIFF_ID]}, 'config_sha256': 'sha256:' + '1' * 64}
        (self.root / 'backend.json').write_text(json.dumps(backend))
        images = {BOUND: {'tags': [G.PINNED], 'content': dict(content or GOOD)}}
        if service:
            images[SERVICE_ID] = {'tags': ['privsep-service:e1ba0d0564f1'], 'content': {k: dict(v) for k, v in G.SERVICE_PATHS.items()}}
            (self.root / 'service-attestation-before.json').write_text(json.dumps({'engine_image_id': SERVICE_ID}))
        self.state.write_text(json.dumps({'images': images, 'containers': list(containers), 'calls': [], 'loads': 0,
                                          'load_content': dict(load_content or GOOD), 'mangle': mangle}))
        shim = self.bin / 'docker'
        shim.write_text(SHIM % {'py': sys.executable, 'state': str(self.state), 'bound': BOUND}); shim.chmod(0o755)
        self.old_path = os.environ['PATH']; os.environ['PATH'] = str(self.bin) + os.pathsep + self.old_path

    def close(self):
        os.environ['PATH'] = self.old_path

    def st(self):
        return json.loads(self.state.read_text())

    def verbs(self):
        return [' '.join(c[:2] if c[0] in ('image',) else c[:1]) for c in self.st()['calls']]

    def gate(self, label='before-installer-start', **kw):
        real = G.module

        def patched(name, path):
            m = real(name, path)
            if name == 'integrity_probe_r3':
                m.ARCHIVE_SHA = self.archive_sha          # test-only: the fake archive's hash; production constant untouched
            return m
        G.module = patched
        try:
            return G.gate(label, root=self.root, **kw)
        finally:
            G.module = real

    def report(self):
        return json.loads((self.root / G.REPORT_NAME).read_text())


def corrupt(env_content, path, bytes_, sha):
    out = {k: dict(v) for k, v in env_content.items()}; out[path] = {'bytes': bytes_, 'sha256': sha}; return out


class ProductionConstants(unittest.TestCase):
    def test_pins_match_the_parent_verified_values(self):
        self.assertEqual(G.BOUND_ID, 'sha256:94cc09235bd65370eff851207b7ee52335098f078ed4f0a564d38d5aea7a5928')
        self.assertEqual(G.PINNED, 'privsep-wam:595523fb')
        self.assertEqual(G.MANAGER, {'bytes': 6908459, 'sha256': 'b824d438aa844d0f1920c0e29a04f8f82986a81bbf812b53b5b8399b9a62ebbe'})
        self.assertEqual(G.EMPTY['sha256'], 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
        P = load('ii_probe_consts', QA / 'manager_probe_guest.py')
        self.assertEqual(P.ARCHIVE_SHA[:8], 'a953c4ce')
        self.assertEqual(P.C.BOUND_ID, G.BOUND_ID)
        self.assertEqual(P.C.LAST_DIFF_ID, 'sha256:4a7d6b8643f5740cfd307bab4e97cec976dcd0a6e8d6facf80fff867a630d482')

    def test_gate_never_prunes_pulls_or_runs_the_app_cmd(self):
        src = (QA / 'install_integrity_guest.py').read_text()
        code = ast.unparse(ast.parse(src))        # comments/docstrings are not code; strings are
        for forbidden in ('system prune', 'image prune', "'pull'", 'compose', 'engine_facts', 'optional_fact'):
            self.assertNotIn(forbidden, code)
        self.assertIn('--pull=never', code)
        self.assertIn("'--entrypoint', 'sh'", code)


class MeasureParser(unittest.TestCase):
    def test_exact_shape_only(self):
        ok = b'6908459\n' + G.MANAGER['sha256'].encode() + b'  /app/main.mjs\n'
        self.assertEqual(G.parse_measure(ok, '/app/main.mjs'), G.MANAGER)
        self.assertEqual(G.parse_measure(b'0\n' + G.EMPTY['sha256'].encode() + b'  /app/main.mjs\n', '/app/main.mjs'), G.EMPTY)
        for bad in (b'', b'6908459\n', b'x\n' + G.MANAGER['sha256'].encode() + b'  /app/main.mjs\n', ok + b'extra\n',
                    ok.replace(b'/app/main.mjs', b'/other'), ok[:-1], b'garbled\n', ok.upper()):
            with self.assertRaises(AssertionError):
                G.parse_measure(bad, '/app/main.mjs')


class GateFlows(unittest.TestCase):
    def setUp(self):
        self.envs = []

    def tearDown(self):
        for e in self.envs:
            e.close()

    def env(self, **kw):
        e = Env(**kw); self.envs.append(e); return e

    def test_already_correct_proceeds_without_any_mutation(self):
        e = self.env()
        row = e.gate(service=True)
        self.assertEqual((row['path'], row['before'], row['after'], row['passed']), ('already-correct', G.MANAGER, G.MANAGER, True))
        self.assertEqual(row['service'], G.SERVICE_PATHS)
        self.assertEqual(e.st()['loads'], 0)
        self.assertFalse([v for v in e.verbs() if v in ('image rm', 'load', 'tag')])
        self.assertEqual(G.validate_export(e.report())['evicted'], False)

    def test_damaged_then_fixed_exact_eviction_and_reload(self):
        e = self.env(content=BAD)
        row = e.gate(service=True)
        self.assertEqual(row['path'], 'exact-evict-and-reload')
        self.assertEqual((row['before'], row['after']), (G.EMPTY, G.MANAGER))
        self.assertEqual((row['helper_before'], row['helper_after']), (G.HELPER, G.HELPER))
        st = e.st()
        rms = [c for c in st['calls'] if c[:2] == ['image', 'rm']]
        self.assertEqual(rms, [['image', 'rm', '--no-prune', G.PINNED], ['image', 'rm', '--no-prune', '-f', BOUND]])
        self.assertEqual(st['loads'], 1)
        self.assertIn(SERVICE_ID, st['images'], 'the Service image must never be evicted')
        self.assertEqual(st['images'][BOUND]['tags'], [G.PINNED])
        self.assertFalse([c for c in st['calls'] if 'prune' in c and c[:2] != ['image', 'rm']])
        # order: measured damaged BEFORE any removal, re-measured AFTER load
        verbs = e.verbs(); first_rm = verbs.index('image rm'); load_at = verbs.index('load')
        self.assertIn('run', verbs[:first_rm]); self.assertIn('run', verbs[load_at:])
        exported = e.report()
        self.assertEqual(exported['owned_ids'], [])
        summary = G.validate_export(exported, ('before-installer-start',))
        self.assertTrue(summary['evicted'])

    def test_helper_damaged_main_ok_also_evicts_and_fixes(self):
        e = self.env(content=corrupt(GOOD, '/app/libexec/wizard-bind-metadata', 0, G.EMPTY['sha256']))
        row = e.gate()
        self.assertEqual(row['path'], 'exact-evict-and-reload'); self.assertEqual(row['helper_after'], G.HELPER)

    def test_still_damaged_after_reload_fails_closed(self):
        e = self.env(content=BAD, load_content=BAD)
        with self.assertRaises(AssertionError):
            e.gate()
        row = e.report()['gates'][-1]
        self.assertEqual((row['passed'], row['path'], row['error']), (False, 'exact-evict-and-reload', 'AssertionError'))
        self.assertEqual(row['after'], G.EMPTY)
        self.assertEqual(e.st()['loads'], 1, 'one reload, never a retry loop')
        with self.assertRaises(AssertionError):
            G.validate_export(e.report())

    def test_unexpected_content_is_refused_without_eviction(self):
        e = self.env(content=corrupt(GOOD, '/app/main.mjs', 17, 'f' * 64))
        with self.assertRaises(AssertionError):
            e.gate()
        self.assertEqual(e.report()['gates'][-1]['path'], 'refused-no-eviction')
        self.assertFalse([v for v in e.verbs() if v in ('image rm', 'load', 'tag')])

    def test_verify_only_mode_never_evicts_even_when_empty(self):
        e = self.env(content=BAD)
        with self.assertRaises(AssertionError):
            e.gate('before-service-cases', allow_evict=False)
        self.assertEqual(e.report()['gates'][-1]['path'], 'refused-no-eviction')
        self.assertFalse([v for v in e.verbs() if v in ('image rm', 'load', 'tag')])

    def test_foreign_container_blocks_eviction(self):
        e = self.env(content=BAD, containers=['c' * 64])
        with self.assertRaises(AssertionError):
            e.gate()
        self.assertFalse([c for c in e.st()['calls'] if c[:2] == ['image', 'rm']])
        self.assertEqual(e.st()['loads'], 0)

    def test_missing_bound_image_or_unparsable_measure_fail_closed(self):
        e = self.env(); st = e.st(); del st['images'][BOUND]; e.state.write_text(json.dumps(st))
        with self.assertRaises(AssertionError):
            e.gate()
        e2 = self.env(mangle=True)
        with self.assertRaises(AssertionError):
            e2.gate()
        self.assertFalse([v for v in e2.verbs() if v in ('image rm', 'load', 'tag')])

    def test_wrong_archive_never_loads(self):
        e = self.env(content=BAD)
        (e.root / 'backend.tar.gz').write_bytes(b'tampered archive')
        with self.assertRaises(AssertionError):
            e.gate()
        self.assertEqual(e.st()['loads'], 0)
        self.assertFalse([c for c in e.st()['calls'] if c[:2] == ['image', 'rm']])

    def test_service_mismatch_fails_closed_without_evicting_service(self):
        e = self.env()
        st = e.st(); st['images'][SERVICE_ID]['content']['/app/server/index.mjs'] = {'bytes': 0, 'sha256': G.EMPTY['sha256']}
        e.state.write_text(json.dumps(st))
        with self.assertRaises(AssertionError):
            e.gate(service=True)
        self.assertIn(SERVICE_ID, e.st()['images'])
        self.assertFalse([c for c in e.st()['calls'] if c[:2] == ['image', 'rm']])

    def test_three_gates_accumulate_in_one_export(self):
        e = self.env(content=BAD)
        e.gate('before-installer-start', service=True)
        e.gate('before-install-sh', service=True)
        e.gate('before-service-cases', service=True, allow_evict=False)
        doc = e.report(); summary = G.validate_export(doc, G.LABELS)
        self.assertEqual([r['path'] for r in summary['gates']], ['exact-evict-and-reload', 'already-correct', 'already-correct'])
        self.assertEqual(e.st()['loads'], 1)
        text = json.dumps(doc)
        self.assertNotIn('garbled', text); self.assertNotIn('Loaded image', text)


class ExportValidation(unittest.TestCase):
    def good(self):
        row = {'label': 'before-installer-start', 'path': 'exact-evict-and-reload', 'passed': True, 'before': dict(G.EMPTY),
               'after': dict(G.MANAGER), 'helper_before': dict(G.HELPER), 'helper_after': dict(G.HELPER),
               'allow_evict': True, 'error': None, 'service': {k: dict(v) for k, v in G.SERVICE_PATHS.items()}}
        return {'schema': 'privsep-install-integrity-v1', 'gates': [row], 'owned_ids': []}

    def test_accepts_the_expected_shape(self):
        self.assertTrue(G.validate_export(self.good())['evicted'])

    def test_rejects_every_tamper(self):
        def mut(f):
            d = self.good(); f(d); return d
        cases = [mut(lambda d: d['gates'][0].update(after=dict(G.EMPTY))),
                 mut(lambda d: d['gates'][0].update(path='already-correct')),
                 mut(lambda d: d['gates'][0].update(passed=False)),
                 mut(lambda d: d['gates'][0].update(error='AssertionError')),
                 mut(lambda d: d['gates'][0].update(raw_output='/app/main.mjs bytes')),
                 mut(lambda d: d['gates'][0].update(allow_evict=False)),
                 mut(lambda d: d['gates'][0].update(label='surprise')),
                 mut(lambda d: d['gates'][0].update(before={'bytes': 3, 'sha256': 'a' * 64})),
                 mut(lambda d: d['gates'][0]['service'].pop('/sbin/tini')),
                 mut(lambda d: d.update(owned_ids=['c' * 64])),
                 mut(lambda d: d.update(gates=[])), mut(lambda d: d.update(schema='x'))]
        for bad in cases:
            with self.assertRaises((AssertionError, KeyError)):
                G.validate_export(bad)

    def test_label_order_requirement(self):
        d = self.good()
        with self.assertRaises(AssertionError):
            G.validate_export(d, ('before-install-sh',))


class GuestWiring(unittest.TestCase):
    def test_guest_is_ast_identical_to_610_outside_the_gate(self):
        new = (QA / 'contract_a_guest.py').read_text()
        if P610_GUEST.is_file():
            old = P610_GUEST.read_text()
        else:
            try:
                old = subprocess.check_output(['git', 'show', '610716410029f2f70fe4233102584c30e9b9022f:qa/privsep-storage/contract_a_guest.py'],
                                              cwd=HERE.parent, text=True, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.CalledProcessError):
                self.skipTest('no 610 guest reference available')
        self.assertEqual(len(R.guest_closure(new, old)), 64)
        with self.assertRaises(AssertionError):                     # drift anywhere else is refused
            R.guest_closure(new.replace("assert re.fullmatch('[0-9a-f]{40}',revision)", "assert True", 1), old)

    def test_gate_runs_immediately_before_the_real_install_sh_call(self):
        tree = ast.parse((QA / 'contract_a_guest.py').read_text())
        hits = []
        for n in ast.walk(tree):
            if isinstance(n, ast.If) and "endswith('/install.sh')" in ast.unparse(n.test) and "event == 'call'" in ast.unparse(n.test):
                hits.append([ast.unparse(s) for s in n.body])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "integrity_gate('before-install-sh')")
        self.assertTrue(hits[0][1].startswith('assert custody().require_custody()'))


class RunnerMath(unittest.TestCase):
    def test_floors_and_alarm_follow_prior_install_stage(self):
        self.assertEqual(R.START_FLOOR, 285781630976); self.assertEqual(R.RUN_FLOOR, 277191696384)
        self.assertEqual(R.ALARM, 10800); self.assertLess(R.ALARM + 420, 11400)
        self.assertEqual(R.EXPECTED_NATIVE_CHECKS[0], 'fresh-synthetic-signedout-inputs'); self.assertEqual(len(R.EXPECTED_NATIVE_CHECKS), 6)
        self.assertEqual(R.GATES, G.LABELS)

    def test_product_rows_exclude_only_qa_paths(self):
        rows = [{'path': 'install.sh'}, {'path': 'qa/privsep-storage/x.py'}, {'path': 'tests/test_install_integrity.py'},
                {'path': 'tests/test_other.py'}, {'path': 'scripts/prepare-alongside.py'}]
        self.assertEqual([r['path'] for r in R.product_rows(rows)], ['install.sh', 'tests/test_other.py', 'scripts/prepare-alongside.py'])


class PerformOrder(unittest.TestCase):
    def test_gate_sequence_in_runner_perform(self):
        src = (QA / 'install_integrity_run.py').read_text()
        body = src[src.index('        def perform(self, production):'):src.index('        def finish(self):')]
        order = ['self.collector_tests()', 'self.start(1)', "'fresh-source-before-images'", "self.load_image('backend'",
                 'self.integrity(GATES[0])', "'actual-none-root-installer'", "'actual-none-install'",
                 'self.integrity(GATES[2]', 'contract.CASES[0]', "'native-security'", "'exact-cleanup-with-negative'"]
        at = [body.index(x) for x in order]
        self.assertEqual(at, sorted(at), 'gate/stage order changed')
        self.assertEqual(len(set(at)), len(at))

    def test_receipt_cannot_pass_without_integrity_summary(self):
        src = (QA / 'install_integrity_run.py').read_text()
        self.assertIn("r.receipt.get('image_integrity_summary') is not None", src)
        self.assertIn('full_acceptance=False', src)


def tearDownModule():
    import shutil
    for p in _TMP:
        shutil.rmtree(p, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=1)
