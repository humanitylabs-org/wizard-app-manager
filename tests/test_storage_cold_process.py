"""New finite process diagnostic only; no real host census or product invocation."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cold_process_probe_test', REPO/'qa/privsep-storage/cold_probe.py')
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def generated():
    return PROBE.build_probe((REPO/'scripts/storage-maintenance-init.py').read_bytes(),
                             (REPO/'scripts/protected-agent-storage.py').read_bytes()).decode()


def support():
    tree = ast.parse(generated())
    start = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'project_error')
    ns = {'sys': sys, 'BOOT': Path(PROBE.BOOT)}
    exec(compile(ast.Module(body=[n for n in tree.body if n.lineno >= start and not isinstance(n, ast.If)], type_ignores=[]), PROBE.BOOT, 'exec'), ns)
    return ns


def stat_text(pid=42, state='S', flags=0, start=99, comm='QA_PRIVATE_CANARY ) x'):
    fields = [state, '1']+['0']*18
    fields[6] = str(flags); fields[19] = str(start)
    return str(pid)+' ('+comm+') '+' '.join(fields)+'\n'


def fixture(tmp_path, state='S', comm='QA_PRIVATE_CANARY ) x'):
    p = tmp_path/'42'; p.mkdir()
    (p/'stat').write_text(stat_text(state=state, comm=comm))
    executable = tmp_path/'QA_PRIVATE_CANARY'
    executable.write_bytes(b'fixed synthetic executable\n')
    (p/'exe').symlink_to(executable)
    return p, executable


def guard(ns, line=258, filename=None, kind=None):
    class Refusal(Exception): pass
    g = {'Refusal': kind or Refusal}
    t = ast.parse('def require_fence():\n userspace = [42]\n if userspace: raise Refusal("QA_PRIVATE_CANARY")\n')
    ast.increment_lineno(t, line-3)
    exec(compile(t, filename or PROBE.PRODUCT, 'exec'), g)
    return g


def run_guard(ns, g):
    sys.settrace(ns['_diagnostic_trace'])
    try:
        with pytest.raises(g['Refusal']): g['require_fence']()
    finally: sys.settrace(None)


def test_generated_original_bootstrap_and_product_call_stop_unchanged():
    old = ast.parse((REPO/'scripts/storage-maintenance-init.py').read_text())
    new = ast.parse(generated())
    for name in ('bootstrap', 'protected', 'main'):
        find = lambda t: next(n for n in t.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert ast.dump(find(old), include_attributes=True) == ast.dump(find(new), include_attributes=True)
    ns = support(); g = {'marker': []}
    exec(compile('def main():\n marker.append("request-or-transaction")\n', PROBE.PRODUCT, 'exec'), g)
    sys.settrace(ns['_diagnostic_trace'])
    try:
        with pytest.raises(ns['_DiagnosticStop']): g['main']()
    finally: sys.settrace(None)
    assert g['marker'] == [] and ns['_DIAGNOSTIC_PROCESS'] is None


def test_exact_guard_exception_collects_only_userspace_once():
    ns = support(); seen = []
    ns['process_snapshot'] = lambda p: seen.append(list(p)) or {'status': 'synthetic'}
    g = guard(ns)
    run_guard(ns, g); run_guard(ns, g)
    assert seen == [[42]] and ns['_DIAGNOSTIC_PROCESS'] == {'status': 'synthetic'}


def test_foreign_filename_and_wrong_guard_line_cannot_capture():
    for opts in ({'filename': '/QA_PRIVATE_CANARY'}, {'line': 257}):
        ns = support(); ns['process_snapshot'] = lambda p: pytest.fail('unbound capture')
        run_guard(ns, guard(ns, **opts))
        assert ns['_DIAGNOSTIC_PROCESS'] is None


def test_noncanonical_guard_code_identity_cannot_capture():
    ns = support(); g = guard(ns); original = g['require_fence']
    g['require_fence'] = lambda: None
    sys.settrace(ns['_diagnostic_trace'])
    try:
        with pytest.raises(g['Refusal']): original()
    finally: sys.settrace(None)
    assert ns['_DIAGNOSTIC_PROCESS'] is None


def test_probe_failure_does_not_replace_actual_refusal_or_leak_text():
    ns = support()
    def fail(_): raise RuntimeError('QA_PRIVATE_CANARY')
    ns['process_snapshot'] = fail
    run_guard(ns, guard(ns))
    assert ns['_DIAGNOSTIC_PROCESS'] == {'status': 'probe-error', 'replay_authorized': False}
    assert 'QA_PRIVATE_CANARY' not in json.dumps(ns['_DIAGNOSTIC_PROCESS'])


def test_stat_flags_field_and_parent_state_are_not_shifted_by_comm(tmp_path):
    p = tmp_path/'stat'; p.write_text(stat_text(flags=0x00200000))
    row = PROBE.process_stat(p, 42)
    assert row['pid'] == 42 and row['ppid'] == 1 and row['state'] == 'S'
    assert row['pf_kthread'] is True and row['flags'] == 0x00200000 and row['start_ticks'] == 99
    assert row['comm_class'] == 'unknown' and 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_stat_bad_pid_state_size_and_numbers_fail_with_fixed_labels(tmp_path):
    for text in (stat_text(pid=43), stat_text(state='QA_PRIVATE_CANARY'), 'x'*4097,
                 stat_text(start=-1), stat_text(flags=2**65), 'QA_PRIVATE_CANARY'):
        p = tmp_path/'stat'; p.write_text(text)
        with pytest.raises((ValueError, IndexError)): PROBE.process_stat(p, 42)


def test_snapshot_is_exact_guard_list_not_a_new_census(tmp_path):
    fixture(tmp_path); extra = tmp_path/'43'; extra.mkdir(); (extra/'stat').write_text('QA_PRIVATE_CANARY')
    row = PROBE.process_snapshot([42], tmp_path)
    assert row['candidate_pids'] == [42] and row['candidate_count'] == 1
    assert [p['pid'] for p in row['processes']] == [42]
    assert row['processes'][0]['same_pid_lifetime'] is True
    assert row['processes'][0]['stat_changed'] is False
    assert row['replay_authorized'] is False and 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_missing_candidate_remains_visible_not_silently_ignored(tmp_path):
    row = PROBE.process_snapshot([42], tmp_path)
    p = row['processes'][0]
    assert p['stat_before_error'] == p['stat_after_error'] == 'missing-or-exited'
    assert p['executable']['hash_status'] == p['executable']['link_error'] == 'missing-or-exited'
    assert p['same_pid_lifetime'] is False and row['candidate_pids'] == [42]


def test_zombie_comm_has_only_fixed_unauthenticated_class(tmp_path):
    p, _ = fixture(tmp_path, state='Z', comm='systemd-udevd'); (p/'exe').unlink()
    row = PROBE.process_snapshot([42], tmp_path)['processes'][0]
    assert row['stat_before']['state'] == 'Z' and row['stat_before']['comm_class'] == 'systemd-udevd'
    assert row['stat_before']['comm_class_is_identity'] is False
    assert row['executable']['hash_status'] == 'missing-or-exited'


def test_pid_reuse_and_state_change_are_explicit(monkeypatch, tmp_path):
    fixture(tmp_path)
    before = {'start_ticks': 1, 'state': 'S'}; after = {'start_ticks': 2, 'state': 'Z'}
    values = iter((before, after)); monkeypatch.setattr(PROBE, 'process_stat', lambda *a: next(values))
    row = PROBE.process_snapshot([42], tmp_path)['processes'][0]
    assert row['same_pid_lifetime'] is False and row['stat_changed'] is True


def test_candidate_type_duplicates_pid_and_count_bounds(tmp_path):
    for bad in (None, (), [True], [0], [1], [-42], [2**31], [42, 42], ['QA_PRIVATE_CANARY']):
        row = PROBE.process_snapshot(bad, tmp_path)
        assert row['status'] == 'invalid-candidate-list' and row['processes'] == []
        assert 'QA_PRIVATE_CANARY' not in json.dumps(row)
    row = PROBE.process_snapshot(list(range(2, 67)), tmp_path)
    assert row['status'] == 'candidate-bound-exceeded' and row['candidate_count'] == 65
    assert row['processes'] == [] and 'candidate_pids' not in row


def test_executable_unknown_path_is_redacted_but_open_inode_hashed(tmp_path):
    p, executable = fixture(tmp_path)
    row = PROBE.process_executable(p/'exe', [64*1024**2])
    assert row['path_class'] == 'unknown' and row['path_class_is_identity'] is False
    assert row['hash_status'] == 'hashed' and row['sha256'] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert row['inode'] == executable.stat().st_ino and 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_known_deleted_exe_path_class_is_not_authenticated_identity(monkeypatch, tmp_path):
    p, _ = fixture(tmp_path)
    monkeypatch.setattr(os, 'readlink', lambda p: '/usr/lib/systemd/systemd-udevd (deleted)')
    row = PROBE.process_executable(p/'exe', [64*1024**2])
    assert row['path_class'] == 'systemd-udevd' and row['deleted'] is True
    assert row['path_class_is_identity'] is False and row['hash_status'] == 'hashed'


def test_executable_permission_failures_are_recorded(monkeypatch, tmp_path):
    def fail(*a, **kw): raise PermissionError('QA_PRIVATE_CANARY')
    monkeypatch.setattr(os, 'readlink', fail); monkeypatch.setattr(os, 'open', fail)
    row = PROBE.process_executable(tmp_path/'exe', [64*1024**2])
    assert row['hash_status'] == row['link_error'] == 'permission-denied'
    assert 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_executable_per_file_and_total_budget_fail_closed(tmp_path):
    p, executable = fixture(tmp_path)
    row = PROBE.process_executable(p/'exe', [1])
    assert row['hash_status'] == 'byte-budget-exceeded' and 'sha256' not in row
    with executable.open('wb') as stream: stream.truncate(16*1024**2+1)
    row = PROBE.process_executable(p/'exe', [64*1024**2])
    assert row['hash_status'] == 'byte-budget-exceeded' and 'sha256' not in row


def test_executable_inode_changes_prevent_stable_hash(monkeypatch, tmp_path):
    p, _ = fixture(tmp_path); original = os.read; changed = False
    def read(fd, count):
        nonlocal changed
        data = original(fd, count)
        if not changed:
            changed = True
            with (tmp_path/'QA_PRIVATE_CANARY').open('ab') as out: out.write(b'changed')
        return data
    monkeypatch.setattr(os, 'read', read)
    row = PROBE.process_executable(p/'exe', [64*1024**2])
    assert row['hash_status'] == 'inode-changed' and 'sha256' not in row


def assert_no_forbidden_process_operations(fn):
    # Only the leading string expression is documentation. Keep all runtime
    # literals, including later/nested string expressions, in the safety scan.
    fn = copy.deepcopy(fn)
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) and isinstance(fn.body[0].value.value, str):
        fn.body = fn.body[1:]
    text = ast.unparse(fn)
    assert not any(s in text for s in ('cmdline', 'environ', 'kill(', 'waitpid', 'sleep(', 'iterdir(', 'listdir('))


def test_process_safety_scan_ignores_only_true_leading_docstring():
    fn = ast.parse('def probe():\n """No cmdline, environ, kill(, waitpid, sleep(, iterdir(, listdir(."""\n return 42\n').body[0]
    before = ast.dump(fn, include_attributes=True)
    assert_no_forbidden_process_operations(fn)
    assert ast.dump(fn, include_attributes=True) == before


def test_process_safety_scan_rejects_forbidden_calls_and_reads():
    for operation in ('p.read_text("cmdline")', 'os.environ', 'os.kill(42, 0)',
                      'os.waitpid(42, 0)', 'time.sleep(1)', 'p.iterdir()', 'os.listdir(p)'):
        fn = ast.parse('def probe():\n """Harmless documentation."""\n '+operation+'\n').body[0]
        with pytest.raises(AssertionError):
            assert_no_forbidden_process_operations(fn)


def test_process_safety_scan_retains_runtime_and_non_docstring_literals():
    for token in ('cmdline', 'environ', 'kill(', 'waitpid', 'sleep(', 'iterdir(', 'listdir('):
        for body in ('value = '+repr(token), 'return '+repr(token),
                     'pass\n '+repr(token), 'if True:\n  '+repr(token),
                     'def nested():\n  '+repr(token)):
            fn = ast.parse('def probe():\n """Harmless documentation."""\n '+body+'\n').body[0]
            with pytest.raises(AssertionError):
                assert_no_forbidden_process_operations(fn)
        for source in ('def probe(value='+repr(token)+'):\n pass\n',
                       'def probe() -> '+repr(token)+':\n pass\n',
                       '@decorate('+repr(token)+')\ndef probe():\n pass\n'):
            with pytest.raises(AssertionError):
                assert_no_forbidden_process_operations(ast.parse(source).body[0])


def test_no_argument_environ_kill_wait_or_process_enumeration_in_probe():
    for name in ('process_error', 'process_stat', 'process_executable', 'process_snapshot'):
        fn = next(n for n in ast.parse(generated()).body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert_no_forbidden_process_operations(fn)
    ns = support()
    fn = next(n for n in ast.parse(generated()).body if isinstance(n, ast.FunctionDef) and n.name == '_diagnostic_trace')
    accesses = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Subscript) and ast.unparse(n.value) == 'frame.f_locals']
    assert accesses == ["frame.f_locals['userspace']"]
    assert ns['_DIAGNOSTIC_PROCESS'] is None


def test_diagnostic_output_remains_review_only_and_replay_forbidden():
    fn = next(n for n in ast.parse(generated()).body if isinstance(n, ast.FunctionDef) and n.name == 'diagnostic_main')
    text = ast.unparse(fn)
    for token in ("'full_acceptance': False", "'review_required': True", "'transaction_executed': False", "'request_reader_executed': False", "row['replay_authorized'] = False", "row['sole_init_process_snapshot'] = _DIAGNOSTIC_PROCESS"):
        assert token in text
