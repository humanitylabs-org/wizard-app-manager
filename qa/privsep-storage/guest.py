#!/usr/bin/python3 -I
"""Disposable guest ONLY. Real boot-init admission and power-cut recovery cases.

No model/provider credentials, real payloads, production units or host mounts.
The parent runner kills QEMU at fsynced crash barriers, then boots this same
trusted PID1 driver again. It never reports a missing/unrun case as passing.
"""
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import time
import traceback

ROOT = Path('/root/storage-proof')
SOURCE = ROOT/'source'
PRODUCT = SOURCE/'scripts/protected-agent-storage.py'
REPORT = ROOT/'cases.json'
STATE = ROOT/'crash-state.json'
ANCHOR = Path('/var/lib/wizard-agent-homes')
CONTROL = Path('/var/lib/wizard-agent-storage')
CREDENTIALS = Path('/var/lib/wizard-os-migration/credentials')
NORMAL = ('ordinary-active-respawner-refused', 'root-offline-receipt-refused',
          'stopped-processes-alone-refused', 'nested-pid-init-refused', 'removed-fence-control-detected')
BARRIERS = ('publish_intent', 'after-publication', 'alias_intent', 'after-alias',
            'metadata_intent', 'after-metadata')
INIT = ('sole-init-admitted', 'surviving-respawner-refused', 'samefs-metadata',
        'samefs-rollback-before-db-intent', 'db-intent-rollback-refused',
        'malicious-source-symlink-refused', 'malicious-target-symlink-refused',
        'source-parent-swap-refused', 'target-parent-swap-refused',
        'nested-mount-refused', 'fifo-refused', 'external-hardlink-refused',
        'agent-leaf-write-and-parent-rename-denied', 'crossfs-metadata', 'crossfs-rollback')
CRASH = tuple(f'{fs}-crash-{phase}' for fs in ('samefs', 'crossfs') for phase in BARRIERS)
EXPECTED = NORMAL + INIT + CRASH


