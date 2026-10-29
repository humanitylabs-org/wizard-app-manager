"""New bounded runner controls only; synthetic files, no VM/process/host census."""
import ast

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cold_intermittent_test', REPO/'qa/privsep-storage/cold_intermittent.py')
assert SPEC is not None and SPEC.loader is not None
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


def observation(state='pre-request-stop'):
    return {'diagnostic': 'storage-cold-bootstrap-v1', 'full_acceptance': False, 'review_required': True,
            'transaction_executed': False, 'request_reader_executed': False, 'replay_authorized': False,
            'state': state, 'sole_init_process_snapshot': None}


def test_finite_campaign_all_clear_still_runs_exact_three():
    calls, rows = [], []
    M.finite_campaign(lambda i: calls.append(i) or observation(), rows.append)
    assert calls == [1, 2, 3] and len(rows) == 3
    assert all(r['full_acceptance'] is False for r in rows)


def test_finite_campaign_all_refused_does_not_retry_until_green():
    calls, rows = [], []
    M.finite_campaign(lambda i: calls.append(i) or observation('bootstrap-refused'), rows.append)
    assert calls == [1, 2, 3] and len(rows) == 3


def test_finite_campaign_mixed_outcomes_keep_every_observation():
    calls, rows = [], []
    def observe(i):
        calls.append(i)
        return {'index': i, 'observation': observation('bootstrap-refused' if i == 2 else 'pre-request-stop')}
    M.finite_campaign(observe, rows.append)
    assert calls == [1, 2, 3] and [r['index'] for r in rows] == calls
    assert [r['observation']['state'] for r in rows] == ['pre-request-stop', 'bootstrap-refused', 'pre-request-stop']


def test_cleanup_safety_failure_aborts_before_next_boot():
    calls, rows = [], []
    def observe(i):
        calls.append(i); raise RuntimeError('synthetic-cleanup-failure')
    with pytest.raises(RuntimeError): M.finite_campaign(observe, rows.append)
    assert calls == [1] and rows == []


def test_parser_accepts_both_real_guard_outcomes_not_acceptance():
    for state in ('pre-request-stop', 'bootstrap-refused'):
        row = observation(state)
        assert M.parse_observation(b'not json\n'+json.dumps(row).encode()) == row
    assert M.parse_observation(b'not-yet\n') is None


def test_parser_rejects_replay_acceptance_wrong_boolean_and_unknown_state():
    for field in ('full_acceptance', 'transaction_executed', 'request_reader_executed', 'replay_authorized'):
        row = observation(); row[field] = True
        with pytest.raises(AssertionError): M.parse_observation(json.dumps(row).encode())
    for field, value in (('review_required', False), ('full_acceptance', 0), ('state', 'unexpected-bootstrap-return')):
        row = observation(); row[field] = value
        with pytest.raises(AssertionError): M.parse_observation(json.dumps(row).encode())


def test_parser_rejects_ambiguous_bounded_or_canary_serial():
    raw = json.dumps(observation()).encode()
    for bad in (raw+b'\n'+raw, b'x'*1048577, b'QA_PRIVATE_CANARY'):
        with pytest.raises(AssertionError): M.parse_observation(bad)


def test_parser_snapshot_cannot_authorize_replay():
    row = observation('bootstrap-refused'); row['sole_init_process_snapshot'] = {'replay_authorized': True}
    with pytest.raises(AssertionError): M.parse_observation(json.dumps(row).encode())
    row['sole_init_process_snapshot']['replay_authorized'] = False
    assert M.parse_observation(json.dumps(row).encode()) == row


def test_junit_requires_exact_unique_cases_no_failures_errors_skips(tmp_path):
    path = tmp_path/'report.xml'; expected = ['tests/synthetic.py::test_one']
    case = '<testcase classname="tests.synthetic" name="test_one"/>'
    path.write_text('<testsuite>'+case+'</testsuite>')
    assert M.junit(path, expected)['valid']
    for body in (case+case, case.replace('/>', '><failure/></testcase>'), case.replace('/>', '><error/></testcase>'),
                 case.replace('/>', '><skipped/></testcase>'), case.replace('test_one', 'test_wrong')):
        path.write_text('<testsuite>'+body+'</testsuite>')
        assert M.junit(path, expected)['valid'] is False


