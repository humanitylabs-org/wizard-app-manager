#!/usr/bin/python3
"""Injected reviewed public source only. Never installed in the failed guest.

Executed as PID 1 in the existing 851 maintenance root, itself block-RO;
failed 610 is a SECOND block-RO device. Only its one allowlisted log is read.
Parser source and VOCAB are prepended by the admitted host runner.
"""
import json
import os
from pathlib import Path
import stat
import subprocess

TARGET = ('root', 'storage-contract-a', 'installer-private.log')


def call(argv):
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
    if p.returncode:
        raise RuntimeError('fixed operation failed')


def mount_row(where):
    rows = [s.split() for s in Path('/proc/self/mountinfo').read_text().splitlines()]
    found = [r for r in rows if r[4] == where]
    if len(found) != 1:
        raise RuntimeError('mount identity')
    r = found[0]
    sep = r.index('-')
    return r[2], set(r[5].split(',')), r[sep+1], set(r[sep+3].split(','))


def open_log():
    fd = os.open('/run/wz-forensic/failed', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in TARGET[:-1]:
            n = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = n
        result = os.open(TARGET[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    return result


def diagnose():
    operation = 'identity'
    mounted = False
    receipt = {'schema': 'installer-forensic-guest-v1', 'review_required': True,
               'full_acceptance': False, 'diagnosis': 'unknown'}
    try:
        assert os.getpid() == 1
        assert os.uname().release == '6.1.0-54-cloud-amd64'
        assert set(p.name for p in Path('/sys/class/net').iterdir()) == {'lo'}
        assert Path('/sys/block/vda/serial').read_text().strip() == 'WZ_MAINT_851_RO'
        assert Path('/sys/block/vdb/serial').read_text().strip() == 'WZ_FAILED_610_RO'
        assert all(Path('/sys/block/'+d+'/ro').read_text().strip() == '1' for d in ('vda', 'vdb'))
        # No OS daemons: only init and kernel tasks may remain after initramfs.
        other = 0
        for p in Path('/proc').iterdir():
            if p.name.isdigit() and p.name != '1':
                try:
                    other += bool((p/'cmdline').read_bytes())
                except FileNotFoundError:
                    pass
        assert other == 0
        root_dev, opts, fs, superopts = mount_row('/')
        assert root_dev == Path('/sys/class/block/vda1/dev').read_text().strip()
        assert 'ro' in opts and fs == 'ext4' and ('noload' in superopts or 'norecovery' in superopts)
        receipt['identity'] = {'pid1': True, 'no_os_daemons': True, 'no_nic': True,
                               'maintenance_root_ro_noload': True, 'both_devices_ro': True,
                               'mnt_namespace_inode': os.stat('/proc/self/ns/mnt').st_ino,
                               'pid_namespace_inode': os.stat('/proc/self/ns/pid').st_ino}
        operation = 'mount-log-filesystem'
        # Only tmpfs and an explicitly RO ext4 mount, never fsck/journal replay.
        call(['/bin/mount', '-t', 'tmpfs', '-o', 'size=2m,nosuid,nodev,noexec', 'tmpfs', '/run'])
        os.mkdir('/run/wz-forensic', 0o700)
        os.mkdir('/run/wz-forensic/failed', 0o700)
        call(['/bin/mount', '-t', 'ext4', '-o', 'ro,noload,nosuid,nodev,noexec', '/dev/vdb1', '/run/wz-forensic/failed'])
        mounted = True
        dev, opts, fs, superopts = mount_row('/run/wz-forensic/failed')
        assert dev == Path('/sys/class/block/vdb1/dev').read_text().strip()
        assert fs == 'ext4' and 'ro' in opts and ('noload' in superopts or 'norecovery' in superopts)
        receipt['identity']['failed_mount_ro_noload'] = True
        operation = 'read-allowlisted-log'
        fd = open_log()
        try:
            before = os.fstat(fd)
            assert stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            assert before.st_dev == os.stat('/run/wz-forensic/failed').st_dev
            data = bytearray()
            limit = min(before.st_size, MAX_BYTES)
            while len(data) < limit:
                part = os.read(fd, min(65536, limit-len(data)))
                if not part:
                    break
                data.extend(part)
            after = os.fstat(fd)
            assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            assert len(data) == limit
            receipt['log_metadata'] = {'inode': before.st_ino, 'size': before.st_size,
                'mode': before.st_mode, 'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns}
            receipt['projection'] = project(bytes(data), VOCAB, eof=before.st_size <= MAX_BYTES)
            del data
            receipt['diagnosis'] = receipt['projection']['diagnosis']
        finally:
            os.close(fd)
    except BaseException as exc:
        row = {'operation': operation, 'error_class': type(exc).__name__ if type(exc).__name__ in CLASSES else 'UnknownError'}
        if type(getattr(exc, 'errno', None)) is int and 0 <= exc.errno <= 4095:
            row['errno'] = exc.errno
        receipt['first_error'] = row
    finally:
        if mounted:
            try:
                p = subprocess.run(['/bin/umount', '/run/wz-forensic/failed'], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                receipt['failed_mount_unmounted'] = p.returncode == 0
            except BaseException:
                receipt['failed_mount_unmounted'] = False
    # Host independently validates this finite schema before any retention.
    print('WZ_FORENSIC_RESULT=' + json.dumps(receipt, sort_keys=True, separators=(',', ':')), flush=True)
    # PID1 stays inert until its single owned QEMU is stopped by the host.
    import time
    while True:
        time.sleep(3600)


diagnose()