def product():
    spec = importlib.util.spec_from_file_location('protected_storage', PRODUCT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def persist(path, value):
    tmp = path.with_suffix('.new')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def receipt(name, fn):
    rows = json.loads(REPORT.read_text()) if REPORT.exists() else []
    if any(row['name'] == name for row in rows): return
    row = {'name': name, 'passed': False, 'phase': sys.argv[1]}
    try:
        detail = fn()
        row.update(passed=True, detail=detail or {})
    except Exception as exc:
        row.update(error_type=type(exc).__name__, frames=[{'file': Path(f.filename).name,
                   'function': f.name, 'line': f.lineno} for f in traceback.extract_tb(exc.__traceback__)])
        rows.append(row); persist(REPORT, rows)
        raise
    rows.append(row); persist(REPORT, rows)
    print('STORAGE_CASE:' + name + ':PASS', flush=True)


def command(argv):
    result = subprocess.run([str(v) for v in argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if result.returncode: raise AssertionError('fixed synthetic operation failed: ' + str(argv[0]))
    return result.stdout


def refuse(fn, contains):
    try: fn()
    except (ValueError, OSError) as exc:
        assert contains in str(exc), (type(exc).__name__, contains)
        return {'refusal': contains}
    raise AssertionError('required refusal absent')


def fixture(name):
    p = pwd.getpwnam(name); home = Path(p.pw_dir); source = home/'.hermes'
    source.mkdir(mode=0o700)
    paths = [source]
    (source/'nested').mkdir(mode=0o750); paths.append(source/'nested')
    (source/'held').write_bytes(b'opaque-held\x00\xff'); paths.append(source/'held')
    (source/'nested/payload').write_bytes(bytes(range(256))*32); paths.append(source/'nested/payload')
    os.link(source/'nested/payload', source/'hardlink'); paths.append(source/'hardlink')
    os.symlink('nested/payload', source/'relative-link'); paths.append(source/'relative-link')
    os.symlink(str(source/'nested/payload'), source/'absolute-logical-link'); paths.append(source/'absolute-logical-link')
    os.setxattr(source/'held', 'user.storage-proof', b'opaque-xattr\x00')
    command(['setfacl', '-m', 'u:65534:r--', source/'held'])
    for path in paths:
        os.chown(path, p.pw_uid, p.pw_gid, follow_symlinks=False)
        os.utime(path, ns=(1700000000123456789, 1700000100987654321), follow_symlinks=False)
    return source


def account_for(index): return 'hermes' if index == 0 else f'storage{index:02d}'


def prepare():
    assert os.geteuid() == 0 and os.getpid() != 1 and ROOT.is_dir()
    for index in range(36):
        name = account_for(index)
        command(['useradd', '-m', '-u', str(1100+index), '-s', '/bin/sh', name]); fixture(name)
    command(['mkfs.ext4', '-F', '/dev/vdb'])
    (ROOT/'pairing-sentinel.json').write_text('{"synthetic":"unchanged"}\n')
    # A live service with real systemd automatic restart, not a JSON/PID receipt.
    Path('/usr/local/libexec').mkdir(mode=0o755,exist_ok=True)
    writer = Path('/usr/local/libexec/storage-proof-writer.py')
    writer.write_text('import os,pathlib,sys,time\nf=open(sys.argv[1],"ab")\np=pathlib.Path(sys.argv[2])\nwith p.open("ab") as h: h.write(b"start\\n"); h.flush(); os.fsync(h.fileno())\na=pathlib.Path(sys.argv[1]).with_name("active-writes")\nfor _ in range(10):\n with a.open("ab") as h: h.write(b"writer\\n"); h.flush(); os.fsync(h.fileno())\n time.sleep(0.05)\n')
    writer.chmod(0o644)
    def start(name):
        home = Path(pwd.getpwnam(name).pw_dir)
        (Path('/etc/systemd/system/storage-proof-writer.service')).write_text(
            '[Unit]\nDescription=Synthetic automatic writer\nStartLimitIntervalSec=0\n[Service]\nUser='+name+'\n'
            'ExecStart=/usr/bin/python3 '+str(writer)+' '+str(home/'.hermes/held')+' '+str(home/'respawns')+'\n'
            'Restart=always\nRestartSec=0.1\n[Install]\nWantedBy=multi-user.target\n')
        command(['systemctl', 'daemon-reload']); command(['systemctl', 'start', 'storage-proof-writer.service'])
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            if (home/'respawns').exists() and len((home/'respawns').read_bytes().splitlines()) >= 2: return
            time.sleep(0.1)
        raise AssertionError('real respawner did not restart')
    start('hermes')
    m = product(); source = Path('/home/hermes/.hermes')
    inode = m.identity(source.lstat())
    def stable_bytes():
        # An active writer's OWN append-only file must change; do not claim an
        # impossible whole-tree freeze while it runs. Fixed sentinels, source
        # identity, absent transaction controls and pairing attest no relocation
        # mutation. Stopped-only refusal additionally compares the full tree.
        values=[]
        for path in ('held','nested/payload','hardlink'):
            fd=os.open(source/path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
            with os.fdopen(fd,'rb') as stream: values.append(hashlib.file_digest(stream,'sha256').hexdigest())
        return values
    before = stable_bytes()
    pairing = hashlib.sha256((ROOT/'pairing-sentinel.json').read_bytes()).hexdigest()
    def attempt(active=False):
        frozen = None if active else m.fingerprint(source)
        code = subprocess.run(['/usr/bin/python3','-I',str(PRODUCT),'relocate','hermes',str(source)], capture_output=True)
        assert code.returncode == 2 and b'maintenance-needed' in code.stderr
        assert not CONTROL.exists() and not ANCHOR.exists()
        assert stable_bytes() == before and m.identity(source.lstat()) == inode
        if not active: assert m.fingerprint(source) == frozen
        assert (source/'active-writes').stat().st_size > 0
        assert hashlib.sha256((ROOT/'pairing-sentinel.json').read_bytes()).hexdigest() == pairing
        return {'exit': code.returncode, 'automatic_restarts_observed': True,
                'payload_sentinels_and_pairing_unchanged': True, 'full_tree_compared': not active,
                'real_payload_write_syscalls_observed': True}
    receipt(NORMAL[0], lambda:attempt(True))
    (ROOT/'offline.json').write_text('{"stopped":true,"offline":true,"root_owned":true}\n')
    receipt(NORMAL[1], lambda:attempt(True))
    command(['systemctl','stop','storage-proof-writer.service'])
    receipt(NORMAL[2], attempt)
    def namespace():
        code = subprocess.run(['unshare','--pid','--fork','--mount-proc','/usr/bin/python3','-I',
                               str(ROOT/'namespace-check.py'),str(PRODUCT)], capture_output=True,timeout=30)
        assert code.returncode == 0 and code.stdout.strip() == b'NESTED_REFUSED'
        assert not CONTROL.exists() and not ANCHOR.exists()
        return {'actual_nested_pid1': True, 'refused': True}
    (ROOT/'namespace-check.py').write_text('import importlib.util,sys\ns=importlib.util.spec_from_file_location("s",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nassert __import__("os").getpid()==1\ntry:m.require_fence()\nexcept m.Refusal as e:\n assert str(e)=="nested PID namespace",str(e)\n print("NESTED_REFUSED")\nelse:raise AssertionError("nested admitted")\n')
    receipt(NORMAL[3], namespace)
    def removed_fence():
        start('storage35')
        mutable = product(); mutable.require_fence = lambda: {'UNSAFE_CONTROL': True}
        txn=mutable.Transaction('storage35','/home/storage35/.hermes')
        try:
            try: txn.run()
            except mutable.Refusal as exc:
                assert 'maintenance-needed' not in str(exc)
            # A later byte-drift refusal cannot substitute for the missing
            # pre-mutation maintenance guard. The root transaction has started
            # online while a real writer/respawner continues modifying payload.
            assert (CONTROL/'1135/journal.json').is_file() and txn.doc['phase']
            assert command(['systemctl','show','storage-proof-writer.service','--property=NRestarts','--value']).strip() != b'0'
            return {'removed_fence_permitted_forbidden_transaction': True, 'observed_phase':txn.doc['phase'],
                    'detector':'missing pre-mutation maintenance refusal; not a data-corruption claim'}
        finally: command(['systemctl','stop','storage-proof-writer.service'])
    receipt(NORMAL[4], removed_fence)
    (ROOT/'init.sh').write_text('#!/bin/sh\nmount -t proc proc /proc 2>/dev/null || :\nmount -t sysfs sysfs /sys 2>/dev/null || :\nmount -o remount,rw /\nexec /usr/bin/python3 -I '+str(Path(__file__))+' init\n')
    (ROOT/'init.sh').chmod(0o755)
    os.sync()


def crash(name, phase):
    persist(STATE, {'name':name,'resume':True,'phase':phase})
    os.sync()
    print('STORAGE_CRASH_BARRIER:'+name, flush=True)
    while True: time.sleep(1)


def init():
    assert os.getpid() == 1 and os.geteuid() == 0
    m = product()
    receipt(INIT[0], m.require_fence)
    def surviving():
        pid = os.fork()
        if pid == 0:
            os.setgid(1100); os.setuid(1100)
            while True: time.sleep(1)
        try:
            return refuse(lambda:m.Transaction('hermes','/home/hermes/.hermes').run(), 'another userspace process')
        finally: os.kill(pid,signal.SIGKILL); os.waitpid(pid,0)
    receipt(INIT[1], surviving)
    def move(name, method):
        source = Path(pwd.getpwnam(name).pw_dir)/'.hermes'; uid=pwd.getpwnam(name).pw_uid
        before=m.fingerprint(source); original=m.identity(source.lstat())
        result=m.Transaction(name,source).run()
        doc=m.protected_json(CONTROL/str(uid)/'journal.json',private=True)
        assert result['phase']=='committed' and doc['method']==method
        assert m.fingerprint(ANCHOR/str(uid))==before
        assert source.is_symlink() and os.readlink(source)==str(ANCHOR/str(uid))
        assert (m.identity((ANCHOR/str(uid)).lstat())==original)==(method=='rename')
        # Absolute logical symlink remains valid, not text rewritten.
        assert (source/'absolute-logical-link').resolve()==ANCHOR/str(uid)/'nested/payload'
        return {'method':method,'metadata_exact':True,'logical_preserved':True}
    receipt(INIT[2],lambda:move('hermes','rename'))
    def rollback(name):
        source=Path(pwd.getpwnam(name).pw_dir)/'.hermes';uid=pwd.getpwnam(name).pw_uid
        doc=m.protected_json(CONTROL/str(uid)/'journal.json',private=True)
        result=m.Transaction(name,source).run('rollback')
        assert result['phase']=='rolled_back' and not source.is_symlink()
        assert m.fingerprint(source)==doc['fingerprint']
        assert not (ANCHOR/'.mappings'/f'{uid}.json').exists()
        return {'restored':True,'copies_retained':True}
    receipt(INIT[3],lambda:rollback('hermes'))
    def db_intent():
        move('storage01','rename')
        CREDENTIALS.mkdir(parents=True,mode=0o700)
        CREDENTIALS.parent.chmod(0o700);CREDENTIALS.chmod(0o700)
        persist(CREDENTIALS/'journal.json',{'phase':'db_intent'})
        paths=[CONTROL/'1101/journal.json',ANCHOR/'.mappings/1101.json']
        before=[p.read_bytes() for p in paths];ino=m.identity((ANCHOR/'1101').lstat())
        result=refuse(lambda:m.Transaction('storage01','/home/storage01/.hermes').run('rollback'),'db_intent-or-later')
        assert [p.read_bytes() for p in paths]==before and m.identity((ANCHOR/'1101').lstat())==ino
        assert Path('/home/storage01/.hermes').is_symlink()
        # Synthetic credential journal only; retain it, do not rotate credentials.
        (CREDENTIALS/'journal.json').rename(CREDENTIALS/'db-intent-proof-retained.json')
        return result|{'journal_mapping_payload_unchanged':True,'credentials_rotated':False}
    receipt(INIT[4],db_intent)
    def source_link():
        p=Path('/home/storage02/.hermes');p.rename(p.with_name('.hermes-original'))
        p.symlink_to(p.with_name('.hermes-original'))
        return refuse(lambda:m.Transaction('storage02',p).run(),'real agent-owned directory')
    receipt(INIT[5],source_link)
    def target_link():
        (ANCHOR/'1103').symlink_to('/home/storage03/.hermes')
        return refuse(lambda:m.Transaction('storage03','/home/storage03/.hermes').run(),'destination already exists')
    receipt(INIT[6],target_link)
    def parent_swap(target=False):
        name='storage05' if target else 'storage04';path=Path('/home/'+name)
        def hook(phase):
            if phase=='snapshot_ready':
                parent=ANCHOR if target else path
                parent.rename(parent.with_name(parent.name+'-swapped'))
                parent.mkdir(mode=0o755)
                if not target:os.chown(parent,pwd.getpwnam(name).pw_uid,pwd.getpwnam(name).pw_gid)
        try:return refuse(lambda:m.Transaction(name,path/'.hermes').run(checkpoint=hook),'parent identity changed')
        finally:
            parent=ANCHOR if target else path
            parent.rmdir();parent.with_name(parent.name+'-swapped').rename(parent)
    receipt(INIT[7],lambda:parent_swap(False));receipt(INIT[8],lambda:parent_swap(True))
    def nested():
        path=Path('/home/storage06/.hermes/nested')
        command(['mount','-t','tmpfs','-o','size=1m','storage-nested',path])
        try:return refuse(lambda:m.Transaction('storage06',path.parent).run(),'nested payload mount')
        finally:command(['umount',path])
    receipt(INIT[9],nested)
    def fifo():
        path=Path('/home/storage07/.hermes');os.mkfifo(path/'not-a-file')
        return refuse(lambda:m.Transaction('storage07',path).run(),'special payload file')
    receipt(INIT[10],fifo)
    def hardlink():
        path=Path('/home/storage08/.hermes');os.link(path/'held',path.parent/'external')
        return refuse(lambda:m.Transaction('storage08',path).run(),'hardlink escapes')
    receipt(INIT[11],hardlink)
    def agent_denial():
        move('storage09','rename');uid=pwd.getpwnam('storage09').pw_uid
        pid=os.fork()
        if pid==0:
            try:
                os.setgid(uid);os.setuid(uid)
                leaf=ANCHOR/str(uid);(leaf/'agent-write').write_bytes(b'agent-useful')
                (leaf/'agent-write').rename(leaf/'agent-renamed')
                try:leaf.rename(ANCHOR/(str(uid)+'-replaced'))
                except PermissionError:os._exit(0)
            except BaseException:pass
            os._exit(1)
        _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==0
        return {'leaf_useful':True,'canonical_parent_rename_denied':True}
    receipt(INIT[12],agent_denial)
    def crash_case(fs,phase,index):
        name=f'{fs}-crash-{phase}';account=account_for(index);source=Path('/home/'+account+'/.hermes')
        state=json.loads(STATE.read_text()) if STATE.exists() else {}
        if state.get('name')==name and state.get('resume'):
            result=m.Transaction(account,source).run()
            doc=m.protected_json(CONTROL/str(pwd.getpwnam(account).pw_uid)/'journal.json',private=True)
            assert result['phase']=='committed' and m.fingerprint(ANCHOR/str(pwd.getpwnam(account).pw_uid))==doc['fingerprint']
            assert doc['method']==('rename' if fs=='samefs' else 'copy')
            persist(STATE,{'name':name,'resume':False})
            return {'real_qemu_power_cut_resumed':True,'boundary':phase}
        atomic=m.atomic;rename=m.rename_new;symlink=os.symlink
        def atomic_hook(parent,key,value,mode=0o600):
            atomic(parent,key,value,mode)
            if phase=='after-metadata' and key==str(pwd.getpwnam(account).pw_uid)+'.json':crash(name,phase)
        def rename_hook(oldp,old,newp,new):
            rename(oldp,old,newp,new)
            if phase=='after-publication' and new==str(pwd.getpwnam(account).pw_uid):crash(name,phase)
        def symlink_hook(*args,**kwargs):
            symlink(*args,**kwargs)
            if phase=='after-alias':crash(name,phase)
        def hook(value):
            if value==phase:crash(name,phase)
        m.atomic=atomic_hook;m.rename_new=rename_hook;os.symlink=symlink_hook
        try:m.Transaction(account,source).run(checkpoint=hook)
        finally:m.atomic=atomic;m.rename_new=rename;os.symlink=symlink
        raise AssertionError('crash barrier not reached')
    # The order is stable across actual kernel reboots; completed case rows are
    # skipped only by exact persisted identity, never counted twice.
    for i,phase in enumerate(BARRIERS,10):
        receipt(f'samefs-crash-{phase}',lambda phase=phase,i=i:crash_case('samefs',phase,i))
    # Actual distinct block filesystem; no mocked EXDEV or host mount effects.
    ANCHOR.mkdir(exist_ok=True)
    command(['mount','/dev/vdb',ANCHOR])
    try:
        assert ANCHOR.stat().st_dev!=Path('/home/storage20/.hermes').stat().st_dev
        receipt(INIT[13],lambda:move('storage20','copy'))
        receipt(INIT[14],lambda:rollback('storage20'))
        for i,phase in enumerate(BARRIERS,21):
            receipt(f'crossfs-crash-{phase}',lambda phase=phase,i=i:crash_case('crossfs',phase,i))
    finally:
        command(['umount',ANCHOR])
    rows=json.loads(REPORT.read_text());names=[r['name'] for r in rows]
    assert len(names)==len(set(names))==len(EXPECTED) and set(names)==set(EXPECTED)
    assert all(r['passed'] is True for r in rows)
    persist(ROOT/'complete.json',{'passed':True,'expected':list(EXPECTED),'cases':len(rows),'runtime_scope':'storage seam only; not installed gateway/Service acceptance'})
    os.sync();print('STORAGE_PROOF_COMPLETE',flush=True)
    ctypes.CDLL(None).reboot(0x4321FEDC)  # power off this disposable guest only
    while True:time.sleep(1)


if __name__=='__main__':
    try:
        if sys.argv[1]=='prepare':prepare()
        elif sys.argv[1]=='init':init()
        else:raise AssertionError('unknown fixed guest phase')
    except BaseException as exc:
        persist(ROOT/'first-error.json',{'phase':sys.argv[1],'error_type':type(exc).__name__,
                'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]})
        print('STORAGE_PROOF_FAILED',flush=True)
        if os.getpid()!=1:raise
        os.sync()
        while True:time.sleep(1)
