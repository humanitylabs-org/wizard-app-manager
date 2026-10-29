#!/usr/bin/python3 -I
"""Root-only, recovery-boot Hermes storage transaction (Linux, local filesystems).

This is deliberately NOT an online migration. Mutation requires this process to
be PID 1 in the host namespaces, with no other userspace task. No service manager,
login, cron daemon, container daemon or agent is started by this program. A normal
root shell, stopped-service receipt, lock or namespace container is not a fence.
Payloads are opaque: only cp and a byte comparator read regular-file contents;
no payload configuration, database, plugin or executable is interpreted.

Install this reviewed file on protected root-owned ancestry, boot it explicitly
as init= with rw root, and supply /etc/wizard-agent-storage/request.json. Boot
selection and real-home authorization belong to the administrator, not get.sh.
On success OR failure PID 1 stays in maintenance until the administrator reboots.
The public check command is read-only and does not need maintenance or root.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import sys
import time

ANCHOR = Path('/var/lib/wizard-agent-homes')
CONTROL = Path('/var/lib/wizard-agent-storage')
CREDENTIALS = Path('/var/lib/wizard-os-migration/credentials')
REQUEST = Path('/etc/wizard-agent-storage/request.json')
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LOCAL_FS = {'ext4', 'xfs', 'btrfs'}


class Refusal(ValueError):
    pass


def identity(st):
    return [st.st_dev, st.st_ino]


def clean_path(path):
    value = os.fspath(path)
    if not value.startswith('/') or str(Path(value)) != value or '..' in Path(value).parts:
        raise Refusal('noncanonical path')
    return Path(value)


class Pinned:
    """No-follow directory walk; retain and revalidate EVERY ancestor inode."""
    def __init__(self, path, *, owner=None):
        self.path = clean_path(path)
        self.fds = [os.open('/', DIRECTORY)]
        self.parts = self.path.parts[1:]
        try:
            for part in self.parts:
                self.fds.append(os.open(part, DIRECTORY, dir_fd=self.fds[-1]))
            self.ids = [identity(os.fstat(fd)) for fd in self.fds]
            for fd in self.fds[:-1] if owner is not None else self.fds:
                st = os.fstat(fd)
                if st.st_uid != 0 or st.st_mode & 0o022:
                    raise Refusal('unprotected control ancestry')
            if owner is not None:
                st = os.fstat(self.fds[-1])
                if st.st_uid != owner or st.st_mode & 0o022:
                    raise Refusal('source parent ownership or mode')
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        return self.fds[-1]

    def check(self):
        fd = os.open('/', DIRECTORY)
        try:
            if identity(os.fstat(fd)) != self.ids[0]: raise Refusal('root identity changed')
            for i, part in enumerate(self.parts, 1):
                new = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd); fd = new
                if identity(os.fstat(fd)) != self.ids[i]:
                    raise Refusal('parent identity changed')
        finally:
            os.close(fd)

    def close(self):
        for fd in getattr(self, 'fds', []): os.close(fd)
        self.fds = []

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def protected_json(path, *, private=False, maxbytes=1048576):
    path = clean_path(path)
    with Pinned(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=parent.fd)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1
                    or st.st_mode & (0o077 if private else 0o022) or st.st_size > maxbytes):
                raise Refusal('unsafe control record')
            with os.fdopen(os.dup(fd)) as stream: value = json.load(stream)
            parent.check()
            if identity(os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)) != identity(st):
                raise Refusal('control record replaced')
            return value
        finally:
            os.close(fd)


def atomic(parent, name, value, mode=0o600):
    parent.check()
    tmp = '.record-' + secrets.token_hex(16)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 mode, dir_fd=parent.fd)
    try:
        with os.fdopen(os.dup(fd), 'w') as stream:
            json.dump(value, stream, sort_keys=True); stream.write('\n'); stream.flush()
        os.fchmod(fd, mode); os.fsync(fd)
    finally:
        os.close(fd)
    parent.check()
    os.rename(tmp, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
    os.fsync(parent.fd)


def mkdir(parent, name, mode):
    parent.check()
    created = False
    try:
        os.mkdir(name, mode, dir_fd=parent.fd); created = True
    except FileExistsError: pass
    fd = os.open(name, DIRECTORY, dir_fd=parent.fd)
    try:
        if created: os.fchmod(fd, mode)
        st = os.fstat(fd)
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != mode:
            raise Refusal('unsafe existing storage directory')
    finally: os.close(fd)
    os.fsync(parent.fd)


def rename_new(old_parent, old, new_parent, new):
    old_parent.check(); new_parent.check()
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.renameat2
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(old_parent.fd, os.fsencode(old), new_parent.fd, os.fsencode(new), 1):
        e = ctypes.get_errno(); raise OSError(e, os.strerror(e))
    os.fsync(old_parent.fd); os.fsync(new_parent.fd)
    old_parent.check(); new_parent.check()


def require_fence():
    """Prevention = sole host init; census alone is NEVER an online admission."""
    if os.getuid() != 0 or os.geteuid() != 0 or os.getpid() != 1:
        raise Refusal('maintenance-needed: boot the protected storage entrypoint as sole host init')
    if Path('/proc/self/uid_map').read_text().split() != ['0', '0', '4294967295']:
        raise Refusal('remapped user namespace')
    fields = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    caps = int(fields['CapEff'].strip(), 16)
    if caps & ((1 << 21) | (1 << 22)) != ((1 << 21) | (1 << 22)):
        raise Refusal('host init capabilities unavailable')
    # In the initial user namespace CAP_SYS_ADMIN allows opening a nested PID
    # namespace's parent. Initial PID namespace has no parent and returns EPERM.
    # A rootless user namespace cannot pass the complete uid mapping above.
    fd = os.open('/proc/self/ns/pid', os.O_RDONLY | os.O_CLOEXEC)
    try:
        try: parent = fcntl.ioctl(fd, 0xb702)  # NS_GET_PARENT
        except OSError as exc:
            if exc.errno != errno.EPERM: raise Refusal('unknown PID namespace') from None
        else:
            os.close(parent); raise Refusal('nested PID namespace')
    finally: os.close(fd)
    boot_inits = [token.removeprefix('init=') for token in Path('/proc/cmdline').read_text().split()
                  if token.startswith('init=')]
    if len(boot_inits) != 1 or boot_inits[0] in ('/sbin/init', '/usr/lib/systemd/systemd'):
        raise Refusal('explicit protected recovery boot init required')
    boot_init = clean_path(boot_inits[0])
    with Pinned(boot_init.parent) as parent:
        st = os.stat(boot_init.name, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode & 0o022 or not st.st_mode & 0o111:
            raise Refusal('unprotected recovery boot init')
    userspace = []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or path.name == '1': continue
        try:
            fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            if not int(fields[6]) & 0x00200000: userspace.append(int(path.name))  # PF_KTHREAD
        except (FileNotFoundError, ProcessLookupError):
            raise Refusal('process census changed') from None
    if userspace: raise Refusal('maintenance contains another userspace process')
    if len(list(Path('/proc/self/task').iterdir())) != 1:
        raise Refusal('maintenance must be single-threaded')
    # A trust check, not source authentication. Administrator installs the exact
    # reviewed bytes. Root/admin/kernel remain trusted; root can forge any state.
    code = Path(__file__).absolute()
    with Pinned(code.parent) as parent:
        st = os.stat(code.name, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise Refusal('maintenance code is not protected')
    return {'mechanism': 'sole-host-init-v1', 'userspace_tasks': 1}


def rollback_guard():
    # Must run BEFORE creating a directory/lock/journal, or changing any path.
    # Missing parent means rotation never started. Unsafe ancestry never means
    # "absent"; Pinned/protected_json refuse symlinks and foreign ownership.
    try: doc = protected_json(CREDENTIALS/'journal.json', private=True)
    except FileNotFoundError: return
    if doc.get('phase') not in ('prepared', 'quiesced', 'aborted'):
        raise Refusal('db_intent-or-later: forward-only storage mapping; rollback forbidden')


def mount_check(path):
    """Refuse nested mounts (including same-device bind mounts) and remote FS."""
    path = str(clean_path(path))
    selected = None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        raw = left.split()[4]
        mount = raw.replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')
        if mount.startswith(path + '/'): raise Refusal('nested payload mount')
        if path == mount or path.startswith(mount.rstrip('/') + '/'):
            if selected is None or len(mount) > len(selected[0]): selected = (mount, right.split()[0])
    if selected is None or selected[1] not in LOCAL_FS:
        raise Refusal('unsupported payload filesystem')


def fingerprint(path, *, sync=False):
    """Opaque bytes + numeric metadata; retain no names/content in reports.

    ctime/birthtime/inode numbers cannot survive a cross-filesystem copy and are
    not compared. UID/GID/mode/atime/mtime/ACL/xattrs and hardlinks are exact.
    External hardlinks and special files are unsupported, never flattened.
    """
    path = clean_path(path); mount_check(path)
    rows = []; links = {}; counts = {}
    rootdev = path.lstat().st_dev

    def walk(current, rel):
        st = current.lstat()
        if st.st_dev != rootdev: raise Refusal('payload crosses filesystem')
        mode = stat.S_IFMT(st.st_mode)
        if mode not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK):
            raise Refusal('special payload file')
        attrs = [(name, os.getxattr(current, name, follow_symlinks=False).hex())
                 for name in sorted(os.listxattr(current, follow_symlinks=False))]
        row = [rel, mode, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_mtime_ns, st.st_atime_ns, attrs]
        if mode == stat.S_IFDIR:
            fd = os.open(current, DIRECTORY | os.O_NOATIME)
            try:
                names = sorted(os.listdir(fd))
                for name in names: walk(current/name, rel + '/' + name)
                if sync: os.fsync(fd)
            finally: os.close(fd)
        elif mode == stat.S_IFLNK:
            key = (st.st_dev, st.st_ino)
            links.setdefault(key, rel)
            row.extend([os.readlink(current), links[key], st.st_nlink])
            # Linux readlink updates atime; metadata inspection must undo that.
            if current.lstat().st_atime_ns != st.st_atime_ns:
                os.utime(current, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        else:
            key = (st.st_dev, st.st_ino)
            links.setdefault(key, rel); counts[key] = counts.get(key, 0) + 1
            row.extend([st.st_size, links[key]])
            fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_CLOEXEC)
            try:
                if identity(os.fstat(fd)) != identity(st): raise Refusal('payload inode changed')
                h = hashlib.sha256()
                while data := os.read(fd, 1048576): h.update(data)
                row.append(h.hexdigest())
                if sync: os.fsync(fd)
            finally: os.close(fd)
            row.append(st.st_nlink)
        rows.append(row)
    walk(path, '')
    link_counts = {}
    for row in rows:
        if row[1] != stat.S_IFDIR:
            link_counts[row[9]] = link_counts.get(row[9], 0) + 1
    for row in rows:
        if row[1] != stat.S_IFDIR:
            # Count by the canonical relative name, not target inode identity.
            expected = link_counts[row[9]]
            if expected != row[-1]: raise Refusal('hardlink escapes selected tree')
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def private_leaf(path):
    """First slice: the leaf itself, not its former home, must deny other UIDs.

    Leaf ACLs (even masked ones) are deliberately unsupported; inner ACLs remain
    opaque preserved metadata. No chmod/chown is a compatibility repair.
    """
    owner = path.parent.stat().st_uid if str(path).startswith('/home/') else None
    with Pinned(path.parent, owner=owner) as parent:
        fd = os.open(path.name, DIRECTORY | os.O_NOATIME, dir_fd=parent.fd)
        try:
            st = os.fstat(fd)
            if st.st_mode & 0o077 or any(a in os.listxattr(fd) for a in
                    ('system.posix_acl_access', 'system.posix_acl_default')):
                raise Refusal('unsupported selected-leaf privacy layout')
        finally: os.close(fd)


def copy_parent(path):
    # A default ACL overrides umask during mkdir/cp. Refuse it before creating
    # any target, even if cp would eventually replace the inherited metadata.
    fd = os.open(path, DIRECTORY | os.O_NOATIME)
    try:
        if 'system.posix_acl_default' in os.listxattr(fd):
            raise Refusal('unsupported copy-parent default ACL')
    finally: os.close(fd)


def graph(path):
    """Read-only, descriptor-relative graph proof. Never readlink/utime here.

    cp changes atime, not ctime/mtime. ctime therefore detects intervening edits,
    including edit-and-restore of mtime. Collect AFTER any fingerprint's own
    symlink atime repair. Aliases are validated together before restoring once.
    """
    path = clean_path(path); mount_check(path)
    rows = []; counts = {}; links = {}
    with Pinned(path.parent, owner=path.parent.stat().st_uid if str(path).startswith('/home/') else None) as parent:
        rootdev = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False).st_dev
        def walk(pfd, name, relative):
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            kind = stat.S_IFMT(st.st_mode)
            if st.st_dev != rootdev or kind not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK):
                raise Refusal('unsupported recovery graph object')
            key = (st.st_dev, st.st_ino)
            proof = [st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                     st.st_nlink, st.st_size if kind != stat.S_IFDIR else 0,
                     st.st_mtime_ns, st.st_ctime_ns]
            row = {'relative': relative, 'proof': proof, 'atime': st.st_atime_ns}
            if kind != stat.S_IFDIR:
                counts[key] = counts.get(key, 0) + 1; links[key] = st.st_nlink
            if kind in (stat.S_IFDIR, stat.S_IFREG):
                fd = os.open(name, (DIRECTORY if kind == stat.S_IFDIR else
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC) | os.O_NOATIME, dir_fd=pfd)
                try:
                    if identity(os.fstat(fd)) != identity(st): raise Refusal('recovery graph inode changed')
                    row['attrs'] = [[a, os.getxattr(fd, a).hex()] for a in sorted(os.listxattr(fd))]
                    if kind == stat.S_IFDIR:
                        for child in sorted(os.listdir(fd)): walk(fd, child, relative + [child])
                    else:
                        h = hashlib.sha256()
                        while data := os.read(fd, 1048576): h.update(data)
                        row['bytes'] = h.hexdigest()
                finally: os.close(fd)
            rows.append(row)
        walk(parent.fd, path.name, [])
        parent.check()
    if counts != links: raise Refusal('hardlink escapes selected tree')
    return {'schema': 2, 'rows': rows}


def validate_graph_record(record):
    if not isinstance(record, dict) or set(record) != {'schema', 'rows'} or record['schema'] != 2:
        raise Refusal('ambiguous interrupted copy record')
    rows = record['rows']
    if not isinstance(rows, list) or not rows: raise Refusal('invalid recovery graph')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not {'relative', 'proof', 'atime'} <= set(row):
            raise Refusal('invalid recovery graph')
        rel = row['relative']; proof = row['proof']
        if (not isinstance(rel, list) or any(not isinstance(p, str) or p in ('', '.', '..')
                or '/' in p or '\x00' in p for p in rel) or tuple(rel) in seen
                or not isinstance(proof, list) or len(proof) != 9
                or any(type(v) is not int for v in proof) or type(row['atime']) is not int):
            raise Refusal('invalid recovery graph')
        seen.add(tuple(rel))
    if () not in seen: raise Refusal('invalid recovery graph')
    for rel in seen:
        if rel and rel[:-1] not in seen: raise Refusal('invalid recovery graph')
    return rows


def graph_without_atime(record):
    validate_graph_record(record)
    return [{k: v for k, v in row.items() if k != 'atime'} for row in record['rows']]


def restoration_plan(path, record, *, times=None):
    """Validate the ENTIRE graph read-only before the first privileged write."""
    rows = validate_graph_record(record)
    actual = graph(path)
    if graph_without_atime(actual) != graph_without_atime(record): raise Refusal('interrupted copy source graph changed')
    desired = {tuple(r['relative']): r for r in validate_graph_record(times or record)}
    if set(desired) != {tuple(r['relative']) for r in rows}: raise Refusal('copy graph membership mismatch')
    # Complete target layout/mtime/type/alias agreement before writing either tree.
    aliases = {}; source_aliases = {}
    for row in rows:
        src = desired[tuple(row['relative'])]; a = row['proof']; b = src['proof']
        if a[2:8] != b[2:8]: raise Refusal('copy graph metadata mismatch')
        aliases.setdefault(tuple(a[:2]), []).append(row['relative'])
        source_aliases.setdefault(tuple(b[:2]), []).append(row['relative'])
    if sorted(aliases.values()) != sorted(source_aliases.values()): raise Refusal('copy hardlink mismatch')
    return rows, desired


def restore_times(path, record, *, times=None):
    """Each graph has its OWN identities. Only validated atime is restored.

    mtime is never repaired after drift. Every intermediate fd uses NOFOLLOW.
    A crash during restoration is explicitly ambiguous and refuses on resume.
    """
    rows, desired = restoration_plan(path, record, times=times)
    done = set(); proofs = {tuple(r['relative']): r['proof'] for r in rows}
    with Pinned(path.parent, owner=path.parent.stat().st_uid if str(path).startswith('/home/') else None) as parent:
        for row in rows:
            rel = row['relative']; key = tuple(row['proof'][:2])
            if key in done: continue
            done.add(key); fds = []
            try:
                pfd = parent.fd
                components = [path.name] + rel
                for index, component in enumerate(components[:-1]):
                    pfd = os.open(component, DIRECTORY | os.O_NOATIME, dir_fd=pfd); fds.append(pfd)
                    if identity(os.fstat(pfd)) != proofs[tuple(rel[:index])][:2]:
                        raise Refusal('recovery intermediate identity changed')
                st = os.stat(components[-1], dir_fd=pfd, follow_symlinks=False)
                if identity(st) != list(key): raise Refusal('recovery graph inode changed')
                atime = desired[tuple(rel)]['atime']
                if st.st_atime_ns != atime:
                    os.utime(components[-1], ns=(atime, st.st_mtime_ns), dir_fd=pfd, follow_symlinks=False)
            finally:
                for fd in reversed(fds): os.close(fd)
        parent.check()


def copy_opaque(source, target):
    require_fence()
    if not stat.S_ISDIR(source.lstat().st_mode): raise Refusal('opaque source is not a real directory')
    if os.path.lexists(target): raise Refusal('opaque destination already exists')
    # These are reviewed root-owned OS executables, not payload-derived tools.
    for executable in (Path('/usr/bin/cp'), Path('/usr/bin/python3').resolve()):
        with Pinned(executable.parent) as parent:
            st = os.stat(executable.name, dir_fd=parent.fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode & 0o022:
                raise Refusal('unprotected transfer executable')
    private_leaf(source); copy_parent(target.parent)
    result = subprocess.run(['/usr/bin/cp', '-a', '--preserve=all', '--reflink=auto', '--',
                             str(source), str(target)], env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, umask=0o077)
    require_fence()  # cp is reaped; no child or inherited writer remains.
    if result.returncode: raise Refusal('opaque transfer failed (partial copy retained)')


def selected(agent, logical):
    account = pwd.getpwnam(agent)
    logical = clean_path(logical)
    if account.pw_uid == 0 or account.pw_name == 'wizard-system': raise Refusal('nonroot agent required')
    # Finite first slice: direct child of an ordinary protected /home account.
    # Other layouts need explicit engineering, not resolve()/home-wide chown.
    home = clean_path(account.pw_dir)
    if home.parent != Path('/home') or logical.parent != home:
        raise Refusal('first storage slice requires a direct account-home child')
    return account, logical


def require_mapping(agent, logical):
    account, logical = selected(agent, logical)
    doc = protected_json(ANCHOR/'.mappings'/f'{account.pw_uid}.json')
    expected = {'schema': 1, 'state': 'ready', 'uid': account.pw_uid, 'gid': account.pw_gid,
                'logical': str(logical), 'physical': str(ANCHOR/str(account.pw_uid))}
    if any(doc.get(k) != v for k, v in expected.items()): raise Refusal('storage mapping mismatch')
    with Pinned(ANCHOR) as parent:
        st = os.stat(str(account.pw_uid), dir_fd=parent.fd, follow_symlinks=False)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != account.pw_uid or st.st_gid != account.pw_gid
                or st.st_mode & 0o077 or identity(st) != doc.get('identity')):
            raise Refusal('canonical storage identity changed')
        if any(a in os.listxattr(ANCHOR/str(account.pw_uid), follow_symlinks=False)
               for a in ('system.posix_acl_access', 'system.posix_acl_default')):
            raise Refusal('unsupported canonical-leaf ACL')
    # This is a current consistency observation, NOT a durable alias race pin.
    # Never resolve/adopt its target. The physical consumer stays canonical.
    with Pinned(logical.parent, owner=account.pw_uid) as home:
        st = os.stat(logical.name, dir_fd=home.fd, follow_symlinks=False)
        if not stat.S_ISLNK(st.st_mode) or os.readlink(logical.name, dir_fd=home.fd) != expected['physical']:
            raise Refusal('logical storage alias disagreement')
        home.check()
    return doc


def check_pairing(path):
    # Existing finite manager metadata, not anything in the Hermes payload.
    # This reader grants no authority; the root-owned public mapping is final.
    doc = json.loads(Path(path).read_bytes())
    if type(doc.get('agent_uid')) is not int or not isinstance(doc.get('hermes_home'), str):
        raise Refusal('invalid pairing metadata')
    return require_mapping(pwd.getpwuid(doc['agent_uid']).pw_name, doc['hermes_home'])


class Transaction:
    def __init__(self, agent, logical):
        self.account, self.logical = selected(agent, logical)
        self.uid = self.account.pw_uid
        self.target = ANCHOR/str(self.uid)
        self.control = CONTROL/str(self.uid)
        self.doc: dict = {}

    def save(self, phase):
        require_fence(); self.home.check(); self.dest.check(); self.state.check()
        self.doc['phase'] = phase
        atomic(self.state, 'journal.json', self.doc)

    def source_check(self, name=None):
        self.home.check()
        st = os.stat(name or self.logical.name, dir_fd=self.home.fd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or identity(st) != self.doc['source_identity']:
            raise Refusal('selected source identity changed')
        private_leaf(self.logical.parent/(name or self.logical.name))
        return st

    def matches(self, path, expected):
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or identity(st) != expected:
            raise Refusal('transaction endpoint identity changed')
        private_leaf(path)
        if fingerprint(path) != self.doc['fingerprint']: raise Refusal('opaque transfer metadata/bytes changed')

    def run(self, action='relocate', *, checkpoint=None):
        # No caller-supplied offline boolean or receipt can bypass this check.
        require_fence()
        if action not in ('relocate', 'rollback'): raise Refusal('unknown storage operation')
        if action == 'rollback': rollback_guard()
        # Privacy admission precedes ALL directory/lock/journal/payload writes.
        # Resume inventories finite known endpoints without following an alias.
        try: existing = protected_json(self.control/'journal.json', private=True)
        except FileNotFoundError: existing = None
        if existing is None:
            with Pinned(self.logical.parent, owner=self.uid):
                st = self.logical.lstat()
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != self.uid or st.st_gid != self.account.pw_gid:
                    raise Refusal('selected source must be a real agent-owned directory')
                private_leaf(self.logical)
        else:
            paths = [self.logical, self.target]
            for key in ('snapshot', 'stage', 'restore', 'retained', 'post_mapping'):
                if key in existing:
                    name = existing[key]
                    if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\x00' in name:
                        raise Refusal('invalid transaction endpoint name')
                    parent = self.control if key == 'snapshot' else ANCHOR if key in ('stage', 'post_mapping') else self.logical.parent
                    paths.append(parent/name)
            for path in paths:
                try: st = path.lstat()
                except FileNotFoundError: continue
                if stat.S_ISDIR(st.st_mode): private_leaf(path)
                elif path != self.logical or not stat.S_ISLNK(st.st_mode):
                    raise Refusal('transaction endpoint type changed')
        for path in (Path('/var/lib'), ANCHOR, CONTROL, self.control, self.logical.parent):
            if path.exists(): copy_parent(path)
        with Pinned('/var/lib') as lib:
            mkdir(lib, ANCHOR.name, 0o755); mkdir(lib, CONTROL.name, 0o700)
        with Pinned(CONTROL) as control:
            mkdir(control, str(self.uid), 0o700)
        with Pinned(ANCHOR) as anchor:
            mkdir(anchor, '.mappings', 0o755)
        with Pinned(self.logical.parent, owner=self.uid) as self.home, Pinned(ANCHOR) as self.dest, Pinned(self.control) as self.state:
            fd = os.open('lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.state.fd)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_nlink != 1:
                    raise Refusal('unsafe transaction lock')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try: self.doc = protected_json(self.control/'journal.json', private=True)
                except FileNotFoundError:
                    if action == 'rollback': raise Refusal('no storage transaction to roll back')
                    st = os.stat(self.logical.name, dir_fd=self.home.fd, follow_symlinks=False)
                    if not stat.S_ISDIR(st.st_mode) or st.st_uid != self.uid or st.st_gid != self.account.pw_gid:
                        raise Refusal('selected source must be a real agent-owned directory')
                    if os.path.lexists(self.target): raise Refusal('destination already exists')
                    self.doc = {'schema': 1, 'uid': self.uid, 'gid': self.account.pw_gid,
                                'logical': str(self.logical), 'physical': str(self.target),
                                'source_identity': identity(st), 'home_identity': self.home.ids,
                                'target_parent_identity': self.dest.ids,
                                'method': 'rename' if st.st_dev == os.fstat(self.dest.fd).st_dev else 'copy',
                                'fingerprint': fingerprint(self.logical),
                                'retained': self.logical.name + '.wizard-retained-' + secrets.token_hex(16)}
                    self.save('prepared')
                if (self.doc['schema'], self.doc['uid'], self.doc['gid'], self.doc['logical'], self.doc['physical']) != (1, self.uid, self.account.pw_gid, str(self.logical), str(self.target)):
                    raise Refusal('transaction belongs to another source')
                if self.home.ids != self.doc['home_identity'] or self.dest.ids != self.doc['target_parent_identity']:
                    raise Refusal('transaction parent identity changed')
                if action == 'relocate' and (self.doc.get('direction') == 'rollback' or
                        self.doc['phase'].startswith('rollback') or self.doc['phase'] == 'rolled_back'):
                    raise Refusal('rollback transaction cannot resume forward')
                if action == 'relocate':
                    try: mapping = protected_json(ANCHOR/'.mappings'/f'{self.uid}.json')
                    except FileNotFoundError: mapping = None
                    if mapping is not None and mapping.get('state') != 'ready':
                        raise Refusal('rollback mapping unavailable; forward resume forbidden')
                if action == 'rollback': self.begin_rollback(checkpoint)
                if 'pending_copy' in self.doc:
                    pending = self.doc['pending_copy']
                    original = clean_path(pending['source'])
                    if pending.get('schema') != 2 or pending.get('state') != 'copying':
                        raise Refusal('ambiguous interrupted copy record')
                    if (not isinstance(pending.get('times'), str) or
                            not re.fullmatch(r'times-[0-9a-f]{32}\.json', pending['times'])):
                        raise Refusal('invalid recovery record name')
                    rows = protected_json(self.control/pending['times'], private=True, maxbytes=67108864)
                    # An interrupted restoration is ambiguous. Persist that fact
                    # before writing; never authorize it by a half-updated ctime.
                    validate_graph_record(rows)
                    if graph_without_atime(graph(original)) != graph_without_atime(rows):
                        raise Refusal('interrupted copy source graph changed')
                    pending['state'] = 'restoring'; self.save(self.doc['phase'])
                    restore_times(original, rows)
                    fingerprint(original, sync=True)
                    # The unfinished destination is retained and never trusted
                    # as a snapshot/publication endpoint. No payload is deleted.
                    self.doc.pop('pending_copy'); self.save(self.doc['phase'])
                if action == 'rollback': return self.rollback(checkpoint)
                if self.doc['phase'].startswith('rollback') or self.doc['phase'] == 'rolled_back':
                    raise Refusal('rollback transaction cannot resume forward')
                return self.forward(checkpoint)
            finally: os.close(fd)

    def mark(self, phase, checkpoint):
        self.save(phase)
        if checkpoint: checkpoint(phase)  # trusted in-process QA only; never request/code input

    def transfer(self, source, target):
        # Persist source access timestamps before cp can update them, including
        # during a crash. This is our metadata, never a parsed payload manifest.
        self.home.check(); self.dest.check(); self.state.check()
        if not stat.S_ISDIR(source.lstat().st_mode): raise Refusal('opaque source is not a real directory')
        if os.path.lexists(target): raise Refusal('opaque destination already exists')
        private_leaf(source); copy_parent(target.parent)
        rows = graph(source)
        if len(json.dumps(rows).encode()) > 67108864:
            raise Refusal('timestamp manifest exceeds finite storage limit')
        name = 'times-' + secrets.token_hex(16) + '.json'
        atomic(self.state, name, rows)
        self.doc['pending_copy'] = {'schema': 2, 'state': 'copying', 'source': str(source), 'target': str(target), 'times': name}
        self.save(self.doc['phase'])
        copy_opaque(source, target)
        private_leaf(source); private_leaf(target)
        target_rows = graph(target)
        # Validate source before marking restoration and before any timestamp
        # writes. cp's own access has no authority to hide a legitimate edit.
        if graph_without_atime(graph(source)) != graph_without_atime(rows):
            raise Refusal('interrupted copy source graph changed')
        if {tuple(r['proof'][:2]) for r in rows['rows']} & {tuple(r['proof'][:2]) for r in target_rows['rows']}:
            raise Refusal('copy source and target share inode identities')
        restoration_plan(source, rows); restoration_plan(target, target_rows, times=rows)
        self.doc['pending_copy']['state'] = 'restoring'; self.save(self.doc['phase'])
        restore_times(source, rows); restore_times(target, target_rows, times=rows)
        fingerprint(source, sync=True); fingerprint(target, sync=True)
        self.doc.pop('pending_copy'); self.save(self.doc['phase'])

    def forward(self, checkpoint):
        phase = self.doc['phase']
        if phase in ('prepared', 'snapshot_intent'):
            self.source_check()
            if fingerprint(self.logical) != self.doc['fingerprint']: raise Refusal('source changed since intent')
            name = 'snapshot-' + secrets.token_hex(16)
            self.doc['snapshot'] = name; self.mark('snapshot_intent', checkpoint)
            self.transfer(self.logical, self.control/name)
            self.source_check()
            if fingerprint(self.logical) != self.doc['fingerprint']: raise Refusal('source changed during snapshot')
            if fingerprint(self.control/name, sync=True) != self.doc['fingerprint']: raise Refusal('snapshot mismatch')
            os.fsync(self.state.fd)
            self.doc['snapshot_identity'] = identity((self.control/name).lstat())
            self.mark('snapshot_ready', checkpoint); phase = self.doc['phase']
        if phase in ('snapshot_ready', 'transfer_intent'):
            self.matches(self.control/self.doc['snapshot'], self.doc['snapshot_identity'])
            if self.doc['method'] == 'rename':
                self.source_check()
                self.doc['target_identity'] = self.doc['source_identity']
            else:
                name = '.staging-' + secrets.token_hex(16)
                self.doc['stage'] = name; self.mark('transfer_intent', checkpoint)
                self.transfer(self.control/self.doc['snapshot'], ANCHOR/name)
                if fingerprint(ANCHOR/name, sync=True) != self.doc['fingerprint']: raise Refusal('transfer mismatch')
                os.fsync(self.dest.fd)
                self.doc['target_identity'] = identity((ANCHOR/name).lstat())
            self.mark('transfer_ready', checkpoint); phase = self.doc['phase']
        if phase in ('transfer_ready', 'publish_intent'):
            self.mark('publish_intent', checkpoint)
            if os.path.lexists(self.target):
                if self.doc['method'] == 'rename':
                    if os.path.lexists(self.logical): raise Refusal('ambiguous rename publication')
                elif os.path.lexists(ANCHOR/self.doc['stage']): raise Refusal('ambiguous publication')
                self.matches(self.target, self.doc['target_identity'])
            else:
                self.source_check()
                if fingerprint(self.logical) != self.doc['fingerprint']: raise Refusal('source changed before publish')
                if self.doc['method'] == 'rename':
                    rename_new(self.home, self.logical.name, self.dest, str(self.uid))
                else:
                    self.matches(ANCHOR/self.doc['stage'], self.doc['target_identity'])
                    rename_new(self.dest, self.doc['stage'], self.dest, str(self.uid))
            self.mark('published', checkpoint); phase = self.doc['phase']
        if phase in ('published', 'link_intent'):
            self.matches(self.target, self.doc['target_identity'])
            self.mark('link_intent', checkpoint)
            if self.doc['method'] == 'rename':
                if os.path.lexists(self.logical): raise Refusal('renamed logical source reappeared')
            else:
                retained = self.logical.parent/self.doc['retained']
                if os.path.lexists(retained):
                    self.source_check(self.doc['retained'])
                    if os.path.lexists(self.logical): raise Refusal('ambiguous source retention')
                else:
                    self.source_check()
                    rename_new(self.home, self.logical.name, self.home, self.doc['retained'])
            self.mark('source_retained', checkpoint); phase = self.doc['phase']
        if phase in ('source_retained', 'alias_intent'):
            if self.doc['method'] != 'rename': self.source_check(self.doc['retained'])
            self.matches(self.target, self.doc['target_identity'])
            self.mark('alias_intent', checkpoint)
            if os.path.lexists(self.logical):
                if not self.logical.is_symlink() or os.readlink(self.logical) != str(self.target):
                    raise Refusal('logical path occupied')
            else:
                self.home.check()
                os.symlink(str(self.target), self.logical.name, dir_fd=self.home.fd)
                os.fsync(self.home.fd)
            self.mark('alias_ready', checkpoint); phase = self.doc['phase']
        if phase in ('alias_ready', 'metadata_intent'):
            self.matches(self.target, self.doc['target_identity'])
            if not self.logical.is_symlink() or os.readlink(self.logical) != str(self.target):
                raise Refusal('logical alias changed')
            self.mark('metadata_intent', checkpoint)
            mapping = {k: self.doc[k] for k in ('schema', 'uid', 'gid', 'logical', 'physical')}
            mapping['identity'] = self.doc['target_identity']; mapping['state'] = 'ready'
            with Pinned(ANCHOR/'.mappings') as maps: atomic(maps, f'{self.uid}.json', mapping, 0o644)
            self.mark('committed', checkpoint); phase = self.doc['phase']
        if phase != 'committed': raise Refusal('unknown transaction phase')
        if not self.logical.is_symlink() or os.readlink(self.logical) != str(self.target):
            raise Refusal('logical compatibility link drift')
        require_mapping(self.account.pw_name, self.logical)
        return {'phase': phase, 'physical': str(self.target), 'logical': str(self.logical)}

    def begin_rollback(self, checkpoint):
        rollback_guard()
        if self.doc['phase'] == 'rolled_back': return
        # The public record changes FIRST. A cut before private direction save
        # still refuses forward resume. Alias checks are not durable race pins.
        if self.doc.get('direction') != 'rollback':
            mapping = {k: self.doc[k] for k in ('schema', 'uid', 'gid', 'logical', 'physical')}
            mapping['state'] = 'rollback-unavailable'
            with Pinned(ANCHOR/'.mappings') as maps: atomic(maps, f'{self.uid}.json', mapping, 0o644)
            if checkpoint: checkpoint('rollback_unavailable_public')
            self.doc['direction'] = 'rollback'; self.doc['rollback_origin'] = self.doc['phase']
            self.mark('rollback_unavailable_intent', checkpoint)

    def withdraw_mapping(self, checkpoint):
        self.mark('rollback_withdraw_intent', checkpoint)
        with Pinned(ANCHOR/'.mappings') as maps:
            try: os.unlink(f'{self.uid}.json', dir_fd=maps.fd)
            except FileNotFoundError: pass
            os.fsync(maps.fd)
        if checkpoint: checkpoint('rollback_after_withdrawal')
        self.mark('rolled_back', checkpoint)
        return {'phase': 'rolled_back'}

    def rollback(self, checkpoint):
        rollback_guard()  # also immediately before the first rollback mutation
        phase = self.doc['phase']
        if phase == 'rolled_back': return {'phase': phase}
        if phase == 'rollback_withdraw_intent': return self.withdraw_mapping(checkpoint)
        origin = self.doc.get('rollback_origin', phase)
        # Before link publication the original is still selected. Nothing needs
        # deletion; preserve all copies and simply withdraw any public mapping.
        if phase == 'rollback_unavailable_intent' and (origin in ('prepared', 'snapshot_intent', 'snapshot_ready', 'transfer_intent', 'transfer_ready') or (
                origin in ('publish_intent', 'published', 'link_intent') and self.logical.is_dir() and not self.logical.is_symlink())):
            self.source_check()
            return self.withdraw_mapping(checkpoint)
        if phase == 'rollback_unavailable_intent':
            self.matches(self.control/self.doc['snapshot'], self.doc['snapshot_identity'])
            self.doc['restore'] = '.wizard-restore-' + secrets.token_hex(16)
            self.doc['post_mapping'] = '.retained-' + secrets.token_hex(16)
            self.mark('rollback_copy_intent', checkpoint); phase = self.doc['phase']
        if phase == 'rollback_copy_intent':
            # Interrupted partial copies remain private to maintenance until
            # reboot; use a new name rather than deleting unapproved data.
            self.doc['restore'] = '.wizard-restore-' + secrets.token_hex(16)
            self.mark('rollback_copy_intent', checkpoint)
            self.transfer(self.control/self.doc['snapshot'], self.logical.parent/self.doc['restore'])
            if fingerprint(self.logical.parent/self.doc['restore'], sync=True) != self.doc['fingerprint']:
                raise Refusal('rollback copy mismatch')
            os.fsync(self.home.fd)
            self.doc['restore_identity'] = identity((self.logical.parent/self.doc['restore']).lstat())
            self.mark('rollback_alias_intent', checkpoint); phase = self.doc['phase']
        if phase == 'rollback_alias_intent':
            self.home.check()
            if os.path.lexists(self.logical):
                if self.logical.is_symlink() and os.readlink(self.logical) == str(self.target):
                    os.unlink(self.logical.name, dir_fd=self.home.fd); os.fsync(self.home.fd)
                else:
                    # Early crossfs rollback may still have the selected source.
                    self.source_check()
                    rename_new(self.home, self.logical.name, self.home, self.doc['retained'])
            self.mark('rollback_restore_intent', checkpoint); phase = self.doc['phase']
        if phase == 'rollback_restore_intent':
            restore = self.logical.parent/self.doc['restore']
            if os.path.lexists(self.logical):
                if os.path.lexists(restore): raise Refusal('ambiguous rollback restoration')
                self.matches(self.logical, self.doc['restore_identity'])
            else:
                self.matches(restore, self.doc['restore_identity'])
                rename_new(self.home, self.doc['restore'], self.home, self.logical.name)
            self.mark('rollback_metadata_intent', checkpoint); phase = self.doc['phase']
        if phase == 'rollback_metadata_intent':
            # Retain the current canonical tree, including all later user writes.
            if os.path.lexists(self.target):
                st = self.target.lstat()
                if identity(st) != self.doc['target_identity'] or not stat.S_ISDIR(st.st_mode):
                    raise Refusal('rollback canonical identity changed')
                rename_new(self.dest, str(self.uid), self.dest, self.doc['post_mapping'])
            else:
                st = (ANCHOR/self.doc['post_mapping']).lstat()
                if identity(st) != self.doc['target_identity']: raise Refusal('retained canonical identity changed')
            return self.withdraw_mapping(checkpoint)
        if phase != 'rolled_back': raise Refusal('unknown rollback phase')
        return {'phase': phase}


def main(argv=None):
    if os.getpid() == 1:
        # No arbitrary commands, imports, services or subprocesses from request.
        # Boot init remains running after errors: never exec systemd on failure.
        try:
            require_fence()
            request = protected_json(REQUEST, private=True)
            if set(request) != {'action', 'agent', 'logical'}: raise Refusal('unknown maintenance request field')
            value = Transaction(request['agent'], request['logical']).run(request['action'])
            print(json.dumps(value), flush=True)
        except BaseException as exc:
            # Do not expose private names/bytes through arbitrary exception text.
            print(json.dumps({'phase': 'refused', 'error_type': type(exc).__name__,
                              'reason': str(exc) if isinstance(exc, Refusal) else 'fixed maintenance exception'}), flush=True)
        while True: time.sleep(3600)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'check-pairing', 'relocate', 'rollback'))
    parser.add_argument('agent'); parser.add_argument('logical', nargs='?')
    args = parser.parse_args(argv)
    try:
        if args.action == 'check-pairing': value = check_pairing(args.agent)
        else:
            if not args.logical: raise Refusal('logical home required')
            value = (require_mapping(args.agent, args.logical) if args.action == 'check'
                     else Transaction(args.agent, args.logical).run(args.action))
        print(json.dumps(value, sort_keys=True)); return 0
    except (Refusal, OSError, KeyError) as exc:
        # Only fixed Refusal strings may go to the caller; OS paths may be private.
        reason = str(exc) if isinstance(exc, Refusal) else type(exc).__name__
        print('maintenance-needed: ' + reason, file=sys.stderr); return 2


if __name__ == '__main__': sys.exit(main())
