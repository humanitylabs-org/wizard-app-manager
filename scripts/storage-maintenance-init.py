#!/usr/bin/python3 -I
"""Fixed cold-boot init. No request, command or environment controls bootstrap.

Install with install-storage-maintenance.py, then select this file ONCE as init=
with rw root. Never invoked by the agent or ordinary get.sh. Only a missing
kernel procfs mount is prepared; no host namespace bind, sysfs or root remount.
"""
import ctypes
import os
from pathlib import Path
import runpy
import stat
import sys
import time

BOOT = Path('/usr/local/lib/wizard-storage/maintenance-init')
PRODUCT = BOOT.with_name('protected-agent-storage.py')
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def protected(path, executable=False):
    """Bootstrap's fixed code check works before procfs exists."""
    fd = os.open('/', DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            st = os.fstat(fd)
            if st.st_uid or st.st_mode & 0o022: raise ValueError('code ancestry')
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd); fd = child
        st = os.fstat(fd)
        if st.st_uid or st.st_mode & 0o022: raise ValueError('code parent')
        code = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            st = os.fstat(code)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode & 0o022
                    or st.st_nlink != 1 or (executable and not st.st_mode & 0o111)):
                raise ValueError('code endpoint')
        finally: os.close(code)
    finally: os.close(fd)


def bootstrap():
    if os.getpid() != 1 or os.getuid() != 0 or os.geteuid() != 0:
        raise ValueError('not root boot init')
    if len(sys.argv) != 1 or not sys.flags.isolated:
        raise ValueError('unexpected init invocation')
    if (os.uname().sysname != 'Linux' or os.uname().machine not in ('x86_64', 'aarch64')
            or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8):
        raise ValueError('unsupported bootstrap ABI')
    protected(BOOT, True); protected(PRODUCT)
    # Import only the fixed reviewed root-protected program. No payload imports.
    sys.dont_write_bytecode = True
    product = runpy.run_path(str(PRODUCT))
    with product['Pinned']('/proc') as proc:
        kind = product['namespace_fs_type'](proc.fd)
        if kind != 0x9FA0:  # PROC_SUPER_MAGIC; never cover another mount/directory.
            if os.listdir(proc.fd): raise ValueError('nonempty proc mountpoint')
            libc = ctypes.CDLL(None, use_errno=True)
            mount = libc.mount
            mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                              ctypes.c_ulong, ctypes.c_void_p]
            mount.restype = ctypes.c_int
            # Fixed kernel boot prerequisite ONLY, even on a read-only root.
            if mount(b'proc', b'/proc', b'proc', 2 | 4 | 8, None):
                raise ValueError('proc mount unavailable')
    # No rootRW/mutation/record reads until runtime namespace + sole-init proof.
    product['require_fence']()
    if os.statvfs('/').f_flag & os.ST_RDONLY:
        raise ValueError('writable boot root required')
    print('{"entry":"wizard-storage-maintenance-v1","state":"admitted"}', flush=True)
    product['main']()  # actual shipped PID1 request reader; never Transaction from QA


def main():
    boot = os.getpid() == 1
    try:
        bootstrap()
    except BaseException as exc:
        # Fixed output enums only, never exception/request/path/byte echo.
        # Distinguish intended cold negatives from an earlier bootstrap failure.
        codes = {'writable boot root required': 'readonly-root',
                 'maintenance contains another userspace process': 'another-userspace-process',
                 'nested PID namespace': 'nested-pid-namespace'}
        code = codes.get(str(exc), 'bootstrap-failed') if isinstance(exc, ValueError) else 'bootstrap-failed'
        print('{"entry":"wizard-storage-maintenance-v1","state":"refused","code":"'+code+'"}', flush=True)
    if not boot: return 2
    while True: time.sleep(3600)


if __name__ == '__main__': sys.exit(main())
