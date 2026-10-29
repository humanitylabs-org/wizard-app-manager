#!/usr/bin/env python3
"""Bounded disposable-guest metadata acceptance. No host/forum mount operations.
Runs only after the parent admits the exact-source heavy packet.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path('/opt/metadata-security')
SOURCE = Path('/opt/wam-candidate')
OUT = Path('/root/metadata-security.json')
HELPER = '/app/libexec/wizard-bind-metadata'
SUPERVISOR = '/bin/busybox timeout -s KILL 12 "$@"; rc=$?; exit "$rc"'
# The rig must use the production fixed shell text, not a different watchdog.
assert "export const BIND_METADATA_SUPERVISOR = '"+SUPERVISOR+"';" in (SOURCE/'packages/backend/src/modules/docker/bind-helper.ts').read_text()
rows = []
containers = set()
mounts = []
image = Path('/root/new-image-id').read_text().strip()
assert len(image) == 71 and image.startswith('sha256:')
assert os.geteuid() == 0 and Path('/run/wizard-lifecycle-disposable').read_text().startswith('synthetic')

def cmd(*args, timeout=45, check=True):
    p = subprocess.run([str(x) for x in args], text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise AssertionError(f'command failed: {args[0]} rc={p.returncode}; stderr={p.stderr[:400]}')
    return p

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def docker_args(binary=HELPER, *, extra=(), cap=True, nnp=True):
    args = ['docker', 'create', '--name', 'metadata-'+str(len(rows))+'-'+str(time.monotonic_ns()),
            '--label', 'wizard.metadata-security=synthetic', '--network=none', '--read-only', '--restart=no',
            '--stop-timeout=1', '--user=0:0', '--entrypoint=/bin/busybox']
    if cap:
        args += ['--cap-drop=ALL']
    if nnp:
        args += ['--security-opt=no-new-privileges:true']
    args += ['--mount', 'type=bind,source=/,target=/host,readonly']
    args += ['--mount', f'type=bind,source={ROOT}/bin,target=/probe,readonly']
    args += list(extra)
    return args + [image, 'sh', '-c', SUPERVISOR, 'wizard-bind-metadata-supervisor', binary]

def launch(mode, source, binary=HELPER, *, extra=(), cap=True, nnp=True, expected=0):
    cid = cmd(*docker_args(binary, extra=extra, cap=cap, nnp=nnp), mode, source).stdout.strip()
    assert len(cid) == 64 and all(c in '0123456789abcdef' for c in cid)
    containers.add(cid)
    p = cmd('docker', 'start', '--attach', cid, timeout=18, check=False)
    info = json.loads(cmd('docker', 'inspect', '--type', 'container', cid).stdout)[0]
    assert info['Id'] == cid and not info['State']['Running']
    assert info['State']['ExitCode'] == expected, (mode, expected, info['State']['ExitCode'], p.stdout[:400], p.stderr[:400])
    cmd('docker', 'rm', '--force', '--volumes', cid)
    q = cmd('docker', 'inspect', '--type', 'container', cid, check=False)
    assert q.returncode == 1 and q.stderr.strip().lower() in {'error: no such '+k+': '+cid for k in ('container', 'object')}
    containers.remove(cid)
    return p.stdout

def check(name, fn):
    start = time.monotonic()
    try:
        detail = fn()
        rows.append({'case': name, 'passed': True, 'detail': detail, 'seconds': time.monotonic()-start})
    except Exception as error:
        rows.append({'case': name, 'passed': False, 'error': str(error)[:1600]})
        raise
    finally:
        OUT.write_text(json.dumps({'scope':'disposable-amd64-native-self-filter', 'cases':rows, 'passed':all(x['passed'] for x in rows)}, indent=2)+'\n')

def hostile(mode='p', target='/host/opt/metadata-security/nested/sentinel', extra=()):
    before = (ROOT/'nested/sentinel').read_bytes()
    text = launch(mode, target, '/probe/hostile', extra=extra)
    assert text.startswith('post-filter\n') and text.endswith('denied\n'), text[:400]
    calls = [line.split() for line in text.splitlines() if line and line[0].isdigit()]
    assert len(calls) > 100 and all(len(row) == 2 and row[1] == '-1' for row in calls)
    assert (ROOT/'nested/sentinel').read_bytes() == before
    return {'syscall_probes':len(calls), 'errno':'EPERM', 'sha256':hashlib.sha256(before).hexdigest()}

cleanup = []
try:
    assert not ROOT.exists(), 'fresh synthetic mount fixture required'
    (ROOT/'bin').mkdir(parents=True, mode=0o755)
    (ROOT/'gate').mkdir()
    (ROOT/'nested').mkdir()
    (ROOT/'future').mkdir()
    (ROOT/'underlay').mkdir()
    (ROOT/'nested/sentinel').write_bytes(b'underlay\n')
    cmd('mount', '-t', 'tmpfs', '-o', 'mode=0755', 'metadata-nested', ROOT/'nested')
    mounts.append(ROOT/'nested')
    (ROOT/'nested/sentinel').write_bytes(b'existing-mounted\n')
    mounted = (ROOT/'nested/sentinel').stat()
    under = (ROOT/'underlay').stat()
    assert mounted.st_dev != under.st_dev
    # Obtain exact shipped helper + policy without invoking it or its image startup.
    copy = cmd('docker','create','--name','metadata-artifact-copy',image).stdout.strip()
    containers.add(copy)
    cmd('docker','cp',copy+':'+HELPER,ROOT/'bin/production')
    cmd('docker','cp',copy+':'+HELPER+'.policy',ROOT/'bin/production.policy')
    cmd('docker','rm',copy); containers.remove(copy)
    cc = ['cc','-std=c11','-Os','-Wall','-Wextra','-Werror','-static','-nostdlib','-fno-builtin','-fno-stack-protector','-fno-pie','-no-pie','-Wl,--build-id=none,-z,noexecstack']
    probe = SOURCE/'qa/privsep/bind-metadata-probe.c'
    cmd(*cc, probe, '-o', ROOT/'bin/hostile', timeout=120)
    cmd('objcopy','--dump-section',f'.wizard_policy={ROOT}/bin/probe.policy',ROOT/'bin/hostile')
    assert (ROOT/'bin/production.policy').read_bytes() == (ROOT/'bin/probe.policy').read_bytes(), 'not the production policy'
    # Test-only source copies: exactly one audited replacement each; never
    # alter the checked source, build context, shipped image, or production helper.
    original = (SOURCE/'packages/backend/native/bind-metadata.c').read_text()
    install = 'if (SC(seccomp,SECCOMP_SET_MODE_FILTER,0,&program,0,0,0)) refuse();'
    assert original.count(install) == 1
    for kind, text in [('unfiltered', original.replace(install,'(void)program; /* test control only */')),
                       ('mutated', original.replace('BPF_STMT(BPF_RET|BPF_K,DENY)\n};','BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)\n};'))]:
        assert text != original
        local = ROOT/'bin'/(kind+'.c')
        body = probe.read_text().replace('#include "../../packages/backend/native/bind-metadata.c"', '#include "'+str(local)+'"')
        local.write_text(text); unit = ROOT/'bin'/(kind+'-probe.c'); unit.write_text(body)
        cmd(*cc,unit,'-o',ROOT/'bin'/kind,timeout=120)
    for p in (ROOT/'bin').iterdir():
        p.chmod(0o555 if p.name in ('production','hostile','unfiltered','mutated') else 0o444)
    # An actual writable tmpfs exists below the nominal read-only root bind.
    # Its device differs from the underlay, and an unfiltered same-bootstrap
    # control writes it using the very same runtime mount arrangement.
    def control():
        launch('u','/host/opt/metadata-security/nested/sentinel','/probe/unfiltered')
        assert (ROOT/'nested/sentinel').read_bytes() == b'control\n'
        assert (ROOT/'nested/sentinel').stat().st_dev == mounted.st_dev
        return {'mounted_dev':mounted.st_dev,'underlay_dev':under.st_dev,'writable':True}
    check('A-existing-real-writable-submount-positive-control',control)
    check('B-same-production-policy-denies-hostile-syscall-families',hostile)
    check('B-mutated-policy-is-detected',lambda: launch('m','/host/opt/metadata-security/nested/sentinel','/probe/mutated',expected=46))
    check('A-packaged-startup-protected-endpoint',lambda: launch('0',str(ROOT/'nested/sentinel')) == 'safe\n' or (_ for _ in ()).throw(AssertionError('not safe')))
    # An app-owned writable endpoint is permitted; only ancestry is protected.
    os.chown(ROOT/'nested/sentinel',54321,54321); os.chmod(ROOT/'nested/sentinel',0o666)
    before = ((ROOT/'nested/sentinel').read_bytes(), (ROOT/'nested/sentinel').stat())
    check('A-nonroot-endpoint-does-not-change-bytes-or-metadata',lambda: launch('0',str(ROOT/'nested/sentinel')) == 'safe\n' or (_ for _ in ()).throw(AssertionError('not safe')))
    after = (ROOT/'nested/sentinel').stat()
    assert before[0] == (ROOT/'nested/sentinel').read_bytes() and (before[1].st_uid,before[1].st_gid,before[1].st_mode) == (after.st_uid,after.st_gid,after.st_mode)
    (ROOT/'owned').mkdir(mode=0o755); os.chown(ROOT/'owned',54321,54321)
    (ROOT/'owned/endpoint').write_bytes(b'owned\n')
    check('A-approved-manager-UID-ancestor',lambda:launch('54321',str(ROOT/'owned/endpoint')) == 'safe\n' or (_ for _ in ()).throw(AssertionError('not safe')))
    check('A-unapproved-UID-ancestor-refused',lambda:launch('0',str(ROOT/'owned/endpoint'),expected=42))
    def invalid_protocol():
        for uid,source in [('4294967296',str(ROOT/'absent')),('4294967295',str(ROOT/'absent')),('x',str(ROOT/'absent')),('0','/a/../b'),('0','/'+'a'*4090)]:
            assert launch(uid,source,expected=42)==''
        return {'refusals':5}
    check('A-fixed-UID-and-path-protocol-bounds',invalid_protocol)
    check('A-missing-is-ENOENT',lambda: launch('0',str(ROOT/'absent')) == 'missing\n' or (_ for _ in ()).throw(AssertionError('not missing')))
    os.symlink('absent',ROOT/'dangling')
    check('A-dangling-symlink-refused',lambda:launch('0',str(ROOT/'dangling'),expected=42))
    os.symlink('nested',ROOT/'link')
    check('A-symlink-ancestor-refused',lambda:launch('0',str(ROOT/'link/sentinel'),expected=42))
    os.chmod(ROOT/'gate',0o777)
    check('A-writable-ancestor-refused',lambda:launch('0',str(ROOT/'gate/absent'),expected=42))
    os.chmod(ROOT/'gate',0o755)
    check('A-non-directory-error-not-missing',lambda:launch('0',str(ROOT/'nested/sentinel/absent'),expected=42))
    check('A-capability-bootstrap-refuses-before-output',lambda:launch('0',str(ROOT/'absent'),cap=False,expected=42) == '' or (_ for _ in ()).throw(AssertionError('bootstrap output')))
    check('A-NNP-bootstrap-refuses-before-output',lambda:launch('0',str(ROOT/'absent'),nnp=False,expected=42) == '' or (_ for _ in ()).throw(AssertionError('bootstrap output')))
    check('B-x32-ABI-killed',lambda:launch('x','/host/opt/metadata-security/nested/sentinel','/probe/hostile',expected=159))
    check('B-compat-ABI-killed',lambda:launch('i','/host/opt/metadata-security/nested/sentinel','/probe/hostile',expected=159))
    check('B-core-producing-signal-cannot-create-core',lambda:launch('c','/host/opt/metadata-security/nested/sentinel','/probe/hostile',expected=139))
    assert not list(ROOT.rglob('core*'))
    check('A-terminal-fd-bootstrap-refuses-before-output',lambda:launch('0',str(ROOT/'absent'),extra=('--tty',),expected=42) == '' or (_ for _ in ()).throw(AssertionError('bootstrap output')))
    denied = ROOT/'deny-seccomp.json'
    denied.write_text(json.dumps({'defaultAction':'SCMP_ACT_ALLOW','syscalls':[{'names':['seccomp'],'action':'SCMP_ACT_ERRNO','errnoRet':38}]}))
    check('A-unsupported-filter-refuses-before-output',lambda:launch('0',str(ROOT/'absent'),extra=('--security-opt','seccomp='+str(denied)),expected=42) == '' or (_ for _ in ()).throw(AssertionError('bootstrap output')))
    def startup_trace():
        host = Path('/host'); host.mkdir(mode=0o755)
        (host/'trace').write_bytes(b'trace-only\n')
        trace = ROOT/'startup.trace'
        p=cmd('strace','-o',trace,'-e','trace=statx,seccomp,prctl,close_range,capget,prlimit64,fcntl,exit_group,write',
              'setpriv','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all','--no-new-privs',
              ROOT/'bin/production','0','/trace')
        assert p.stdout == 'safe\n'
        text=trace.read_text(); at=text.index('seccomp(SECCOMP_SET_MODE_FILTER')
        end=text.index(' = 0',at)
        assert '"/host' not in text[:end] and '"/host/trace"' in text[end:]
        assert 'close_range(0, 0, 0)' in text[:at] and 'close_range(3, 4294967295, 0)' in text[:at]
        assert text.count('seccomp(SECCOMP_SET_MODE_FILTER') == 1
        (host/'trace').unlink(); host.rmdir()
        return {'trace_sha256':digest(trace),'selected_path_first_seen_after_filter':True,'binary_sha256':digest(ROOT/'bin/production')}
    check('A-exact-shipped-static-binary-startup-trace',startup_trace)
    def future():
        # Only THIS fresh synthetic directory becomes shared. Never /, daemon
        # mount config, host/forum namespaces, or an existing user's mount.
        cmd('mount','--bind',ROOT,ROOT); mounts.append(ROOT)
        cmd('mount','--make-rshared',ROOT)
        (ROOT/'future/sentinel').write_bytes(b'future-underlay\n')
        old=(ROOT/'future/sentinel').stat()
        extra=('--mount',f'type=bind,source={ROOT},target=/host/opt/metadata-security,readonly,bind-propagation=rslave')
        target='/host/opt/metadata-security/future/sentinel'
        cid=cmd(*docker_args('/probe/hostile',extra=extra),'b',target).stdout.strip(); containers.add(cid)
        cmd('docker','start',cid)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            logs=cmd('docker','logs',cid,timeout=2).stdout
            if 'barrier\n' in logs: break
            time.sleep(0.05)
        else: raise AssertionError('post-filter barrier missing')
        # Exact packaged BusyBox supervisor remains PID1; the same-bootstrap
        # filtered body is its non-init child, killable by timeout's watcher.
        init = cmd('docker','exec',cid,'/bin/busybox','cat','/proc/1/cmdline').stdout.split('\0')[:-1]
        assert init==['/bin/busybox','sh','-c',SUPERVISOR,'wizard-bind-metadata-supervisor','/probe/hostile','b',target],init
        # comm is kernel exec identity, not a read of the nondumpable body's
        # address space (its /proc/PID/cmdline may legitimately be inaccessible).
        process_rows = [line.split() for line in cmd('docker','exec',cid,'/bin/busybox','ps','-o','pid,ppid,comm').stdout.splitlines()[1:]]
        body = [r for r in process_rows if len(r)==3 and int(r[0])>1 and r[1]=='1' and r[2]=='hostile']
        assert len(body)==1,process_rows
        cmd('mount','-t','tmpfs','-o','mode=0755','metadata-future',ROOT/'future'); mounts.append(ROOT/'future')
        (ROOT/'future/sentinel').write_bytes(b'future-mounted\n')
        now=(ROOT/'future/sentinel').stat()
        assert now.st_dev != old.st_dev and (now.st_dev,now.st_ino) != (old.st_dev,old.st_ino)
        (ROOT/'gate/release').touch()
        p=cmd('docker','wait',cid,timeout=15); assert p.stdout.strip() == '0'
        logs=cmd('docker','logs',cid).stdout
        met=[tuple(map(int,line.split()[1:])) for line in logs.splitlines() if line.startswith('M ')]
        assert met == [(os.major(old.st_dev),os.minor(old.st_dev),old.st_ino),(os.major(now.st_dev),os.minor(now.st_dev),now.st_ino)],met
        denied=[line.split() for line in logs.splitlines() if line and line[0].isdigit()]
        assert len(denied)>100 and all(row[1]=='-1' for row in denied) and logs.endswith('denied\n')
        assert (ROOT/'future/sentinel').read_bytes() == b'future-mounted\n'
        # Unfiltered control under the same synthetic propagation/mount setup.
        launch('u',target,'/probe/unfiltered',extra=extra)
        assert (ROOT/'future/sentinel').read_bytes()==b'control\n' and (ROOT/'future/sentinel').stat().st_dev==now.st_dev
        cmd('docker','rm','--force','--volumes',cid)
        gone=cmd('docker','inspect','--type','container',cid,check=False)
        assert gone.returncode==1 and gone.stderr.strip().lower() in {'error: no such '+k+': '+cid for k in ('container','object')}
        containers.remove(cid)
        return {'before':met[0],'after':met[1],'postfilter_barrier':True,'unfiltered_control_wrote_actual_mount':True,
                'supervisor_pid':1,'postfilter_body_pid':int(body[0][0]),'postfilter_body_parent_pid':1,
                'scope':'synthetic shared/slave child only; private root fallback does not promise future visibility'}
    check('C-future-propagated-actual-writable-mount',future)
finally:
    for cid in tuple(containers):
        try:
            cmd('docker','rm','--force','--volumes',cid)
            q=cmd('docker','inspect','--type','container',cid,check=False)
            assert q.returncode == 1 and q.stderr.strip().lower() in {'error: no such '+k+': '+cid for k in ('container','object')}
        except Exception as e:
            cleanup.append(type(e).__name__)
    for mount in reversed(mounts):
        try:
            cmd('umount',mount)
        except Exception as e:
            cleanup.append(type(e).__name__)
    OUT.write_text(json.dumps({'scope':'disposable-amd64-native-self-filter','cases':rows,'cleanup_errors':cleanup,
        'passed':bool(rows) and all(r['passed'] for r in rows) and not cleanup,
        'image':image,'source':str(SOURCE)},indent=2)+'\n')
    if cleanup:
        raise RuntimeError('Metadata security cleanup incomplete; halt all later activity')
