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
PENDING = ('samefs-crash-pending-unchanged', 'samefs-crash-pending-substitution',
           'samefs-crash-pending-edit', 'samefs-crash-pending-ambiguous', 'samefs-crash-pending-privacy')
ROLLBACK_BARRIERS = ('rollback_unavailable_public', 'rollback_unavailable_intent',
    'rollback_copy_intent', 'rollback_alias_intent', 'rollback_restore_intent',
    'rollback_metadata_intent', 'rollback_withdraw_intent', 'rollback_after_withdrawal',
    'pending-rollback-copy', 'after-logical-restoration')
SECURITY = ('samefs-private-leaf-755-refused', 'samefs-private-leaf-750-refused',
    'samefs-private-leaf-acl-refused', 'samefs-default-parent-acl-refused',
    'crossfs-private-leaf-755-refused', 'crossfs-private-leaf-750-refused',
    'crossfs-private-leaf-acl-refused', 'crossfs-default-parent-acl-refused',
    'mapping-drifted-alias-refused', 'mapping-absent-alias-refused',
    'mapping-directory-alias-refused', 'mapping-relative-alias-refused',
    'samefs-private-commit-other-uid-denied', 'crossfs-private-commit-other-uid-denied',
    'crossfs-default-anchor-acl-refused','samefs-early-rollback-unavailable','crossfs-early-rollback-unavailable',
    'samefs-prealias-rollback-unavailable','crossfs-prealias-rollback-unavailable')
ROLLBACK_CRASH = tuple(f'{fs}-crash-{phase}' for fs in ('samefs', 'crossfs') for phase in ROLLBACK_BARRIERS)
CUTS = CRASH + PENDING + ('crossfs-crash-private-stage',) + ROLLBACK_CRASH
EXPECTED = NORMAL + INIT + CRASH + PENDING + SECURITY + ('crossfs-crash-private-stage',) + ROLLBACK_CRASH
INTERVENTIONS = PENDING + ('crossfs-crash-private-stage',)


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
    for index in range(100):
        name = account_for(index)
        command(['useradd', '-m', '-u', str(1100+index), '-s', '/bin/sh', name]); fixture(name)
    # Bounded actual copy window, not sparse files/reflink publication barriers.
    for index in (40,41,42,43,44,58,68,88):
        p=pwd.getpwnam(account_for(index));path=Path(p.pw_dir)/'.hermes/large-copy'
        with path.open('wb') as stream:
            chunk=b'opaque-synthetic-copy-window\x00' * 2048
            for _ in range(4096): stream.write(chunk)
        os.chown(path,p.pw_uid,p.pw_gid)
    outside=ROOT/'outside';outside.mkdir(mode=0o700);(outside/'payload').write_bytes(b'external synthetic sentinel')
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


def denied_other_uid(paths):
    """Real kernel denial, with no inherited supplementary group authority."""
    paths=[Path(p) for p in paths];assert paths and all(p.is_dir() for p in paths)
    pid=os.fork()
    if pid==0:
        try:
            os.setgroups([]);os.setgid(65534);os.setuid(65534)
            for path in paths:
                try:fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
                except PermissionError:pass
                else:os.close(fd);os._exit(2)
                try:os.open(path/'held',os.O_RDONLY)
                except PermissionError:pass
                else:os._exit(3)
            os._exit(0)
        except BaseException:os._exit(4)
    _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==0
    return {'other_uid':65534,'supplementary_groups':[],'denied_endpoints':len(paths)}


