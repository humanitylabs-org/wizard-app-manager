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


def process_error(exc):
    """Only fixed failure labels; never exception text or arbitrary attributes."""
    if isinstance(exc, (FileNotFoundError, ProcessLookupError)): return 'missing-or-exited'
    if isinstance(exc, PermissionError): return 'permission-denied'
    if isinstance(exc, OSError): return 'os-error'
    if isinstance(exc, (ValueError, IndexError, UnicodeError)): return 'malformed-or-bound'
    return 'probe-error'


def process_stat(path, pid):
    """Bounded proc stat; omit comm entirely, including embedded right parens."""
    import os
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try: raw = os.read(fd, 4097)
    finally: os.close(fd)
    if len(raw) > 4096: raise ValueError('bound')
    head, tail = raw.rsplit(b')', 1)
    if int(head.split(b'(', 1)[0]) != pid: raise ValueError('pid')
    fields = tail.split()
    state = fields[0].decode('ascii')
    if state not in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I'):
        raise ValueError('state')
    # field 3 is state; field 9 is flags; field 22 is starttime.
    ppid, flags, start = int(fields[1]), int(fields[6]), int(fields[19])
    if min(ppid, flags, start) < 0 or max(ppid, flags, start) > 2**64-1:
        raise ValueError('integer')
    # Fixed comm classification can still help when a zombie has no exe link.
    # Like path_class this is not authenticated identity; arbitrary comm is omitted.
    comm = head.split(b'(', 1)[1]
    classes = {b'systemd-udevd': 'systemd-udevd', b'udev-worker': 'udev-worker',
               b'udevadm': 'udevadm', b'run-init': 'run-init', b'sh': 'shell',
               b'busybox': 'busybox'}
    return {'pid': pid, 'ppid': ppid, 'state': state, 'flags': flags,
            'comm_class': classes.get(comm, 'unknown'), 'comm_class_is_identity': False,
            'pf_kthread': bool(flags & 0x00200000), 'start_ticks': start}


def process_executable(path, budget):
    """Fixed path classifications are clues, NOT service identity/admission.

    The proc exe magic link is intentionally followed to hash its opened inode.
    Never open the path returned by readlink, nor report arbitrary link text.
    """
    import hashlib
    import os
    import stat
    labels = {'/usr/lib/systemd/systemd-udevd': 'systemd-udevd',
              '/lib/systemd/systemd-udevd': 'systemd-udevd',
              '/sbin/udevd': 'udevd', '/usr/sbin/udevd': 'udevd',
              '/bin/udevadm': 'udevadm', '/usr/bin/udevadm': 'udevadm',
              '/sbin/udevadm': 'udevadm', '/usr/sbin/udevadm': 'udevadm',
              '/bin/run-init': 'run-init', '/usr/bin/run-init': 'run-init',
              '/bin/busybox': 'busybox', '/usr/bin/busybox': 'busybox'}
    row = {'path_class': 'unknown', 'path_class_is_identity': False}
    try:
        target = os.readlink(path)
        if len(target) > 4096: raise ValueError('bound')
        deleted = target.endswith(' (deleted)')
        if deleted: target = target[:-10]
        row.update(path_class=labels.get(target, 'unknown'), deleted=deleted)
    except Exception as exc: row['link_error'] = process_error(exc)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            row['hash_status'] = 'not-regular'; return row
        row.update(device=st.st_dev, inode=st.st_ino, uid=st.st_uid,
                   mode=stat.S_IMODE(st.st_mode), size=st.st_size)
        if st.st_size > min(16*1024**2, budget[0]):
            row['hash_status'] = 'byte-budget-exceeded'; return row
        h = hashlib.sha256()
        size = 0
        # At most 257 reads per executable, 64MiB for the whole snapshot.
        for _ in range(257):
            left = min(16*1024**2-size, budget[0])
            if left <= 0:
                # fstat size and exact byte count still cannot exclude growth.
                if size == st.st_size and os.fstat(fd).st_size == size:
                    break
                row['hash_status'] = 'byte-budget-exceeded'; return row
            chunk = os.read(fd, min(65536, left))
            if not chunk: break
            budget[0] -= len(chunk); size += len(chunk); h.update(chunk)
        else:
            row['hash_status'] = 'read-bound-exceeded'; return row
        after = os.fstat(fd)
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or size != st.st_size:
            row['hash_status'] = 'inode-changed'; return row
        row.update(hash_status='hashed', sha256=h.hexdigest())
    except Exception as exc: row['hash_status'] = process_error(exc)
    finally:
        if fd is not None: os.close(fd)
    return row


