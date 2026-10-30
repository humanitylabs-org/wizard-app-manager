#!/usr/bin/python3
"""One stopped-610 log projection. Parent-admitted forum job ONLY.

No installer, Docker, NBD, network, normal guest boot, raw log export, retries,
image overlay, filesystem repair, or product mutation. --preflight is metadata
only. Runtime and tests MUST be admitted by the parent via the heavy worker.
"""
import argparse
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import stat
import subprocess
import sys
import time

SOURCE_FILES = ('qa/privsep-storage/installer_diagnostic_run.py',
                'qa/privsep-storage/installer_diagnostic_parser.py',
                'qa/privsep-storage/installer_diagnostic_guest.py',
                'tests/test_installer_diagnostic_parser.py')
RESERVE = 217179869184
PENDING_MAX = 67108864
SERIAL_CAP = 2097152
DEADLINE_SECONDS = 600


def digest(path):
    h = hashlib.sha256()
    deadline = time.monotonic() + 60
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if os.lstat(path).st_uid == os.geteuid():
        flags |= os.O_NOATIME
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as f:
        for data in iter(lambda: f.read(1048576), b''):
            if time.monotonic() > deadline:
                raise TimeoutError('hash-deadline')
            h.update(data)
    return h.hexdigest()


def identity(path):
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode)
    return {k: getattr(st, k) for k in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size', 'st_blocks', 'st_mtime_ns', 'st_ctime_ns')}


def clean_ancestors(path):
    p = Path(path)
    assert p.is_absolute()
    for ancestor in (*reversed(p.parents), p):
        assert not ancestor.is_symlink()


def load_parser(root):
    spec = importlib.util.spec_from_file_location('installer_projection', root/SOURCE_FILES[1])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory(root):
    result = []
    for p in sorted(root.rglob('*')):
        rel = str(p.relative_to(root))
        st = p.lstat()
        if p.is_symlink():
            result.append({'path': rel, 'kind': 'symlink', 'target': os.readlink(p), 'mode': stat.S_IMODE(st.st_mode)})
        elif p.is_file():
            result.append({'path': rel, 'kind': 'file', 'sha256': digest(p), 'mode': stat.S_IMODE(st.st_mode)})
        elif not p.is_dir():
            raise ValueError('source-special-member')
    return sorted(result, key=lambda row: row['path'])


def preflight(stage, manifest_sha, *, full=True):
    clean_ancestors(stage/'manifest.json')
    assert digest(stage/'manifest.json') == manifest_sha
    m = json.loads((stage/'manifest.json').read_text())
    assert m['schema'] == 'installer-forensic-manifest-v1'
    assert m['scope'] == 'stopped-610-single-log-readonly'
    assert m['reserve_bytes'] == RESERVE and m['pending_max_bytes'] == PENDING_MAX
    assert len(m['inputs']) == 6 and len(set(x['path'] for x in m['inputs'])) == 6
    assert inventory(stage/'source') == json.loads((stage/'source-members.json').read_text())
    assert digest(stage/'source-members.json') == m['source_members_sha256']
    assert digest(stage/'source.tar') == m['source_tar_sha256']
    assert digest(stage/'source.bundle') == m['source_bundle_sha256']
    for x in m['inputs']:
        clean_ancestors(x['path'])
        assert identity(x['path']) == x['stat']
        if full:
            assert digest(x['path']) == x['sha256']
    assert not Path('/proc/3926665').exists()
    fs = os.statvfs(stage)
    free = fs.f_bavail * fs.f_frsize
    assert free >= RESERVE + PENDING_MAX
    for x in m['tools']:
        clean_ancestors(x['path'])
        assert identity(x['path']) == x['stat'] and digest(x['path']) == x['sha256']
    # Inherit the completed RO proof, not a claimed root cause. Hash-only reads.
    for x in m['inherited_diagnostic']['artifacts']:
        clean_ancestors(x['path'])
        assert identity(x['path']) == x['stat'] and digest(x['path']) == x['sha256']
    assert Path(sys.executable).resolve() == Path(m['python']).resolve()
    module = load_parser(stage/'source')
    vocab = module.repertoire({p: (stage/'source'/p).read_text() for p in module.SOURCE_PATHS})
    assert vocab == m['vocabulary']
    return m, module, {'free_bytes': free, 'required_bytes': RESERVE + PENDING_MAX,
                       'source_revision': m['source_revision'], 'all_inputs_bound': True}


def qemu_argv(m, fds):
    # Explicit node graph, no implicit backing-file opens. Every layer is RO.
    argv = ['/usr/bin/qemu-system-x86_64', '-no-user-config', '-nodefaults',
            '-machine', 'q35,accel=kvm,dump-guest-core=off,mem-merge=off',
            '-cpu', 'host', '-smp', '1', '-m', '1536', '-display', 'none',
            '-monitor', 'none', '-serial', 'stdio', '-nic', 'none', '-no-reboot',
            '-sandbox', 'on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny']
    for i in range(3, -1, -1):
        file_node = {'driver': 'file', 'node-name': 'f'+str(i),
                     'filename': '/proc/self/fd/'+str(fds[i]), 'read-only': True,
                     'auto-read-only': False, 'locking': 'on'}
        block_node = {'driver': 'qcow2', 'node-name': 'b'+str(i), 'file': 'f'+str(i),
                      'read-only': True, 'auto-read-only': False,
                      'backing': 'b'+str(i+1) if i < 3 else None}
        argv += ['-blockdev', json.dumps(file_node), '-blockdev', json.dumps(block_node)]
    argv += ['-device', 'virtio-blk-pci,drive=b1,serial=WZ_MAINT_851_RO,bus=pcie.0,addr=0x5',
             '-device', 'virtio-blk-pci,drive=b0,serial=WZ_FAILED_610_RO,bus=pcie.0,addr=0x6',
             '-kernel', '/proc/self/fd/'+str(fds[4]), '-initrd', '/proc/self/fd/'+str(fds[5]),
             '-append', 'root=/dev/vda1 ro rootfstype=ext4 rootflags=noload console=ttyS0 init=/bin/sh ip=off fsck.mode=skip panic=-1']
    return argv


def namespace_launcher(stage, manifest_sha, fdtext, nstext):
    """Only reached INSIDE an isolated user/mount/net namespace after admission."""
    m = json.loads((stage/'manifest.json').read_text())
    assert digest(stage/'manifest.json') == manifest_sha
    fds = [int(n) for n in fdtext.split(',')]
    parent_ns = json.loads(nstext)
    assert len(fds) == len(m['inputs']) == 6
    private = {k: os.stat('/proc/self/ns/'+k).st_ino for k in ('mnt', 'user', 'net', 'pid')}
    assert all(private[k] != parent_ns[k] for k in private)
    assert os.getpid() == 1
    for i, row in enumerate(m['inputs']):
        assert fcntl.fcntl(fds[i], fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        st = os.fstat(fds[i])
        assert (st.st_dev, st.st_ino) == (row['stat']['st_dev'], row['stat']['st_ino'])
        # Bind only exact regular image/kernel files in THIS private namespace.
        # Remount them kernel-readonly; capabilities are then irrevocably dropped.
        for cmd in (['/usr/bin/mount', '--bind', '/proc/self/fd/'+str(fds[i]), row['path']],
                    ['/usr/bin/mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec', row['path']]):
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, timeout=10, pass_fds=fds)
            assert p.returncode == 0
        assert os.statvfs(row['path']).f_flag & os.ST_RDONLY
        # Replace the pre-bind inherited descriptor: its new struct file must
        # itself refer to the RO bind mount, not the earlier writable mount.
        newfd = os.open(row['path'], os.O_RDONLY | os.O_NOFOLLOW)
        os.dup2(newfd, fds[i], inheritable=True)
        os.close(newfd)
        assert os.fstatvfs(fds[i]).f_flag & os.ST_RDONLY
        assert fcntl.fcntl(fds[i], fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    print('WZ_NAMESPACE='+json.dumps({'private': private, 'readonly_input_fds': True,
          'all_exact_input_binds_ro': True}, separators=(',', ':')), flush=True)
    argv = ['/usr/bin/setpriv', '--no-new-privs', '--bounding-set=-all',
            '--inh-caps=-all', '--ambient-caps=-all', *qemu_argv(m, fds)]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execve(argv[0], argv, {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
                             'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(stage/'run')})


def get_frame(stream, deadline, prefix, state):
    """Bounded PRIVATE in-memory console. Never print/persist unfiltered bytes."""
    sel = selectors.DefaultSelector()
    sel.register(stream, selectors.EVENT_READ)
    buf = state.setdefault('pending', bytearray())
    try:
        while time.monotonic() < deadline:
            # Prompt matching is restricted to the end of the current console.
            if prefix == b'# ' and buf.endswith(prefix):
                buf.clear()
                return b''
            if prefix != b'# ':
                while b'\n' in buf:
                    line, _, rest = buf.partition(b'\n')
                    buf[:] = rest
                    line = line.rstrip(b'\r')
                    if line.startswith(prefix):
                        return bytes(line[len(prefix):])
            ready = sel.select(min(1, max(0, deadline-time.monotonic())))
            if not ready:
                continue
            data = os.read(stream.fileno(), 4096)
            if not data:
                raise EOFError('private-channel-closed')
            state['bytes'] = state.get('bytes', 0) + len(data)
            if state['bytes'] > SERIAL_CAP:
                raise ValueError('private-channel-cap')
            buf.extend(data)
            assert len(buf) <= SERIAL_CAP
        raise TimeoutError('private-channel-deadline')
    finally:
        sel.close()


def send_private(stream, data, deadline):
    fd = stream.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_WRITE)
    try:
        offset = 0
        while offset < len(data):
            if time.monotonic() >= deadline:
                raise TimeoutError('private-send-deadline')
            if sel.select(min(1, max(0, deadline-time.monotonic()))):
                try:
                    offset += os.write(fd, data[offset:offset+4096])
                except BlockingIOError:
                    pass
    finally:
        sel.close()
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)


def child_death_fence(parent):
    # No detached VM survives an abrupt worker/runner death. unshare itself
    # sets the corresponding KILL-on-parent-death fence on its QEMU child.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(125)


def execute(stage, sha):
    os.umask(0o077)
    m, module, before = preflight(stage, sha)
    run = stage/'run'
    run.mkdir(mode=0o700)  # one-use; NEVER reuse a prior attempt
    receipt = {'schema': 'installer-forensic-receipt-v1', 'review_required': True,
        'full_acceptance': False, 'manifest_sha256': sha, 'preflight': before,
        'retained_105_tests': 'inherited-only-not-rerun', 'prior_native': 'partial-only',
        'submitted_source_revision': m['source_revision'], 'diagnosis': 'unknown',
        'qemu_launch_count': 0, 'cleanup': {'qemu_reaped': True}}
    proc = None
    fds = []
    def save():
        (run/'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True)+'\n')
    save()
    try:
        qa = subprocess.run([m['python'], '-I', '-B', str(stage/'source'/SOURCE_FILES[3])],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45,
            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(run)}, check=False)
        receipt['fresh_synthetic_test_exit'] = qa.returncode
        save()
        qa_summary = json.loads(qa.stdout)
        assert qa.returncode == 0 and qa_summary == {'tests': 35, 'failures': 0, 'errors': 0, 'skipped': 0}
        receipt['fresh_synthetic_tests'] = qa_summary
        save()
        for row in m['inputs']:
            fd = os.open(row['path'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
            fds.append(fd)
            assert identity(row['path']) == row['stat']
            assert (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (row['stat']['st_dev'], row['stat']['st_ino'])
        parent_ns = {k: os.stat('/proc/self/ns/'+k).st_ino for k in ('mnt', 'user', 'net', 'pid')}
        argv = ['/usr/bin/unshare', '--user', '--map-root-user', '--mount', '--net',
                '--pid', '--mount-proc',
                '--propagation', 'private', '--fork', '--kill-child=KILL',
                m['python'], '-I', '-B', str(stage/'source'/SOURCE_FILES[0]),
                '--inside', sha, ','.join(map(str, fds)), json.dumps(parent_ns)]
        receipt['qemu_launch_count'] = 1
        receipt['cleanup']['qemu_reaped'] = False
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, pass_fds=fds, start_new_session=True,
            preexec_fn=lambda parent=os.getpid(): child_death_fence(parent),
            cwd=stage, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'PYTHONDONTWRITEBYTECODE': '1'})
        receipt['owned_process_group'] = proc.pid
        save()
        assert proc.stdin is not None and proc.stdout is not None
        deadline = time.monotonic()+240
        state = {}
        ns = json.loads(get_frame(proc.stdout, deadline, b'WZ_NAMESPACE=', state))
        assert set(ns) == {'private', 'readonly_input_fds', 'all_exact_input_binds_ro'}
        assert set(ns['private']) == set(parent_ns)
        assert all(type(ns['private'][k]) is int and ns['private'][k] > 0 and ns['private'][k] != parent_ns[k] for k in parent_ns)
        assert ns['readonly_input_fds'] is True and ns['all_exact_input_binds_ro'] is True
        receipt['host_namespace'] = ns
        get_frame(proc.stdout, deadline, b'# ', state)
        parser = (stage/'source'/SOURCE_FILES[1]).read_bytes()
        guest = (stage/'source'/SOURCE_FILES[2]).read_bytes()
        payload = parser + b'\nVOCAB=' + repr(m['vocabulary']).encode('ascii') + b'\n' + guest
        assert len(payload) < 65536
        bootstrap = "mount -t proc proc /proc >/dev/null 2>&1; mount -t sysfs sysfs /sys >/dev/null 2>&1; stty raw -echo; exec /usr/bin/python3 -I -B -c 'import sys;print(\"WZ_INPUT_READY\",flush=True);exec(compile(sys.stdin.buffer.read("+str(len(payload))+"),\"<forensic>\",\"exec\"))' 2>/dev/null\n"
        send_private(proc.stdin, bootstrap.encode('ascii'), deadline)
        get_frame(proc.stdout, deadline, b'WZ_INPUT_READY', state)
        send_private(proc.stdin, payload, deadline)
        result = json.loads(get_frame(proc.stdout, deadline, b'WZ_FORENSIC_RESULT=', state))
        module.validate_export(result, m['vocabulary'])
        p = result['projection']
        expected = m['expected_private_log']
        assert result['log_metadata'] == expected['metadata']
        assert p['prefix_sha256'] == expected['sha256'] and p['bytes_read'] == expected['bytes']
        assert p['processed_lines'] == expected['records'] and p['eof'] and not p['bounded_incomplete']
        receipt['guest'] = result
        receipt['diagnosis'] = result['diagnosis']
        receipt['private_channel_bytes'] = state['bytes']
        state.clear()
    except BaseException as exc:
        # Preserve first error only. Never repr/str/traceback of runtime exception.
        receipt['first_error'] = {'error_class': type(exc).__name__ if type(exc).__name__ in (*module.CLASSES, 'EOFError') else 'UnknownError'}
    finally:
        if proc is not None:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=15)
            # --kill-child ensures its exact QEMU dies if namespace parent dies.
            receipt['cleanup']['qemu_reaped'] = proc.poll() is not None
            receipt['cleanup']['owned_group_absent'] = False
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                receipt['cleanup']['owned_group_absent'] = True
            for s in (proc.stdin, proc.stdout):
                if s:
                    s.close()
        for fd in fds:
            os.close(fd)
        receipt['preservation'] = []
        for row in m['inputs']:
            try:
                same = identity(row['path']) == row['stat'] and digest(row['path']) == row['sha256']
            except BaseException:
                same = False
            receipt['preservation'].append({'input_index': len(receipt['preservation']), 'exact_hash_stat_unchanged': same})
        try:
            receipt['source_unchanged'] = inventory(stage/'source') == json.loads((stage/'source-members.json').read_text())
        except BaseException:
            receipt['source_unchanged'] = False
        save()
    print(json.dumps({'review_required': True, 'full_acceptance': False,
                      'diagnosis': receipt['diagnosis'], 'receipt_sha256': digest(run/'receipt.json')}))
    # Unknown is a legitimate diagnostic result, NOT a green acceptance result.
    return 0 if 'guest' in receipt and all(x['exact_hash_stat_unchanged'] for x in receipt['preservation']) and receipt['cleanup']['qemu_reaped'] and receipt['cleanup'].get('owned_group_absent') and receipt['source_unchanged'] else 1


def main():
    assert not sys.flags.optimize
    stage = Path.cwd()
    if sys.argv[1:2] == ['--inside']:
        namespace_launcher(stage, *sys.argv[2:])
        return 1
    assert len(sys.argv) == 3 and sys.argv[1] in ('--preflight', '--execute')
    if sys.argv[1] == '--preflight':
        _, _, report = preflight(stage, sys.argv[2], full=False)
        print(json.dumps(report, sort_keys=True))
        return 0
    return execute(stage, sys.argv[2])


if __name__ == '__main__':
    try:
        code = main()
    except BaseException as error:
        print(json.dumps({'preflight_or_cleanup_refused': True, 'error_class': type(error).__name__ if type(error).__name__ in ('AssertionError', 'FileNotFoundError', 'PermissionError', 'ValueError', 'TimeoutError', 'OSError') else 'UnknownError'}))
        code = 1
    sys.exit(code)