def fixture(monkeypatch, tmp_path):
    origin = tmp_path/'origin'; (origin/'evidence').mkdir(parents=True)
    baseline = tmp_path/'baseline'; (baseline/'evidence').mkdir(parents=True)
    stage = tmp_path/'stage'; stage.mkdir()
    for name in ('disk.qcow2', 'secondary.qcow2', 'id_ed25519', 'seed.iso', 'vmlinuz', 'initrd'):
        (origin/'evidence'/name).write_bytes(b'fixed synthetic '+name.encode())
    (origin/'evidence/boot.json').write_text('{"root_uuid":"synthetic"}')
    disk_hashes = {name: M.digest(origin/'evidence'/name) for name in ('disk.qcow2', 'secondary.qcow2')}
    (baseline/'evidence/receipt.json').write_text(json.dumps({'original_disk_sha256_after': disk_hashes}))
    monkeypatch.setattr(M, 'BASELINE', baseline)
    monkeypatch.setattr(M, 'failure_location', lambda exc: {'class': type(exc).__name__, 'frames': []})
    events = []
    class Base:
        def __init__(self, stage, doc):
            self.stage = stage; self.doc = doc; self.evidence = stage/'evidence'; self.evidence.mkdir()
            self.key = self.evidence/'id_ed25519'; self.proc = None; self.serial = None
            self.receipt = {'first_error': None, 'boots': []}
        def save(self): pass
        def capacity(self): events.append(('capacity',))
        def run(self, label, argv):
            events.append(('run', label, argv))
            Path(argv[-1]).write_bytes(b'fixed thin overlay')
        def start(self, index, *a, **kw):
            events.append(('start', index, a, kw))
            self.serial = self.evidence/('serial-'+str(index))
            self.serial.write_text(json.dumps(observation()))
            self.proc = SimpleNamespace(wait=lambda **kw: None, poll=lambda: None)
        def stop(self):
            events.append(('stop',)); self.proc = None
        def guest(self, label, script, *a):
            events.append(('guest', label, script)); return 0
        def copy(self, local, remote, download=False):
            events.append(('copy', remote, download))
            if download:
                if str(local).endswith('.xml'):
                    Path(local).write_text('<testsuite><testcase classname="tests.synthetic" name="test_one"/></testsuite>')
                else: Path(local).write_text('{"replay_authorized":false,"durable_state_requires_review":false}')
    previous = SimpleNamespace(ORIGIN=origin, ORIGIN_XML='synthetic234', PRIOR_XML='synthetic10',
                               make_runner=lambda base: Base, process_absence=lambda paths: events.append(('absence', paths)))
    runner = M.make_runner(previous, None)(stage, {'new_test_cases': ['tests/synthetic.py::test_one']})
    return runner, events