def process_snapshot(candidates, proc='/proc'):
    """One finite read of ONLY the exact guard's list after its Refusal.

    No process enumeration, arguments, environ, status, arbitrary locals,
    waiting, retry, signalling, reaping, or admission decision occurs here.
    """
    from pathlib import Path
    row = {'status': 'captured', 'source': 'require_fence:258:Refusal',
           'replay_authorized': False, 'processes': []}
    if type(candidates) is not list:
        row['status'] = 'invalid-candidate-list'; return row
    row['candidate_count'] = len(candidates)
    if len(candidates) > 64:
        row['status'] = 'candidate-bound-exceeded'; return row
    if any(type(p) is not int or not 1 < p <= 2**31-1 for p in candidates) or len(set(candidates)) != len(candidates):
        row['status'] = 'invalid-candidate-list'; return row
    row['candidate_pids'] = list(candidates)
    budget = [64*1024**2]
    for pid in candidates:
        p = Path(proc)/str(pid)
        item = {'pid': pid}
        try: item['stat_before'] = process_stat(p/'stat', pid)
        except Exception as exc: item['stat_before_error'] = process_error(exc)
        item['executable'] = process_executable(p/'exe', budget)
        try: item['stat_after'] = process_stat(p/'stat', pid)
        except Exception as exc: item['stat_after_error'] = process_error(exc)
        if 'stat_before' in item and 'stat_after' in item:
            item['same_pid_lifetime'] = item['stat_before']['start_ticks'] == item['stat_after']['start_ticks']
            item['stat_changed'] = item['stat_before'] != item['stat_after']
        else: item['same_pid_lifetime'] = False
        row['processes'].append(item)
    row['executable_bytes_read'] = 64*1024**2-budget[0]
    return row


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
    projector = '\n\n'.join(ast.unparse(n) for n in tree.body if isinstance(n, ast.FunctionDef)
        and n.name in ('project_error', 'process_error', 'process_stat', 'process_executable', 'process_snapshot'))
    support = '''
import json
_DIAGNOSTIC_ALLOWED = @@ALLOWED@@
_DIAGNOSTIC_BOOT = BOOT_VALUE
_DIAGNOSTIC_PRODUCT = PRODUCT_VALUE
class _DiagnosticStop(BaseException): pass
_DIAGNOSTIC_OPERATION = 'bootstrap-invocation'
_DIAGNOSTIC_LAST = None
_DIAGNOSTIC_PROCESS = None

def _diagnostic_trace(frame, event, arg):
    global _DIAGNOSTIC_OPERATION, _DIAGNOSTIC_LAST, _DIAGNOSTIC_PROCESS
    code = frame.f_code
    if event == 'call' and code.co_filename == _DIAGNOSTIC_PRODUCT and code.co_name == 'main':
        raise _DiagnosticStop()
    # Observe only AFTER the real reviewed guard has raised, never before census.
    if (event == 'exception' and code.co_filename == _DIAGNOSTIC_PRODUCT
            and code.co_name == 'require_fence' and frame.f_lineno == 258
            and code is getattr(frame.f_globals.get('require_fence'), '__code__', None)
            and type(arg[1]) is frame.f_globals.get('Refusal')
            and _DIAGNOSTIC_PROCESS is None):
        try: _DIAGNOSTIC_PROCESS = process_snapshot(frame.f_locals['userspace'])
        except Exception as exc:
            _DIAGNOSTIC_PROCESS = {'status': process_error(exc), 'replay_authorized': False}
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
    row['sole_init_process_snapshot'] = _DIAGNOSTIC_PROCESS
    row['replay_authorized'] = False
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