def fixed_cp_cut(m, name, predicate):
    """Intercept only the real fixed cp invocation, never substitute a copy.

    The child must hold the actual source large file open and have an incomplete
    target file. Stop it, attest the kernel stop, then let the HOST kill QEMU.
    Errors kill/reap this child before failing; normal product code is unchanged.
    """
    original=m.subprocess.run
    def run(argv, **kwargs):
        if not (argv[0]=='/usr/bin/cp' and predicate(Path(argv[-2]),Path(argv[-1]))):
            return original(argv,**kwargs)
        assert argv[:6]==['/usr/bin/cp','-a','--preserve=all','--reflink=auto','--',argv[-2]]
        source,target=Path(argv[-2]),Path(argv[-1]);child=subprocess.Popen(argv,**{k:v for k,v in kwargs.items() if k!='check'})
        try:
            end=time.monotonic()+30;observed=False
            while time.monotonic()<end and child.poll() is None:
                try:
                    active=[os.readlink(p) for p in Path(f'/proc/{child.pid}/fd').iterdir()]
                    incomplete=(target/'large-copy').exists() and (target/'large-copy').stat().st_size<(source/'large-copy').stat().st_size
                    if str(source/'large-copy') in active and incomplete:
                        child.send_signal(signal.SIGSTOP);pid,status=os.waitpid(child.pid,os.WUNTRACED)
                        assert pid==child.pid and os.WIFSTOPPED(status)
                        assert (target/'large-copy').stat().st_size<(source/'large-copy').stat().st_size
                        observed=True;break
                except FileNotFoundError:pass
                time.sleep(0.001)
            assert observed,'actual incomplete cp window not observed'
            rows=json.loads((ROOT/'copy-events.json').read_text()) if (ROOT/'copy-events.json').exists() else []
            rows.append({'case':name,'source':str(source),'target':str(target),
                'source_identity':m.identity(source.lstat()),'partial_identity':m.identity(target.lstat()),
                'actual_cp_pid':child.pid,'source_fd_observed':True,'kernel_stopped':True,
                'partial_bytes':(target/'large-copy').stat().st_size,'source_bytes':(source/'large-copy').stat().st_size})
            persist(ROOT/'copy-events.json',rows);crash(name,'actual-pending-copy')
        finally:
            if child.poll() is None:child.kill()
            child.wait(timeout=30)
    return original,run


def intervene():
    assert os.geteuid()==0 and os.getpid()!=1
    state=json.loads(STATE.read_text());name=state['name'];assert name in INTERVENTIONS and state['resume']
    m=product();event=next(r for r in json.loads((ROOT/'copy-events.json').read_text()) if r['case']==name)
    source=Path(event['source']);target=Path(event['target'])
    # A crossfs abandoned stage must be inspected on its actual filesystem.
    mounted=name=='crossfs-crash-private-stage'
    if mounted:command(['mount','/dev/vdb',ANCHOR])
    try:
        assert m.identity(target.lstat())==event['partial_identity']
        denied=denied_other_uid([source,target])
        if name.endswith('-substitution'):
            (source/'nested').rename(source/'nested-retained');(source/'nested').symlink_to(ROOT/'outside')
        elif name.endswith('-edit'):
            fd=os.open(source/'nested/payload',os.O_WRONLY|os.O_APPEND)
            try:os.write(fd,b'legitimate inter-boot edit');os.fsync(fd)
            finally:os.close(fd)
        elif name.endswith('-ambiguous'):
            uid=pwd.getpwnam(source.parent.name).pw_uid
            path=CONTROL/str(uid)/'journal.json';doc=m.protected_json(path,private=True)
            doc['pending_copy'].pop('schema');persist(path,doc)
        elif name.endswith('-privacy'):source.chmod(0o750)
        rows=json.loads((ROOT/'intervention-results.json').read_text()) if (ROOT/'intervention-results.json').exists() else []
        rows.append({'case':name,'source_graph_after':m.graph(source),'external_graph':m.graph(ROOT/'outside'),
                     'external_stat':list((ROOT/'outside/payload').lstat()),
                     'external_atime_ns':(ROOT/'outside/payload').lstat().st_atime_ns,
                     'external_mtime_ns':(ROOT/'outside/payload').lstat().st_mtime_ns,
                     'partial_identity':event['partial_identity'],'denial':denied,'passed':True})
        persist(ROOT/'intervention-results.json',rows);os.sync()
    finally:
        if mounted:command(['umount',ANCHOR])


