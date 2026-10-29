"""Isolated contract units only; actual namespaces are proved in the cold VM.

Queued to forum. No fixture calls require_fence on the development host.
"""
import ast
import ctypes
import errno
import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCT = REPO/'scripts/protected-agent-storage.py'


def load():
    spec = importlib.util.spec_from_file_location('initial_pid_under_test', PRODUCT)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fixture(monkeypatch, *, release='6.1.0-54-cloud-amd64', machine='x86_64',
            inode=0xEFFFFFFC, mode=stat.S_IFREG | 0o444, fs=0x6E736673, nstype=0x20000000):
    m = load(); events = []
    def opened(path, flags):
        assert path == '/proc/self/ns/pid'
        assert flags == m.os.O_RDONLY | m.os.O_CLOEXEC
        events.append(('open', 91)); return 91
    def inspected(fd):
        assert fd == 91; events.append(('fstat', fd))
        return SimpleNamespace(st_dev=4, st_ino=inode, st_mode=mode)
    def ioctl(fd, command):
        assert fd == 91 and command == m.NS_GET_NSTYPE
        events.append(('ioctl', fd)); return nstype
    def fstype(fd):
        assert fd == 91; events.append(('fstatfs', fd)); return fs
    monkeypatch.setattr(m, 'os', SimpleNamespace(uname=lambda: SimpleNamespace(
        sysname='Linux', release=release, machine=machine), open=opened, fstat=inspected,
        close=lambda fd: events.append(('close', fd)), O_RDONLY=m.os.O_RDONLY, O_CLOEXEC=m.os.O_CLOEXEC))
    monkeypatch.setattr(m, 'fcntl', SimpleNamespace(ioctl=ioctl))
    monkeypatch.setattr(m, 'namespace_fs_type', fstype)
    return m, events


@pytest.mark.parametrize('release', ('6.1.0-54-cloud-amd64', '6.8.0-147-generic', '6.12.114', '6.17.0', '6.18.59', '7.3.0'))
def test_audited_initial_descriptor_admitted_and_closed(monkeypatch, release):
    m, events = fixture(monkeypatch, release=release)
    assert m.require_initial_pid_namespace() == {'device': 4, 'inode': 0xEFFFFFFC, 'type': 0x20000000}
    assert events == [('open', 91), ('fstat', 91), ('fstatfs', 91), ('ioctl', 91), ('close', 91)]


@pytest.mark.parametrize('inode', (0xEFFFFFFB, 0xEFFFFFFD, 0xF0000000, 0xFFFFFFFF, 1))
def test_wrong_and_dynamic_reused_inodes_refused(monkeypatch, inode):
    m, events = fixture(monkeypatch, inode=inode)
    with pytest.raises(m.Refusal, match='^nested PID namespace$'): m.require_initial_pid_namespace()
    assert events[-1] == ('close', 91)


@pytest.mark.parametrize('variant', ('filesystem', 'type', 'file-mode'))
def test_reserved_inode_alone_is_not_namespace_attestation(monkeypatch, variant):
    args = {'filesystem': {'fs': 0x65735546}, 'type': {'nstype': 0x10000000}, 'file-mode': {'mode': stat.S_IFDIR | 0o555}}
    m, events = fixture(monkeypatch, **args[variant])
    with pytest.raises(m.Refusal, match='^unattested PID namespace descriptor$'): m.require_initial_pid_namespace()
    assert events[-1] == ('close', 91)


@pytest.mark.parametrize('release', ('5.15.0', '6.6.160', '6.14.0', '7.4.0', 'unknown', '6.1', '6.1.0 extra', '6.1.0/forged'))
def test_unknown_kernel_contract_refuses_before_open(monkeypatch, release):
    m, events = fixture(monkeypatch, release=release)
    with pytest.raises(m.Refusal, match='^unsupported initial PID namespace identity contract$'): m.require_initial_pid_namespace()
    assert events == []


@pytest.mark.parametrize('machine', ('i686', 'armv7l', 'riscv64'))
def test_unaudited_statfs_abi_refuses_before_open(monkeypatch, machine):
    m, events = fixture(monkeypatch, machine=machine)
    with pytest.raises(m.Refusal, match='unsupported'): m.require_initial_pid_namespace()
    assert events == []


def test_aarch64_lp64_uses_same_attested_identity(monkeypatch):
    m, events = fixture(monkeypatch, machine='aarch64')
    assert m.require_initial_pid_namespace()['inode'] == m.PID_NS_INIT_INO
    assert events[-1] == ('close', 91)


@pytest.mark.parametrize('seam', ('open', 'fstat', 'fstatfs', 'ioctl'))
def test_kernel_inspection_error_refuses_never_accepts_eperm(monkeypatch, seam):
    m, events = fixture(monkeypatch)
    def failed(*args): raise OSError(errno.EPERM, 'synthetic ambiguous EPERM')
    target, key = (m.os, seam) if seam in ('open', 'fstat') else ((m.fcntl, 'ioctl') if seam == 'ioctl' else (m, 'namespace_fs_type'))
    monkeypatch.setattr(target, key, failed)
    with pytest.raises(m.Refusal, match='^initial PID namespace identity unavailable$'): m.require_initial_pid_namespace()
    assert events[-1:] == ([] if seam == 'open' else [('close', 91)])


def test_statfs_reads_same_fd_and_lp64_layout(monkeypatch):
    m = load(); calls = []
    def call(fd, address):
        calls.append(fd)
        ctypes.cast(address, ctypes.POINTER(m._StatFS)).contents.f_type = m.NSFS_MAGIC
        return 0
    libc = SimpleNamespace(fstatfs=SimpleNamespace())
    class Call:
        def __call__(self, *args): return call(*args)
    libc.fstatfs = Call()
    monkeypatch.setattr(m.ctypes, 'CDLL', lambda *a, **kw: libc)
    assert ctypes.sizeof(m._StatFS) == 120
    assert m.namespace_fs_type(91) == m.NSFS_MAGIC and calls == [91]


def test_statfs_failure_not_interpreted_as_magic(monkeypatch):
    m = load()
    class Failed:
        def __call__(self, *args): return -1
    monkeypatch.setattr(m.ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(fstatfs=Failed()))
    monkeypatch.setattr(m.ctypes, 'get_errno', lambda: errno.EIO)
    with pytest.raises(OSError) as exc: m.namespace_fs_type(91)
    assert exc.value.errno == errno.EIO


def test_guard_wired_before_boot_and_census_with_existing_checks_retained():
    text = PRODUCT.read_text(); tree = ast.parse(text)
    fence = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'require_fence')
    code = ast.unparse(fence)
    assert code.index('require_initial_pid_namespace()') < code.index("Path('/proc/cmdline')") < code.index("Path('/proc').iterdir()")
    for retained in ('os.getuid()', 'os.geteuid()', 'os.getpid()', "Path('/proc/self/uid_map')", 'CapEff', "Path('/proc/self/task')", 'Pinned(code.parent)'):
        assert retained in code
    assert '0xb702' not in code and 'NS_GET_PARENT' not in code
    assert 'sole-host-init-v2' in code
