#!/usr/bin/python3
"""QA-only bootstrap observation. Never install this as a product entrypoint.

REUSE the exact bootstrap and runtime fence; stop at the product main CALL
boundary, before its first line (and therefore before any request read/replay).
Only trusted, source-bound locations and fixed enum labels reach serial.
"""
import ast
import hashlib
import json

BOOT = '/usr/local/lib/wizard-storage/maintenance-init'
PRODUCT = '/usr/local/lib/wizard-storage/protected-agent-storage.py'
BOOT_SHA = 'a62f9886b5b48cbbf1baaeca00957817a79de75c0457eeb704f9b29ad138593e'
PRODUCT_SHA = '5f0cc290e39a2d9b775155f630783052b8dcf0d0d607c0f171c1a8c0a48d7c27'


def locations(source):
    """Allow only real function names/ranges from the sealed reviewed source."""
    tree = ast.parse(source)
    result = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.setdefault(n.name, []).append([n.lineno, n.end_lineno])
    return result


def project_error(exc, allowed):
    # No exception str/repr, locals, source text, chained cause or arbitrary names.
    names = {ValueError: 'ValueError', OSError: 'OSError', FileNotFoundError: 'FileNotFoundError',
             PermissionError: 'PermissionError', KeyError: 'KeyError', TypeError: 'TypeError',
             RuntimeError: 'RuntimeError', SystemExit: 'SystemExit', KeyboardInterrupt: 'KeyboardInterrupt'}
    kind = names.get(type(exc), 'other-exception')
    frames = []
    tb = exc.__traceback__
    while tb:
        code = tb.tb_frame.f_code
        approved = allowed.get(code.co_filename, {})
        spans = approved.get(code.co_name, [])
        if any(span[0] <= tb.tb_lineno <= span[1] for span in spans):
            frames.append({'file': ('maintenance-init' if code.co_filename == str(BOOT) else 'protected-agent-storage.py'),
                           'function': code.co_name, 'line': tb.tb_lineno})
            # Refusal's identity, not an attacker-selected class name.
            if type(exc) is tb.tb_frame.f_globals.get('Refusal'):
                kind = 'Refusal'
        tb = tb.tb_next
    return {'class': kind, 'frames': frames[-16:]}