def test_execute_single_prepare_three_fresh_overlay_pairs_only_new_tests(monkeypatch, tmp_path):
    runner, events = fixture(monkeypatch, tmp_path)
    runner.execute()
    starts = [e[1] for e in events if e[0] == 'start']
    assert starts == ['prepare', 'cold-1', 'cold-2', 'cold-3']
    cold = [e for e in events if e[0] == 'start' and str(e[1]).startswith('cold-')]
    assert all(e[2] == (True, {'root_uuid': 'synthetic'}) and e[3] == {'init': M.BOOT, 'root_rw': True} for e in cold)
    commands = [e[2] for e in events if e[0] == 'run']
    assert len(commands) == 8 and all(c[:6] == ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2'] for c in commands)
    assert len({str(c[-1]) for c in commands}) == 8
    assert all(Path(c[-2]) in runner.prepared.values() for c in commands[2:])
    scripts = [e[2] for e in events if e[0] == 'guest']
    test_script = next(s for s in scripts if '-m pytest' in s)
    assert 'tests/test_storage_cold_intermittent.py' in test_script and 'test_storage_cold_process.py' not in test_script
    assert runner.receipt['campaign_complete'] is True and runner.receipt['passed'] is False
    assert runner.receipt['transaction_executed'] is runner.receipt['request_reader_executed'] is False
    assert runner.receipt['automatic_further_campaign_authorized'] is False
    assert [r['index'] for r in runner.receipt['observations']] == [1, 2, 3]
    assert sum(e[0] == 'stop' for e in events) == 4


def test_observation_failure_is_retained_and_no_retry_same_index(monkeypatch, tmp_path):
    runner, events = fixture(monkeypatch, tmp_path)
    runner.prepared = {'disk.qcow2': tmp_path/'disk', 'secondary.qcow2': tmp_path/'secondary'}
    def bad_start(*a, **kw): raise ValueError('synthetic')
    monkeypatch.setattr(runner, 'start', bad_start)
    row = runner.observe(2)
    assert row['index'] == 2 and row['observation'] is None and row['error']['class'] == 'ValueError'
    assert row['owned_qemu_absent'] is True and row['serial_sha256'] is None
    assert runner.receipt['first_error'] == row['error']
    assert sum(e[0] == 'run' for e in events) == 2 and sum(e[0] == 'stop' for e in events) == 1


def test_runner_failure_preserves_earliest_error(monkeypatch, tmp_path):
    runner, _ = fixture(monkeypatch, tmp_path)
    runner.prepared = {'disk.qcow2': tmp_path/'disk', 'secondary.qcow2': tmp_path/'secondary'}
    first = {'class': 'earlier', 'frames': []}; runner.receipt['first_error'] = first
    def fail(*a, **kw): raise ValueError('synthetic')
    monkeypatch.setattr(runner, 'start', fail)
    assert runner.observe(1)['error']['class'] == 'ValueError'
    assert runner.receipt['first_error'] == first


def test_owned_cleanup_failure_is_not_converted_to_a_guard_refusal(monkeypatch, tmp_path):
    runner, _ = fixture(monkeypatch, tmp_path)
    runner.prepared = {'disk.qcow2': tmp_path/'disk', 'secondary.qcow2': tmp_path/'secondary'}
    def fail(): raise RuntimeError('synthetic-cleanup')
    monkeypatch.setattr(runner, 'stop', fail)
    with pytest.raises(RuntimeError): runner.observe(1)


def test_static_capacity_gate_unchanged_and_receipt_precedes_disk_allocation():
    assert M.RESERVE == 217179869184 and M.PENDING == 68719476736 and M.COLD_BOOTS == (1, 2, 3)
    tree = ast.parse((REPO/'qa/privsep-storage/cold_intermittent.py').read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    source = ast.unparse(main)
    assert source.index('base.native_lock()') < source.index('r = make_runner')
    assert source.index("assert report['capacity_admissible']") < source.index('r.execute()')
    assert source.index('r.save()') < source.index("assert report['capacity_admissible']")
    assert 'signal.alarm(2100)' in source and 'signal.alarm(0)' in source
    assert 'prepared_sha256_after' in source and 'original_disk_sha256_after' in source


def test_source_delta_validation_binds_all_inherited_bytes_and_unchanged_probe():
    text = (REPO/'qa/privsep-storage/cold_intermittent.py').read_text()
    tree = ast.parse(text); validate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'validate')
    source = ast.unparse(validate)
    predicates = {ast.dump(n.test) for n in ast.walk(validate) if isinstance(n, ast.Assert)}
    for expression in ('set(b)-set(a) == set(ADDED) and not set(a)-set(b)',
                       'all(b[k] == v for k, v in a.items())',
                       "digest(stage/'diagnostic-init.py') == old_doc['files']['diagnostic-init.py']"):
        assert ast.dump(ast.parse(expression, mode='eval').body) in predicates
    assert 'previous.validate(BASELINE, BASELINE_MANIFEST)' in source
    assert "len(tests['actual_cases']) == 22" in source and "len(receipt['steps']) == 10" in source
    assert 'sys.settrace' not in text and 'frame.f_locals' not in text
