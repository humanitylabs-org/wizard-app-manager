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


def access_times(path):
    rows = []
    def walk(current, relative):
        st = current.lstat()
        rows.append([relative, st.st_atime_ns, st.st_mtime_ns])
        if stat.S_ISDIR(st.st_mode):
            fd = os.open(current, DIRECTORY | os.O_NOATIME)
            try:
                for name in sorted(os.listdir(fd)): walk(current/name, relative + [name])
            finally: os.close(fd)
    walk(path, [])
    return rows


def restore_times(path, rows, *, partial=False):
    for relative, atime, mtime in reversed(rows):
        if any(not isinstance(p, str) or p in ('', '.', '..') or '/' in p for p in relative):
            raise Refusal('invalid private timestamp record')
        current = path.joinpath(*relative)
        try: st = current.lstat()
        except FileNotFoundError:
            if partial: continue
            raise
        if (st.st_atime_ns, st.st_mtime_ns) != (atime, mtime):
            os.utime(current, ns=(atime, mtime), follow_symlinks=False)


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
    result = subprocess.run(['/usr/bin/cp', '-a', '--preserve=all', '--reflink=auto', '--',
                             str(source), str(target)], env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
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
    expected = {'schema': 1, 'uid': account.pw_uid, 'gid': account.pw_gid,
                'logical': str(logical), 'physical': str(ANCHOR/str(account.pw_uid))}
    if any(doc.get(k) != v for k, v in expected.items()): raise Refusal('storage mapping mismatch')
    with Pinned(ANCHOR) as parent:
        st = os.stat(str(account.pw_uid), dir_fd=parent.fd, follow_symlinks=False)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != account.pw_uid or st.st_gid != account.pw_gid
                or identity(st) != doc.get('identity')):
            raise Refusal('canonical storage identity changed')
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
        return st

    def matches(self, path, expected):
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or identity(st) != expected:
            raise Refusal('transaction endpoint identity changed')
        if fingerprint(path) != self.doc['fingerprint']: raise Refusal('opaque transfer metadata/bytes changed')

    def run(self, action='relocate', *, checkpoint=None):
        # No caller-supplied offline boolean or receipt can bypass this check.
        require_fence()
        if action not in ('relocate', 'rollback'): raise Refusal('unknown storage operation')
        if action == 'rollback': rollback_guard()
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
                if 'pending_copy' in self.doc:
                    pending = self.doc['pending_copy']
                    original = clean_path(pending['source'])
                    if identity(original.lstat()) != pending['source_identity']:
                        raise Refusal('interrupted copy source identity changed')
                    rows = protected_json(self.control/pending['times'], private=True, maxbytes=67108864)
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
        rows = access_times(source)
        if len(json.dumps(rows).encode()) > 67108864:
            raise Refusal('timestamp manifest exceeds finite storage limit')
        name = 'times-' + secrets.token_hex(16) + '.json'
        atomic(self.state, name, rows)
        self.doc['pending_copy'] = {'source': str(source), 'target': str(target), 'times': name,
                                    'source_identity': identity(source.lstat())}
        self.save(self.doc['phase'])
        copy_opaque(source, target)
        restore_times(source, rows); restore_times(target, rows)
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
            mapping['identity'] = self.doc['target_identity']
            with Pinned(ANCHOR/'.mappings') as maps: atomic(maps, f'{self.uid}.json', mapping, 0o644)
            self.mark('committed', checkpoint); phase = self.doc['phase']
        if phase != 'committed': raise Refusal('unknown transaction phase')
        if not self.logical.is_symlink() or os.readlink(self.logical) != str(self.target):
            raise Refusal('logical compatibility link drift')
        require_mapping(self.account.pw_name, self.logical)
        return {'phase': phase, 'physical': str(self.target), 'logical': str(self.logical)}

    def rollback(self, checkpoint):
        rollback_guard()  # also immediately before the first rollback mutation
        phase = self.doc['phase']
        if phase == 'rolled_back': return {'phase': phase}
        # Before link publication the original is still selected. Nothing needs
        # deletion; preserve all copies and simply withdraw any public mapping.
        if phase in ('prepared', 'snapshot_intent', 'snapshot_ready', 'transfer_intent', 'transfer_ready') or (
                phase in ('publish_intent', 'published') and self.logical.is_dir() and not self.logical.is_symlink()):
            self.source_check()
            self.mark('rolled_back', checkpoint)
            return {'phase': 'rolled_back'}
        if not phase.startswith('rollback'):
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
            with Pinned(ANCHOR/'.mappings') as maps:
                try: os.unlink(f'{self.uid}.json', dir_fd=maps.fd)
                except FileNotFoundError: pass
                os.fsync(maps.fd)
            self.mark('rolled_back', checkpoint); phase = self.doc['phase']
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
