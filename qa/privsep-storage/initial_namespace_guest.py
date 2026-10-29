#!/usr/bin/python3 -I
"""Focused isolated fence proof; only the parent may admit VM execution.

Candidate require_fence and a one-call-removal source mutant are called read-only.
No Transaction, original evidence rewrite, proc alias or bootarg forgery.
"""
import ast
import base64
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal

import subprocess
import sys
import time
import traceback
from typing import Any

ROOT = Path('/root/storage-proof')
PRODUCT = ROOT/'source/scripts/protected-agent-storage.py'
SELF = ROOT/'namespace-diagnostic.py'
CHECKER = ROOT/'namespace-check.py'
MUTANT = ROOT/'namespace-identity-removed.py'
CONTROLS = ('/var/lib/wizard-agent-homes', '/var/lib/wizard-agent-storage',
            '/var/lib/wizard-os-migration/credentials')
EXPECTED = ('ordinary-initial-process', 'ordinary-original-nested-check',
            'ordinary-nested-pid1', 'cold-initial-sole-init',
            'cold-initial-foreign-userspace', 'cold-original-nested-check',
            'cold-nested-hidden-foreign', 'cold-initial-after-reap')
REQUIRED = ('kernel', 'pid', 'ppid', 'uid', 'euid', 'uid_map', 'capabilities',
            'cmdline', 'namespaces', 'self_pid_parent', 'userspace_census',
            'task_ids', 'fence', 'controls_absent', 'errors')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persist(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2)+'\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def ioctl_parent(path):
    row = {'path': str(path), 'success': False, 'errno': None,
           'errno_name': None, 'parent_identity': None, 'error_type': None}
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        parent = fcntl.ioctl(fd, 0xb702)
        try:
            st = os.fstat(parent)
            row.update(success=True, parent_identity=[st.st_dev, st.st_ino])
        finally: os.close(parent)
    except OSError as exc:
        import errno
        row.update(errno=exc.errno, errno_name=errno.errorcode.get(exc.errno or 0),
                   error_type=type(exc).__name__)
    finally:
        if fd is not None: os.close(fd)
    return row


def snapshot(name):
    row: dict[str, Any] = {key: None for key in REQUIRED}
    row.update(name=name, errors=[], diagnostic_only=False, focused_fence_proof=True,
               storage_acceptance=False, pid=os.getpid(), ppid=os.getppid(),
               uid=os.getuid(), euid=os.geteuid())
    def field(key, fn):
        try: row[key] = fn()
        except Exception as exc:
            row['errors'].append({'field': key, 'type': type(exc).__name__, 'reason': str(exc)})
    field('kernel', lambda: dict(zip(('sysname', 'nodename', 'release', 'version', 'machine'), os.uname())))
    field('uid_map', lambda: Path('/proc/self/uid_map').read_text())
    def capabilities():
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        keys = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb', 'NSpid', 'NoNewPrivs', 'Seccomp')
        return {key: status[key].strip() for key in keys}
    field('capabilities', capabilities)
    field('cmdline', lambda: Path('/proc/cmdline').read_text().strip())
    def namespaces():
        result = {}
        for key in ('pid', 'user', 'mnt'):
            path = Path('/proc/self/ns')/key
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                st = os.fstat(fd)
                # Independent overallocated, aligned LP64 statfs observation.
                buf = (ctypes.c_long * 32)()
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.fstatfs(fd, ctypes.byref(buf)): raise OSError(ctypes.get_errno())
                result[key] = {'identity': [st.st_dev, st.st_ino],
                    'link': os.readlink(path), 'nsfs_magic': buf[0],
                    'nstype': fcntl.ioctl(fd, 0xb703)}
            finally: os.close(fd)
        return result
    field('namespaces', namespaces)
    field('self_pid_parent', lambda: ioctl_parent('/proc/self/ns/pid'))
    def census():
        rows = []
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal(): continue
            fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            if not int(fields[6]) & 0x00200000:
                rows.append({'pid': int(path.name), 'state': fields[0], 'flags': int(fields[6])})
        return sorted(rows, key=lambda value: value['pid'])
    field('userspace_census', census)
    field('task_ids', lambda: sorted(int(path.name) for path in Path('/proc/self/task').iterdir()))
    def fence(path=PRODUCT):
        spec = importlib.util.spec_from_file_location('storage_fence_probe', path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        try: return {'admitted': True, 'refusal': None, 'value': module.require_fence(), 'error_type': None}
        except module.Refusal as exc:
            return {'admitted': False, 'refusal': str(exc), 'value': None, 'error_type': None}
        except Exception as exc:
            return {'admitted': None, 'refusal': None, 'value': None,
                    'error_type': type(exc).__name__, 'error': str(exc)}
    field('fence', fence)
    if name == 'cold-nested-hidden-foreign':
        field('identity_guard_removed_fence', lambda: fence(MUTANT))
        row['mutation_sha256'] = digest(MUTANT)
    field('controls_absent', lambda: {path: not os.path.lexists(path) for path in CONTROLS})
    row['collection_complete'] = all(row[key] is not None for key in REQUIRED) and not row['errors']
    row['product_sha256'] = digest(PRODUCT)
    return row


def capture(argv, timeout=30, **kwargs):
    row = {'argv': [str(v) for v in argv], 'exit': None, 'stdout_b64': None,
           'stderr_b64': None, 'timed_out': False, 'error': None}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, **kwargs)
    try:
        try: stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            row['timed_out'] = True; os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate(timeout=5)
        row.update(exit=proc.returncode, stdout_b64=base64.b64encode(stdout).decode(),
                   stderr_b64=base64.b64encode(stderr).decode())
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=5)
    return row


