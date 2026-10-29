"""Focused diagnostic regressions; parent executes only these new identities."""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cold_probe_test', REPO/'qa/privsep-storage/cold_probe.py')
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def generated():
    return PROBE.build_probe((REPO/'scripts/storage-maintenance-init.py').read_bytes(),
                             (REPO/'scripts/protected-agent-storage.py').read_bytes()).decode()


def support():
    tree = ast.parse(generated())
    start = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'project_error')
    nodes = [n for n in tree.body if n.lineno >= start and not isinstance(n, ast.If)]
    namespace = {'sys': sys, 'BOOT': Path(PROBE.BOOT)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), PROBE.BOOT, 'exec'), namespace)
    return namespace


def test_bootstrap_code_and_line_numbers_remain_exact():
    old = ast.parse((REPO/'scripts/storage-maintenance-init.py').read_text())
    new = ast.parse(generated())
    for name in ('protected', 'bootstrap', 'main'):
        find = lambda t: next(n for n in t.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert ast.dump(find(old), include_attributes=True) == ast.dump(find(new), include_attributes=True)


def test_bootstrap_source_drift_refuses():
    with pytest.raises(ValueError, match='bootstrap source drift'):
        PROBE.build_probe(b'not reviewed', (REPO/'scripts/protected-agent-storage.py').read_bytes())


def test_product_source_drift_refuses():
    with pytest.raises(ValueError, match='product source drift'):
        PROBE.build_probe((REPO/'scripts/storage-maintenance-init.py').read_bytes(), b'not reviewed')


def test_real_call_trace_stops_before_first_request_reader_line():
    ns = support()
    product = {'marker': []}
    exec(compile('def main():\n marker.append("request-read-or-transaction")\n', PROBE.PRODUCT, 'exec'), product)
    sys.settrace(ns['_diagnostic_trace'])
    try:
        with pytest.raises(ns['_DiagnosticStop']): product['main']()
    finally: sys.settrace(None)
    assert product['marker'] == []


def test_trace_does_not_stop_the_fence_or_other_main():
    ns = support()
    product = {'marker': []}
    exec(compile('def require_fence():\n marker.append("real-fence")\n', PROBE.PRODUCT, 'exec'), product)
    foreign = {'marker': []}
    exec(compile('def main():\n marker.append("other-main")\n', 'untrusted-private-filename', 'exec'), foreign)
    sys.settrace(ns['_diagnostic_trace'])
    try:
        product['require_fence'](); foreign['main']()
    finally: sys.settrace(None)
    assert product['marker'] == ['real-fence'] and foreign['marker'] == ['other-main']


def test_projection_never_uses_exception_text_names_causes_or_locals():
    class QA_PRIVATE_CANARY(Exception): pass
    namespace = {'problem': QA_PRIVATE_CANARY('QA_PRIVATE_CANARY'), 'ValueError': ValueError}
    exec(compile('def forged():\n secret="QA_PRIVATE_CANARY"\n raise problem from ValueError(secret)\n',
                 '/private/QA_PRIVATE_CANARY', 'exec'), namespace)
    try: namespace['forged']()
    except BaseException as exc: row = PROBE.project_error(exc, {})
    assert row == {'class': 'other-exception', 'frames': []}
    assert 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_projection_contains_only_bound_location_and_class():
    namespace = {}
    exec(compile('def protected():\n raise ValueError("QA_PRIVATE_CANARY")\n', PROBE.BOOT, 'exec'), namespace)
    try: namespace['protected']()
    except BaseException as exc:
        row = PROBE.project_error(exc, {PROBE.BOOT: {'protected': [[1, 2]]}})
    assert row == {'class': 'ValueError', 'frames': [{'file': 'maintenance-init', 'function': 'protected', 'line': 2}]}
    assert 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_duplicate_method_names_keep_all_source_ranges():
    assert PROBE.locations('class A:\n def __init__(self): pass\nclass B:\n def __init__(self): pass\n') == {'__init__': [[2, 2], [4, 4]]}


def test_nested_exception_group_payload_cannot_reach_output():
    # Python 3.11+ is the supported Debian saved-guest runtime.
    row = PROBE.project_error(ExceptionGroup('QA_PRIVATE_CANARY', [ValueError('QA_PRIVATE_CANARY')]), {})
    assert row == {'class': 'other-exception', 'frames': []}
    assert 'QA_PRIVATE_CANARY' not in json.dumps(row)


def test_diagnostic_cannot_emit_normal_acceptance_or_resume():
    tree = ast.parse(generated())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'diagnostic_main')
    body = ast.unparse(fn)
    assert "'full_acceptance': False" in body and "'review_required': True" in body
    assert "'transaction_executed': False" in body and "'request_reader_executed': False" in body
    assert 'bootstrap()' in body and 'while True' in body
    assert not any(isinstance(n, ast.Call) and ast.unparse(n.func) in ('os.execv', 'os.execve', 'subprocess.run') for n in ast.walk(tree))
