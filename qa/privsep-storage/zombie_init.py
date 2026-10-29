#!/usr/bin/python3 -I
"""QA ONLY: deterministic actual-kernel child controls in a synthetic guest.

Not a product bootstrap/transaction substitute. Positive production boots use
maintenance-init directly, after this separate causal proof has succeeded.
"""
import ast
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

PRODUCT = Path('/usr/local/lib/wizard-storage/protected-agent-storage.py')
ROOT = Path('/root/storage-zombie-qa')


def load():
    spec = importlib.util.spec_from_file_location('kernel_reap_product', PRODUCT)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def state(pid):
    fields = Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid, 'state':fields[0], 'ppid':int(fields[1]), 'flags':int(fields[6]), 'start':int(fields[19])}


def snapshot():
    # Only fixed synthetic source/control metadata. No contents, broad trees,
    # environment or arbitrary exception payload can reach serial.
    paths = ['/etc/wizard-agent-storage/request.json', '/etc/wizard-agent-storage/completed.json',
             '/var/lib/wizard-agent-homes', '/var/lib/wizard-agent-storage',
             '/home/storageqa/.hermes/sentinel', '/home/hermes/.hermes/storage-production-sentinel']
    rows = []
    for value in paths:
        try:
            st = Path(value).lstat()
            rows.append([st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_size,st.st_mtime_ns,st.st_ctime_ns])
        except FileNotFoundError: rows.append(None)
    return rows


def zombie():
    # Child cannot exit before parent's explicit release. waitid WNOWAIT is an
    # actual exited-state barrier, NOT a sleep or a fixture reaping its subject.
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(w); assert os.read(r,1) == b'x'; os.close(r); os._exit(0)
        finally: os._exit(1)
    os.close(r); os.write(w,b'x'); os.close(w)
    info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    assert info is not None and info.si_pid == pid and info.si_code == os.CLD_EXITED and info.si_status==0
    row = state(pid)
    assert row['state']=='Z' and row['ppid']==1 and not row['flags'] & 0x00200000
    return pid, row


def old_fence(m):
    tree = ast.parse(PRODUCT.read_text())
    fn = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='require_fence')
    hits = [n for n in fn.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Name) and n.value.func.id=='reap_exited_children']
    assert len(hits)==1
    fn.body.remove(hits[0]); scope = m.__dict__.copy()
    exec(compile(ast.Module(body=[fn],type_ignores=[]), str(PRODUCT)+'-no-reaper-mutant', 'exec'),scope)
    return scope['require_fence']


def assert_refused(m, fn, reason):
    before = snapshot(); start = time.monotonic()
    try: fn()
    except m.Refusal as exc: assert str(exc)==reason
    else: raise AssertionError('expected guard refusal absent')
    assert time.monotonic()-start < 2, 'guard waited for live child'
    assert snapshot()==before


def exited_case(m):
    m.require_fence()  # housekeeping for genuine initramfs children, no retries
    before = snapshot(); pid, observed = zombie()
    try:
        assert_refused(m, old_fence(m), 'maintenance contains another userspace process')
        assert state(pid)==observed, 'mutant reaped its subject'
        assert m.require_fence()['userspace_tasks']==1
        assert not Path('/proc',str(pid)).exists(), 'actual zombie not reaped'
        assert m.require_fence()['userspace_tasks']==1  # bootstrap/main repeated call
        assert snapshot()==before
        return {'actual_zombie_barrier':True, 'old_guard_refused':True,
                'repaired_guard_admitted':True, 'zombie_absent':True, 'repeated_fence_admitted':True,
                'fixed_source_metadata_unchanged':True}
    finally:
        # Scoped fixture cleanup AFTER the assertions, never admission cleanup.
        try: os.waitpid(pid,os.WNOHANG)
        except ChildProcessError: pass


def live_case(m):
    m.require_fence()
    r,w = os.pipe(); ready_r,ready_w = os.pipe(); pid = os.fork()
    if pid==0:
        try:
            os.close(w); os.close(ready_r)
            # A daemon-looking name must not grant an exemption.
            libc=ctypes.CDLL(None); assert libc.prctl(15,b'systemd-udevd',0,0,0)==0
            fd=os.open(ROOT/'live-writer',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            os.write(fd,b'1'); os.write(ready_w,b'y')
            while True:
                command=os.read(r,1)
                if command==b'x':break
                assert command==b'p';os.write(fd,b'2');os.write(ready_w,b'z')
            os.close(fd);os.close(ready_w);os.close(r);os._exit(0)
        finally: os._exit(1)
    os.close(r); os.close(ready_w)
    try:
        assert os.read(ready_r,1)==b'y'
        prior=state(pid); assert prior['ppid']==1 and prior['state']!='Z'
        assert_refused(m,m.require_fence,'maintenance contains another userspace process')
        after=state(pid); assert after['start']==prior['start'] and after['state']!='Z'
        os.write(w,b'p');assert os.read(ready_r,1)==b'z'
        assert (ROOT/'live-writer').read_bytes()==b'12'
        return {'live_child_refused':True,'same_live_child_after_guard':True,
                'writer_progress_after_refusal':True,'no_guard_wait_or_kill':True,'fixed_source_metadata_unchanged':True}
    finally:
        os.write(w,b'x'); os.close(w);os.close(ready_r); assert os.waitpid(pid,0)[0]==pid


def nested_case(m):
    pid, observed=zombie(); calls=[]
    def forbidden(): calls.append(True); raise AssertionError('nested guard reached reaper')
    m.reap_exited_children=forbidden
    try:
        assert_refused(m,m.require_fence,'nested PID namespace')
        assert calls==[] and state(pid)==observed
        return {'actual_nested_pid1_refused_before_reaper':True,'zombie_still_unreaped':True}
    finally: assert os.waitpid(pid,0)[0]==pid


def prepare_proc(m):
    # Same fixed missing-proc prerequisite as bootstrap, not a namespace bypass.
    with m.Pinned('/proc') as proc:
        if m.namespace_fs_type(proc.fd) != 0x9FA0:
            assert not os.listdir(proc.fd)
            mount = ctypes.CDLL(None, use_errno=True).mount
            mount.argtypes = [ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_void_p]
            mount.restype = ctypes.c_int
            assert mount(b'proc',b'/proc',b'proc',2|4|8,None)==0


def main():
    assert os.getuid()==os.geteuid()==0 and os.getpid()==1 and sys.flags.isolated
    mode = 'nested' if sys.argv[1:]==['--nested'] else json.loads((ROOT/'mode.json').read_text())['mode']
    assert mode in ('exited','live','nested')
    row={'fixture':'storage-child-reap-v1','mode':mode,'passed':False,'production_acceptance':False}
    try:
        m=load(); prepare_proc(m)
        row['controls']={'exited':exited_case,'live':live_case,'nested':nested_case}[mode](m)
        row['passed']=True
    except BaseException as exc:
        # Source locations only, not exception strings/private values.
        frames=[]; tb=exc.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_filename in (str(PRODUCT),str(Path(__file__))):
                frames.append({'function':tb.tb_frame.f_code.co_name,'line':tb.tb_lineno})
            tb=tb.tb_next
        row['failure']={'class': 'assertion' if isinstance(exc,AssertionError) else 'guard-or-fixture-error','frames':frames[-12:]}
    print(json.dumps(row,sort_keys=True),flush=True)
    # PID1 must never exit. Host bounds the observation and stops its own VM.
    while True: time.sleep(3600)


if __name__=='__main__': main()