def build_probe(bootstrap, product):
    if hashlib.sha256(bootstrap).hexdigest() != BOOT_SHA:
        raise ValueError('bootstrap source drift')
    if hashlib.sha256(product).hexdigest() != PRODUCT_SHA:
        raise ValueError('product source drift')
    text = bootstrap.decode()
    footer = "if __name__ == '__main__': sys.exit(main())"
    if text.count(footer) != 1:
        raise ValueError('unexpected bootstrap footer')
    allowed = {BOOT: locations(text), PRODUCT: locations(product.decode())}
    # Embed the exact reviewed projection function; no external import path.
    tree = ast.parse(__import__('pathlib').Path(__file__).read_text())
    projector = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'project_error'))
    support = '''
import json
_DIAGNOSTIC_ALLOWED = @@ALLOWED@@
_DIAGNOSTIC_BOOT = BOOT_VALUE
_DIAGNOSTIC_PRODUCT = PRODUCT_VALUE
class _DiagnosticStop(BaseException): pass
_DIAGNOSTIC_OPERATION = 'bootstrap-invocation'
_DIAGNOSTIC_LAST = None

def _diagnostic_trace(frame, event, arg):
    global _DIAGNOSTIC_OPERATION, _DIAGNOSTIC_LAST
    code = frame.f_code
    if event == 'call' and code.co_filename == _DIAGNOSTIC_PRODUCT and code.co_name == 'main':
        raise _DiagnosticStop()
    spans = _DIAGNOSTIC_ALLOWED.get(code.co_filename, {})
    approved = spans.get(code.co_name, [])
    if event == 'line' and any(span[0] <= frame.f_lineno <= span[1] for span in approved):
        _DIAGNOSTIC_LAST = {'file': ('maintenance-init' if code.co_filename == _DIAGNOSTIC_BOOT else 'protected-agent-storage.py'),
                            'function': code.co_name, 'line': frame.f_lineno}
        if code.co_filename == _DIAGNOSTIC_BOOT and code.co_name == 'bootstrap':
            _DIAGNOSTIC_OPERATION = OPERATIONS.get(frame.f_lineno, _DIAGNOSTIC_OPERATION)
    return _diagnostic_trace

def diagnostic_main():
    row = {'diagnostic': 'storage-cold-bootstrap-v1', 'full_acceptance': False,
           'review_required': True, 'transaction_executed': False,
           'request_reader_executed': False}
    sys.settrace(_diagnostic_trace)
    try:
        bootstrap()
        row['state'] = 'unexpected-bootstrap-return'
    except _DiagnosticStop:
        row['state'] = 'pre-request-stop'
    except BaseException as exc:
        row['state'] = 'bootstrap-refused'
        row['error'] = project_error(exc, _DIAGNOSTIC_ALLOWED)
    finally:
        sys.settrace(None)
    row['operation'] = _DIAGNOSTIC_OPERATION
    row['last_location'] = _DIAGNOSTIC_LAST
    print(json.dumps(row, sort_keys=True), flush=True)
    while True: time.sleep(3600)

if __name__ == '__main__': diagnostic_main()
'''
    # Exact pinned original line numbers; no request-derived operation labels.
    operations = {43: 'root-pid1', 45: 'isolated-invocation', 47: 'bootstrap-abi',
                  50: 'protected-code', 52: 'load-product', 54: 'proc-open',
                  55: 'proc-filesystem', 57: 'proc-empty', 58: 'proc-mount',
                  67: 'runtime-fence', 68: 'writable-root', 70: 'entry-admitted',
                  71: 'product-main-call-stopped'}
    support = support.replace('@@ALLOWED@@', repr(allowed), 1).replace('BOOT_VALUE', repr(BOOT), 1).replace('PRODUCT_VALUE', repr(PRODUCT), 1).replace('OPERATIONS', repr(operations), 1)
    # project_error uses BOOT already defined by the original bootstrap.
    result = text.replace(footer, projector + '\n' + support)
    original = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == 'bootstrap')
    generated = next(n for n in ast.parse(result).body if isinstance(n, ast.FunctionDef) and n.name == 'bootstrap')
    if ast.dump(original, include_attributes=True) != ast.dump(generated, include_attributes=True):
        raise ValueError('bootstrap diagnostic changed original code or locations')
    return result.encode()


def metadata_snapshot():
    """Fixed synthetic metadata only; never report record contents or home names.

    All journal/control existence remains visible even when no completion marker
    exists. No result from this function authorizes transaction replay.
    """
    import os
    from pathlib import Path
    import pwd
    import stat
    request = Path('/etc/wizard-agent-storage/request.json')
    uid = pwd.getpwnam('hermes').pw_uid
    fixed = {'request': request, 'completion': request.parent/'completed.json',
             'anchor': Path('/var/lib/wizard-agent-homes'),
             'control': Path('/var/lib/wizard-agent-storage'),
             'journal': Path('/var/lib/wizard-agent-storage')/str(uid)/'journal.json',
             'credential_journal': Path('/var/lib/wizard-os-migration/credentials/journal.json'),
             'logical': Path('/home/hermes/.hermes')}
    result = {}
    for label, path in fixed.items():
        try:
            st = path.lstat()
            row = {'exists': True, 'uid': st.st_uid, 'gid': st.st_gid,
                   'mode': stat.S_IMODE(st.st_mode), 'kind': stat.S_IFMT(st.st_mode),
                   'size': st.st_size, 'links': st.st_nlink}
        except FileNotFoundError:
            row = {'exists': False}
        result[label] = row
    # Content is compared locally to the one fixed synthetic request, never echoed.
    row = result['request']
    if row.get('kind') == stat.S_IFREG and row['size'] <= 1024 and row['links'] == 1:
        fd = os.open(request, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            value = json.loads(os.read(fd, 1025))
            result['request_matches_fixed_synthetic'] = value == {'action': 'relocate', 'agent': 'hermes', 'logical': '/home/hermes/.hermes'}
        except (ValueError, UnicodeError):
            result['request_matches_fixed_synthetic'] = False
        finally: os.close(fd)
    else:
        result['request_matches_fixed_synthetic'] = False
    result['durable_state_requires_review'] = any(result[n]['exists'] for n in ('completion', 'anchor', 'control', 'journal', 'credential_journal'))
    result['replay_authorized'] = False
    return result
