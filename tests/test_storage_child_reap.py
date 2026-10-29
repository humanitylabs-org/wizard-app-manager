"""Queued unit controls. Real PID1 child causality lives in zombie_init.py."""
import ast
import errno
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCT = REPO/'scripts/protected-agent-storage.py'


def load():
    spec = importlib.util.spec_from_file_location('reap_under_test', PRODUCT)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def waits(monkeypatch, results):
    m = load(); calls = []; values = iter(results)
    def wait(pid, flags):
        calls.append((pid, flags)); value = next(values)
        if isinstance(value, BaseException): raise value
        return value
    proxy = SimpleNamespace(**{k:getattr(os,k) for k in dir(os)})
    proxy.waitpid = wait; monkeypatch.setattr(m, 'os', proxy)
    return m, calls


def test_echild_done_and_repeated_fence_housekeeping_safe(monkeypatch):
    m, calls = waits(monkeypatch, [ChildProcessError(errno.ECHILD, 'synthetic')]*2)
    m.reap_exited_children(); m.reap_exited_children()
    assert calls == [(-1, os.WNOHANG)]*2


def test_exited_and_signaled_children_reaped_then_echild(monkeypatch):
    m, calls = waits(monkeypatch, [(42, 0), (43, 9), OSError(errno.ECHILD, 'synthetic')])
    m.reap_exited_children(); assert calls == [(-1, os.WNOHANG)]*3


def test_zero_returns_without_another_wait(monkeypatch):
    m, calls = waits(monkeypatch, [(0, 0)])
    m.reap_exited_children(); assert calls == [(-1, os.WNOHANG)]


def test_finite_bound_refuses_even_if_last_child_was_final(monkeypatch):
    m, calls = waits(monkeypatch, [(42, 0)]*256)
    assert m.CHILD_REAP_CALL_LIMIT == 256
    with pytest.raises(m.Refusal, match='^child housekeeping bound exceeded$'): m.reap_exited_children()
    assert calls == [(-1, os.WNOHANG)]*256


@pytest.mark.parametrize('error', [4, 5, 1])
def test_unexpected_wait_errors_refuse_without_retry(monkeypatch, error):
    m, calls = waits(monkeypatch, [OSError(error, 'secret canary')])
    with pytest.raises(m.Refusal, match='^child housekeeping unavailable$'): m.reap_exited_children()
    assert len(calls) == 1


@pytest.mark.parametrize('result', [(-1, 0), (1, 0), (0, 1), (42, -1), (42, 0x7f),
                                  (True, 0), (42, False), [42, 0], (42,), None, (42, 65536)],
                         ids=['negative-pid','init-pid','nonzero-zero-status','negative-status','stopped',
                              'bool-pid','bool-status','list','short','none','oversize-status'])
def test_unexpected_wait_results_fail_closed(monkeypatch, result):
    m, calls = waits(monkeypatch, [result])
    with pytest.raises(m.Refusal, match='^unexpected child housekeeping result$'): m.reap_exited_children()
    assert len(calls) == 1


def fence_fixture(monkeypatch, *, foreign=False, nested=False, boot_mode=0o100755):
    m = load(); events = []
    class FakePath:
        def __init__(self, value): self.value = str(value)
        @property
        def name(self): return self.value.rsplit('/',1)[-1]
        @property
        def parent(self): return FakePath(self.value.rsplit('/',1)[0])
        def __truediv__(self, part): return FakePath(self.value+'/'+part)
        def absolute(self): return self
        def __fspath__(self): return self.value
        def read_text(self):
            return {'/proc/self/uid_map':'0 0 4294967295',
                    '/proc/self/status':'CapEff: 600000',
                    '/proc/cmdline':'init=/protected/init',
                    '/proc/44/stat':'44 (systemd-udevd) S 1 0 0 0 0 0'}[self.value]
        def iterdir(self):
            events.append(self.value)
            if self.value == '/proc': return iter([FakePath('/proc/1')]+([FakePath('/proc/44')] if foreign else []))
            assert self.value == '/proc/self/task'; return iter([1])
    class Pin:
        def __init__(self, *a): self.fd = 91
        def __enter__(self): return self
        def __exit__(self, *a): pass
    proxy = SimpleNamespace(**{k:getattr(os,k) for k in dir(os)})
    proxy.getuid = proxy.geteuid = lambda:0; proxy.getpid = lambda:1
    proxy.stat = lambda name, **kw: SimpleNamespace(st_mode=boot_mode, st_uid=0)
    monkeypatch.setattr(m, 'os', proxy); monkeypatch.setattr(m, 'Path', FakePath)
    monkeypatch.setattr(m, 'Pinned', Pin); monkeypatch.setattr(m, 'clean_path', lambda path:FakePath(path))
    def namespace():
        events.append('namespace')
        if nested: raise m.Refusal('nested PID namespace')
        return {'inode':m.PID_NS_INIT_INO}
    monkeypatch.setattr(m, 'require_initial_pid_namespace', namespace)
    monkeypatch.setattr(m, 'reap_exited_children', lambda:events.append('reap'))
    return m, events


def test_nested_namespace_refuses_before_any_reap(monkeypatch):
    m, events = fence_fixture(monkeypatch, nested=True)
    with pytest.raises(m.Refusal, match='^nested PID namespace$'): m.require_fence()
    assert events == ['namespace']


def test_unprotected_init_refuses_before_any_reap(monkeypatch):
    m, events = fence_fixture(monkeypatch, boot_mode=0o100777)
    with pytest.raises(m.Refusal, match='^unprotected recovery boot init$'): m.require_fence()
    assert events == ['namespace']


def test_zero_live_child_still_reaches_full_census_and_refuses(monkeypatch):
    m, events = fence_fixture(monkeypatch, foreign=True)
    calls = []; m.os.waitpid = lambda pid, flags:(calls.append((pid,flags)) or (0,0))
    m.reap_exited_children = load().reap_exited_children
    # Bind the original function to this isolated fixture's globals, not host os.
    import types
    m.reap_exited_children = types.FunctionType(m.reap_exited_children.__code__, m.__dict__)
    with pytest.raises(m.Refusal, match='^maintenance contains another userspace process$'): m.require_fence()
    assert calls == [(-1, os.WNOHANG)] and '/proc' in events


def test_repeated_fence_keeps_census_and_singlethread_checks(monkeypatch):
    m, events = fence_fixture(monkeypatch)
    assert m.require_fence()['userspace_tasks'] == m.require_fence()['userspace_tasks'] == 1
    assert events == ['namespace','reap','/proc','/proc/self/task']*2


def test_only_reap_call_added_to_existing_fence():
    tree = ast.parse(PRODUCT.read_text())
    fence = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='require_fence')
    text = ast.unparse(fence)
    assert text.index('require_initial_pid_namespace()') < text.index('Pinned(boot_init.parent)') < text.index('reap_exited_children()') < text.index("Path('/proc').iterdir()") < text.index("Path('/proc/self/task')")
    helper = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='reap_exited_children'))
    for forbidden in ('kill(', 'sleep(', 'signal(', 'waitid(', 'Path(', 'while '): assert forbidden not in helper
    assert 'os.waitpid(-1, os.WNOHANG)' in helper


def test_bootstrap_refusal_codes_are_finite_no_exception_echo():
    tree = ast.parse((REPO/'scripts/storage-maintenance-init.py').read_text())
    main = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    codes = next(n.value for n in ast.walk(main) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='codes' for t in n.targets))
    assert set(ast.literal_eval(codes).values()) == {'readonly-root','another-userspace-process','nested-pid-namespace'}
