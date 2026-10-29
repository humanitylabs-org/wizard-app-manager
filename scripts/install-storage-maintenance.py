#!/usr/bin/python3 -I
"""Root-only installation of two reviewed maintenance files; NO boot selection.

The source directory must already be a protected root-owned reviewed copy (for
example a root-created /run staging directory). No download, request creation,
agent-home read, service operation, sudo grant or bootloader edit is performed.
"""
import os
from pathlib import Path
import runpy
import stat
import sys


def main():
    if os.getuid() or os.geteuid() or not sys.flags.isolated or len(sys.argv) != 1:
        raise SystemExit('root isolated installation required')
    # Bootstrap checks its own source without importing any neighbour first.
    # It is itself an administrator-reviewed root-controlled program.
    source = Path(__file__).absolute().parent
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', directory)
    try:
        for part in source.parts[1:]:
            st = os.fstat(fd)
            if st.st_uid or st.st_mode & 0o022: raise ValueError('unprotected installation source')
            child = os.open(part, directory, dir_fd=fd); os.close(fd); fd = child
        st = os.fstat(fd)
        if st.st_uid or st.st_mode & 0o022: raise ValueError('unprotected installation source')
        contents = {}
        for name in ('protected-agent-storage.py', 'storage-maintenance-init.py'):
            code = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                st = os.fstat(code)
                if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode & 0o022 or st.st_nlink != 1:
                    raise ValueError('unprotected installation file')
                with os.fdopen(os.dup(code), 'rb') as stream: contents[name] = stream.read(1048577)
                if len(contents[name]) > 1048576: raise ValueError('oversize installation file')
            finally: os.close(code)
    finally: os.close(fd)
    sys.dont_write_bytecode = True
    storage = runpy.run_path(str(source/'protected-agent-storage.py'))
    with storage['Pinned']('/usr/local/lib') as lib:
        storage['mkdir'](lib, 'wizard-storage', 0o755)
    with storage['Pinned']('/usr/local/lib/wizard-storage') as target:
        for source_name, target_name, mode in (
                ('protected-agent-storage.py', 'protected-agent-storage.py', 0o644),
                ('storage-maintenance-init.py', 'maintenance-init', 0o755)):
            # Private new inode then atomic replace, only in protected ancestry.
            import secrets
            name = '.install-' + secrets.token_hex(16)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=target.fd)
            try:
                with os.fdopen(os.dup(fd), 'wb') as stream: stream.write(contents[source_name]); stream.flush()
                os.fchmod(fd, mode); os.fsync(fd)
            finally: os.close(fd)
            target.check()
            os.rename(name, target_name, src_dir_fd=target.fd, dst_dir_fd=target.fd)
            os.fsync(target.fd)
    print('Installed protected maintenance code; no boot or request selected.')


if __name__ == '__main__': main()