def init():
    assert os.getpid() == 1 and os.geteuid() == 0
    m = product()
    def sole_init():
        # Compare v2 evidence with independent facts on one real live FD.
        # Device identity is per boot, never a hard-coded observation/token.
        value = m.require_fence()
        import fcntl
        fd = os.open('/proc/self/ns/pid', os.O_RDONLY | os.O_CLOEXEC)
        try:
            st = os.fstat(fd); buf = (ctypes.c_long * 32)()
            libc = ctypes.CDLL(None, use_errno=True)
            assert libc.fstatfs(fd, ctypes.byref(buf)) == 0
            nstype = fcntl.ioctl(fd, 0xB703)
            assert stat.S_ISREG(st.st_mode) and buf[0] == 0x6E736673
            assert st.st_ino == 0xEFFFFFFC and nstype == 0x20000000
            assert value == {'mechanism': 'sole-host-init-v2', 'userspace_tasks': 1,
                'pid_namespace': {'device': st.st_dev, 'inode': st.st_ino, 'type': nstype}}
        finally: os.close(fd)
        return value
    receipt(INIT[0], sole_init)
    def surviving():
        pid = os.fork()
        if pid == 0:
            os.setgroups([]); os.setgid(1100); os.setuid(1100)
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
                os.setgroups([]);os.setgid(uid);os.setuid(uid)
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
    def pending_case(name,index):
        account=account_for(index);source=Path('/home/'+account+'/.hermes');uid=pwd.getpwnam(account).pw_uid
        state=json.loads(STATE.read_text()) if STATE.exists() else {}
        if state.get('name')==name and state.get('resume'):
            intervention=next(r for r in json.loads((ROOT/'intervention-results.json').read_text()) if r['case']==name)
            event=next(r for r in json.loads((ROOT/'copy-events.json').read_text()) if r['case']==name)
            partial=Path(event['target']);assert m.identity(partial.lstat())==event['partial_identity']
            if name.endswith(('-unchanged','-private-stage')):
                result=m.Transaction(account,source).run();assert result['phase']=='committed'
                m.require_mapping(account,source);denied_other_uid([ANCHOR/str(uid),partial])
            else:
                journal=CONTROL/str(uid)/'journal.json';before=journal.read_bytes()
                reason='ambiguous interrupted copy record' if name.endswith('-ambiguous') else 'privacy layout' if name.endswith('-privacy') else 'source graph changed'
                refuse(lambda:m.Transaction(account,source).run(),reason)
                assert journal.read_bytes()==before and m.graph(source)==intervention['source_graph_after']
            outside=(ROOT/'outside/payload').lstat()
            assert m.graph(ROOT/'outside')==intervention['external_graph']
            assert list(outside)==intervention['external_stat']
            assert outside.st_atime_ns==intervention['external_atime_ns'] and outside.st_mtime_ns==intervention['external_mtime_ns']
            assert m.identity(partial.lstat())==event['partial_identity']
            persist(STATE,{'name':name,'resume':False})
            return {'actual_cp_interrupted':True,'intervening_normal_boot':True,'all_partial_copies_retained':True,
                    'external_metadata_unchanged':True,'changed_source_metadata_preserved':not name.endswith(('-unchanged','-private-stage'))}
        predicate=(lambda src,dst:dst.parent==ANCHOR and dst.name.startswith('.staging-')) if name.endswith('-private-stage') else (lambda src,dst:src==source)
        original,hook=fixed_cp_cut(m,name,predicate);m.subprocess.run=hook
        try:m.Transaction(account,source).run()
        finally:m.subprocess.run=original
        raise AssertionError('actual pending copy cut not reached')

    def privacy_negative(fs,variant,index):
        account=account_for(index);p=pwd.getpwnam(account);source=Path(p.pw_dir)/'.hermes'
        source.parent.chmod(0o700)
        if variant in ('755','750'):source.chmod(int(variant,8))
        elif variant=='acl':command(['setfacl','-m','u:65534:rx',source])
        else:command(['setfacl','-m','d:u:65534:rwx',source.parent])
        before=m.graph(source);denied_other_uid([source])
        try:
            reason='default ACL' if variant=='parent' else 'privacy layout'
            refuse(lambda:m.Transaction(account,source).run(),reason)
            assert m.graph(source)==before and not (CONTROL/str(p.pw_uid)).exists()
            assert not (ANCHOR/str(p.pw_uid)).exists()
            denied_other_uid([source])
            return {'pre_mutation_refusal':True,'source_graph_unchanged':True,'no_control_or_payload_creation':True,'filesystem':fs}
        finally:
            if variant=='parent':command(['setfacl','-k',source.parent])

    def alias_negative(variant,index):
        account=account_for(index);source=Path('/home/'+account+'/.hermes')
        move(account,'rename');m.require_mapping(account,source);source.unlink()
        if variant=='drifted':source.symlink_to(ROOT/'outside')
        elif variant=='directory':source.mkdir(mode=0o700)
        elif variant=='relative':source.symlink_to('../../var/lib/wizard-agent-homes/'+str(pwd.getpwnam(account).pw_uid))
        outside=(ROOT/'outside/payload').lstat()
        try:m.require_mapping(account,source)
        except (m.Refusal,OSError):pass
        else:raise AssertionError('stale alias accepted')
        assert (ROOT/'outside/payload').lstat()==outside
        return {'public_mapping_refused':True,'no_target_adopted':True,'private_target_unread':True}

    def private_commit(index,method):
        account=account_for(index);move(account,method)
        return denied_other_uid([ANCHOR/str(pwd.getpwnam(account).pw_uid)])

    def anchor_acl():
        source=Path('/home/storage91/.hermes');before=m.graph(source)
        command(['setfacl','-m','d:u:65534:rwx',ANCHOR])
        try:
            refuse(lambda:m.Transaction('storage91',source).run(),'default ACL')
            assert not (CONTROL/'1191').exists() and not (ANCHOR/'1191').exists() and m.graph(source)==before
            return {'before_state_or_payload_creation':True,'target_default_acl_refused':True}
        finally:command(['setfacl','-k',ANCHOR])

    def early_rollback(index,phase='snapshot_ready'):
        account=account_for(index);source=Path('/home/'+account+'/.hermes');uid=pwd.getpwnam(account).pw_uid
        class Paused(Exception):pass
        def stop(value):
            if value==phase:raise Paused()
        before=m.fingerprint(source)
        try:m.Transaction(account,source).run(checkpoint=stop)
        except Paused:pass
        else:raise AssertionError('early rollback setup did not stop')
        result=m.Transaction(account,source).run('rollback');assert result['phase']=='rolled_back'
        assert m.fingerprint(source)==before and not (ANCHOR/'.mappings'/f'{uid}.json').exists()
        refuse(lambda:m.Transaction(account,source).run(),'cannot resume forward')
        return {'logical_bytes_metadata_retained':True,'readiness_withdrawn':True,'forward_resume_denied':True,'start_phase':phase}

    def rollback_cut(fs,phase,index):
        name=f'{fs}-crash-{phase}';account=account_for(index);source=Path('/home/'+account+'/.hermes');uid=pwd.getpwnam(account).pw_uid
        state=json.loads(STATE.read_text()) if STATE.exists() else {}
        if state.get('name')==name and state.get('resume'):
            try:m.require_mapping(account,source)
            except (m.Refusal,OSError):pass
            else:raise AssertionError('interrupted rollback accepted as ready')
            doc=m.protected_json(CONTROL/str(uid)/'journal.json',private=True)
            before=source.lstat() if os.path.lexists(source) else None
            refuse(lambda:m.Transaction(account,source).run(),'forward resume forbidden' if phase=='rollback_unavailable_public' else 'cannot resume forward')
            assert (source.lstat() if os.path.lexists(source) else None)==before
            result=m.Transaction(account,source).run('rollback');assert result['phase']=='rolled_back'
            assert source.is_dir() and not source.is_symlink() and m.fingerprint(source)==doc['fingerprint']
            final=m.protected_json(CONTROL/str(uid)/'journal.json',private=True)
            assert (ANCHOR/final['post_mapping']/'later-write').read_bytes()==b'retain later user bytes'
            assert not (ANCHOR/'.mappings'/f'{uid}.json').exists()
            assert m.Transaction(account,source).run('rollback')['phase']=='rolled_back'
            persist(STATE,{'name':name,'resume':False})
            return {'rollback_power_cut_resumed':True,'public_unavailable_during_recovery':True,'forward_resume_denied':True,'later_bytes_retained':True,'boundary':phase}
        move(account,'rename' if fs=='samefs' else 'copy')
        (ANCHOR/str(uid)/'later-write').write_bytes(b'retain later user bytes')
        original_rename=m.rename_new;original_run=m.subprocess.run
        def rename_hook(oldp,old,newp,new):
            original_rename(oldp,old,newp,new)
            if phase=='after-logical-restoration' and newp.path==source.parent and new==source.name:crash(name,phase)
        def checkpoint(value):
            if value==phase:crash(name,phase)
        setattr(m,'rename_new',rename_hook)
        if phase=='pending-rollback-copy':
            _,m.subprocess.run=fixed_cp_cut(m,name,lambda src,dst:dst.parent==source.parent and dst.name.startswith('.wizard-restore-'))
        try:m.Transaction(account,source).run('rollback',checkpoint=checkpoint)
        finally:setattr(m,'rename_new',original_rename);m.subprocess.run=original_run
        raise AssertionError('rollback crash not reached')

    # The order is stable across actual kernel reboots; completed case rows are
    # skipped only by exact persisted identity, never counted twice.
    for i,phase in enumerate(BARRIERS,10):
        receipt(f'samefs-crash-{phase}',lambda phase=phase,i=i:crash_case('samefs',phase,i))
    for i,name in enumerate(PENDING,40):receipt(name,lambda name=name,i=i:pending_case(name,i))
    for i,variant in enumerate(('755','750','acl','parent'),36):
        name='samefs-default-parent-acl-refused' if variant=='parent' else f'samefs-private-leaf-{variant}-refused'
        receipt(name,lambda variant=variant,i=i:privacy_negative('samefs',variant,i))
    for i,variant in enumerate(('drifted','absent','directory','relative'),52):
        receipt(f'mapping-{variant}-alias-refused',lambda variant=variant,i=i:alias_negative(variant,i))
    receipt('samefs-private-commit-other-uid-denied',lambda:private_commit(56,'rename'))
    receipt('samefs-early-rollback-unavailable',lambda:early_rollback(92))
    receipt('samefs-prealias-rollback-unavailable',lambda:early_rollback(94,'link_intent'))
    for i,phase in enumerate(ROLLBACK_BARRIERS,60):
        receipt(f'samefs-crash-{phase}',lambda phase=phase,i=i:rollback_cut('samefs',phase,i))
    # Actual distinct block filesystem; no mocked EXDEV or host mount effects.
    ANCHOR.mkdir(exist_ok=True)
    command(['mount','/dev/vdb',ANCHOR])
    try:
        assert ANCHOR.stat().st_dev!=Path('/home/storage20/.hermes').stat().st_dev
        receipt(INIT[13],lambda:move('storage20','copy'))
        receipt(INIT[14],lambda:rollback('storage20'))
        for i,phase in enumerate(BARRIERS,21):
            receipt(f'crossfs-crash-{phase}',lambda phase=phase,i=i:crash_case('crossfs',phase,i))
        for i,variant in enumerate(('755','750','acl','parent'),48):
            name='crossfs-default-parent-acl-refused' if variant=='parent' else f'crossfs-private-leaf-{variant}-refused'
            receipt(name,lambda variant=variant,i=i:privacy_negative('crossfs',variant,i))
        receipt('crossfs-private-commit-other-uid-denied',lambda:private_commit(57,'copy'))
        receipt('crossfs-default-anchor-acl-refused',anchor_acl)
        receipt('crossfs-early-rollback-unavailable',lambda:early_rollback(93))
        receipt('crossfs-prealias-rollback-unavailable',lambda:early_rollback(95,'link_intent'))
        receipt('crossfs-crash-private-stage',lambda:pending_case('crossfs-crash-private-stage',58))
        for i,phase in enumerate(ROLLBACK_BARRIERS,80):
            receipt(f'crossfs-crash-{phase}',lambda phase=phase,i=i:rollback_cut('crossfs',phase,i))
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
        elif sys.argv[1]=='intervene':intervene()
        else:raise AssertionError('unknown fixed guest phase')
    except BaseException as exc:
        persist(ROOT/'first-error.json',{'phase':sys.argv[1],'error_type':type(exc).__name__,
                'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]})
        print('STORAGE_PROOF_FAILED',flush=True)
        if os.getpid()!=1:raise
        os.sync()
        while True:time.sleep(1)