def original(name):
    argv = ['unshare', '--pid', '--fork', '--mount-proc', '/usr/bin/python3', '-I', str(CHECKER), str(PRODUCT)]
    row = capture(argv)
    row.update(name=name, diagnostic_only=False, focused_fence_proof=True, storage_acceptance=False,
               original_assertion_satisfied=(row['exit'] == 0 and
                   base64.b64decode(row['stdout_b64']).strip() == b'NESTED_REFUSED'))
    return row


def nested(name):
    ready_r, ready_w = os.pipe(); release_r, release_w = os.pipe()
    proc = None
    row = {'name': name, 'exit': None, 'stdout_b64': None, 'stderr_b64': None,
           'timed_out': False, 'snapshot': None, 'outer_child_pid': None,
           'parent_scope_ioctl': None, 'error': None, 'diagnostic_only': False, 'focused_fence_proof': True,
           'storage_acceptance': False, 'fixture': None}
    try:
        proc = subprocess.Popen(['unshare', '--pid', '--fork', '--mount-proc',
            '/usr/bin/python3', '-I', str(SELF), 'probe', name, str(ready_w), str(release_r)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(ready_w, release_r), start_new_session=True)
        os.close(ready_w); ready_w = None; os.close(release_r); release_r = None
        if not select.select([ready_r], [], [], 20)[0] or os.read(ready_r, 1) != b'R':
            raise TimeoutError('nested readiness absent; retain child output')
        children = Path(f'/proc/{proc.pid}/task/{proc.pid}/children').read_text().split()
        if len(children) != 1: raise AssertionError('unshare child identity is not unique')
        row['outer_child_pid'] = int(children[0])
        row['parent_scope_ioctl'] = ioctl_parent('/proc/'+children[0]+'/ns/pid')
        os.write(release_w, b'C')
        stdout, stderr = proc.communicate(timeout=10)
        row.update(exit=proc.returncode, stdout_b64=base64.b64encode(stdout).decode(),
                   stderr_b64=base64.b64encode(stderr).decode())
        row['snapshot'] = json.loads(stdout)
        value = row['snapshot']
        initial = os.stat('/proc/self/ns/pid')
        initial_user = os.stat('/proc/self/ns/user')
        initial_id = [initial.st_dev, initial.st_ino]
        row['fixture'] = {
            'pid_is_one': value['pid'] == 1,
            'pid_namespace_differs_from_parent': value['namespaces']['pid']['identity'] != initial_id,
            'same_user_namespace_as_parent': value['namespaces']['user']['identity'] == [initial_user.st_dev, initial_user.st_ino],
            'parent_scope_ioctl_reaches_initial': row['parent_scope_ioctl']['success'] and row['parent_scope_ioctl']['parent_identity'] == initial_id,
            'same_actual_kernel_cmdline': value['cmdline'] == Path('/proc/cmdline').read_text().strip(),
            'only_nested_userspace_pid_one_visible': [v['pid'] for v in value['userspace_census']] == [1]}
    except Exception as exc:
        row['error'] = {'type': type(exc).__name__, 'reason': str(exc)}
        row['timed_out'] = isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
        if proc is not None:
            if proc.poll() is None: os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate(timeout=5)
            row.update(exit=proc.returncode, stdout_b64=base64.b64encode(stdout).decode(),
                       stderr_b64=base64.b64encode(stderr).decode())
    finally:
        for fd in (ready_r, ready_w, release_r, release_w):
            if fd is not None: os.close(fd)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=5)
    return row


def prepare(rows):
    # Remove exactly one namespace-identity guard call; all other checks remain.
    text = PRODUCT.read_text()
    old = '    pid_namespace = require_initial_pid_namespace()\n'
    assert text.count(old) == 1
    mutated = text.replace(old, "    pid_namespace = {'guard_removed_negative_control': True}\n")
    ast.parse(mutated)
    MUTANT.write_text(mutated); MUTANT.chmod(0o600)
    tree = ast.parse((ROOT/'source/qa/privsep-storage/guest.py').read_text())
    found = [node.args[0].value for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'write_text' and 'namespace-check.py' in ast.unparse(node.func.value)
             and node.args and isinstance(node.args[0], ast.Constant)]
    assert len(found) == 1 and isinstance(found[0], str)
    CHECKER.write_text(found[0]); CHECKER.chmod(0o600)
    (ROOT/'init.sh').write_text('#!/bin/sh\nmount -t proc proc /proc 2>/dev/null || :\n'
        'mount -t sysfs sysfs /sys 2>/dev/null || :\nmount -o remount,rw /\n'
        'exec /usr/bin/python3 -I /root/storage-proof/namespace-diagnostic.py cold\n')
    (ROOT/'init.sh').chmod(0o700)
    rows.append(snapshot(EXPECTED[0]))
    rows.append(original(EXPECTED[1]))
    rows.append(nested(EXPECTED[2]))


def cold(rows):
    assert os.getpid() == 1
    rows.append(snapshot(EXPECTED[3]))
    rd, wr = os.pipe(); child = os.fork()
    if child == 0:
        os.close(wr)
        os.read(rd, 1); os.close(rd); os._exit(0)
    os.close(rd)
    try:
        # The helper is alive in the real initial namespace throughout both
        # nested invocations; their freshly mounted proc has no view of it.
        rows.append(snapshot(EXPECTED[4]))
        rows.append(original(EXPECTED[5]))
        value = nested(EXPECTED[6]); rows.append(value)
        os.kill(child, 0)
        value['foreign_outer_pid'] = child
        value['foreign_alive_after_nested'] = True
        value['outer_initial_pid_namespace'] = rows[0].get('namespaces')

    finally:
        os.close(wr); waited, status = os.waitpid(child, 0)
    rows.append(snapshot(EXPECTED[7]))
    rows[-1]['foreign_reaped'] = waited == child and os.waitstatus_to_exitcode(status) == 0



def main():
    os.umask(0o077)
    assert os.getuid() == os.geteuid() == 0 and ROOT.is_dir()
    for path in (ROOT, PRODUCT, SELF):
        st = path.stat(); assert st.st_uid == 0 and not st.st_mode & 0o022
    mode = sys.argv[1]
    if mode == 'probe':
        row = snapshot(sys.argv[2]); print(json.dumps(row, sort_keys=True), flush=True)
        os.write(int(sys.argv[3]), b'R'); os.close(int(sys.argv[3]))
        assert os.read(int(sys.argv[4]), 1) == b'C'; os.close(int(sys.argv[4])); return 0
    assert mode in ('prepare', 'cold')
    report = {'schema': 'initial-pid-repair-proof-v1', 'mode': mode, 'diagnostic_only': False, 'focused_fence_proof': True,
              'storage_acceptance': False, 'full_integration_acceptance': False,
              'review_required': True, 'first_error': None, 'rows': [],
              'product_sha256': digest(PRODUCT), 'guest_sha256': digest(SELF),
              'mutation_sha256': None}
    try:
        if mode == 'prepare': prepare(report['rows'])
        else: cold(report['rows'])
    except BaseException as exc:
        report['first_error'] = {'type': type(exc).__name__, 'reason': str(exc),
            'frames': [{'function': frame.name, 'line': frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)]}
    report['mutation_sha256'] = digest(MUTANT) if MUTANT.exists() else None
    report['checker_sha256'] = digest(CHECKER) if CHECKER.exists() else None
    persist(ROOT/(mode+'-initial-pid-repair.json'), report)
    print('INITIAL_PID_REPAIR_JSON:'+base64.b64encode(json.dumps(report, sort_keys=True).encode()).decode(), flush=True)
    if mode == 'cold':
        os.sync(); ctypes.CDLL(None).reboot(0x4321fedc)
        while True: time.sleep(1)
    return 0 if report['first_error'] is None else 1


if __name__ == '__main__': sys.exit(main())
