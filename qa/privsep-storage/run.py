#!/usr/bin/env python3
"""Parent-admitted forum/KVM storage-only acceptance; --preflight is read-only.

No backend/image rebuild, Docker, host systemd/login/cron/mount mutation, or real
home access. All guest disks are new overlays. All QEMU processes are reaped in
finally. Source stages/evidence/previous images and accepted disks are retained.
"""
import argparse
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback

RESERVE = 217179869184
PENDING = 21474836480
BASE = Path('/srv/work/images/debian-12-genericcloud-amd64.qcow2')
BASE_SHA = '24036a8b79cbb11d809752c60f2f885b1a3b02b1d663faa0b648eb87bd7a256e'
LOCK = Path('/srv/work/heavy/production/worker.lock')


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def put_json(path, value):
    with path.open('w') as stream:
        json.dump(value,stream,sort_keys=True,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())


def inventory(path):
    # Tar bytes are immutable exact git-archive inputs, not a fabricated repo.
    import tarfile
    rows=[]
    with tarfile.open(path) as archive:
        for member in archive:
            if member.isdir():continue
            assert not member.name.startswith('/') and '..' not in Path(member.name).parts
            assert member.isfile() or member.issym()
            data=archive.extractfile(member).read() if member.isfile() else os.fsencode(member.linkname)
            rows.append({'path':member.name,'kind':'file' if member.isfile() else 'symlink',
                         'mode':member.mode,'sha256':hashlib.sha256(data).hexdigest()})
    return sorted(rows,key=lambda row:row['path'])


def preflight(stage):
    doc=json.loads((stage/'manifest.json').read_text())
    assert re.fullmatch('[0-9a-f]{40}',doc['revision']) and stage.name==doc['revision']
    assert str(stage.parent)=='/srv/work/privsep/storage'
    assert doc['reserve_bytes']==RESERVE and doc['pending_bytes']==PENDING
    for name,value in doc['files'].items():
        assert '/' not in name and not (stage/name).is_symlink() and digest(stage/name)==value
    assert inventory(stage/'source.tar')==json.loads((stage/'source-members.json').read_text())
    rows=json.loads((stage/'source-members.json').read_text())
    for name,key in [('qa/privsep-storage/run.py','runner.py'),('qa/privsep-storage/guest.py','guest.py')]:
        assert next(row['sha256'] for row in rows if row['path']==name)==digest(stage/key)
    # Parse—not import—the staged guest and exact expected runtime case identities.
    tree=ast.parse((stage/'guest.py').read_text());scope={}
    for node in tree.body:
        if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('NORMAL','INIT','BARRIERS'):
            scope[node.targets[0].id]=ast.literal_eval(node.value)
    scope['EXPECTED']=scope['NORMAL']+scope['INIT']+tuple(f'{fs}-crash-{phase}' for fs in ('samefs','crossfs') for phase in scope['BARRIERS'])
    assert list(scope['EXPECTED'])==doc['expected_cases']
    assert len(scope['EXPECTED'])==len(set(scope['EXPECTED']))
    assert BASE.is_file() and digest(BASE)==BASE_SHA
    free=shutil.disk_usage(stage).free
    result={'static_integrity':True,'revision':doc['revision'],'cases_expected':len(scope['EXPECTED']),
            'free_bytes':free,'reserve_bytes':RESERVE,'pending_bytes':PENDING,
            'required_bytes':RESERVE+PENDING,'capacity_admissible':free>=RESERVE+PENDING,
            'shortfall_bytes':max(0,RESERVE+PENDING-free),'heavy_executed':False,
            'scope':'static only; no runtime writer-exclusion/relocation claim'}
    for exe in ('qemu-system-x86_64','qemu-img','cloud-localds','ssh','scp','git'):
        assert shutil.which(exe),exe
    assert os.access('/dev/kvm',os.R_OK|os.W_OK)
    # Source/revision provenance is genuine Git metadata; the worker reconstructs
    # it before any tests. The bundle ref is bound to the requested branch/head.
    heads=subprocess.check_output(['git','bundle','list-heads',str(stage/'source.bundle')],text=True,timeout=30)
    assert doc['revision']+' refs/heads/'+doc['branch'] in heads
    return doc,result


def native_lock():
    raw=os.environ.get('WIZARD_HEAVY_LOCK_FD');assert raw is not None and raw.isdecimal(),'native parent admission required'
    fd=int(raw);a=os.fstat(fd);b=LOCK.stat()
    assert (a.st_dev,a.st_ino)==(b.st_dev,b.st_ino),'native worker lock inode mismatch'
    # Must be a live exclusive worker lease, not merely an open pathname.
    probe=os.open(LOCK,os.O_RDWR|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:raise AssertionError('native worker lock is not held')
    finally:os.close(probe)


class Runner:
    def __init__(self,stage,doc):
        self.stage=stage;self.doc=doc;self.evidence=stage/'evidence'
        self.evidence.mkdir(mode=0o700)
        self.receipt={'revision':doc['revision'],'runner_sha256':digest(stage/'runner.py'),
            'passed':False,'full_integration_acceptance':False,'steps':[],'boots':[],
            'first_error':None,'cleanup_errors':[],'source_binding':'genuine pushed commit + complete git archive',
            'scope':'protected storage primitive only; stock gateway, installed Service/HTTP/install follow separately'}
        self.proc=None;self.log=None
        self.key=self.evidence/'id_ed25519';self.disk=self.evidence/'disk.qcow2'
        self.secondary=self.evidence/'secondary.qcow2';self.serial=None
        self.port=None
        self.save()

    def save(self):put_json(self.evidence/'receipt.json',self.receipt)

    def capacity(self):
        free=shutil.disk_usage(self.stage).free
        self.receipt['latest_capacity']={'free_bytes':free,'required_bytes':RESERVE+PENDING}
        self.save();assert free>=RESERVE+PENDING,'protected reserve + pending footprint not available'

    def run(self,label,argv,*,timeout=180,input=None,required=True):
        path=self.evidence/(label+'.log')
        with path.open('wb') as log:
            try:
                result=subprocess.run([str(v) for v in argv],input=input,stdout=log,stderr=subprocess.STDOUT,
                                      timeout=timeout,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
                rc=result.returncode
            except subprocess.TimeoutExpired:rc=124
        self.receipt['steps'].append({'name':label,'exit':rc,'sha256':digest(path),'log':path.name});self.save()
        if required:assert rc==0,'fixed phase failed: '+label
        return rc

    @property
    def ssh(self):return ['ssh','-i',str(self.key),'-p',str(self.port),'-o','BatchMode=yes',
        '-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null','-o','LogLevel=ERROR',
        '-o','ConnectTimeout=5','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','root@127.0.0.1']

    def guest(self,label,script,timeout=180,required=True):
        (self.evidence/(label+'.sh')).write_text(script)
        return self.run(label,self.ssh+['bash','-se'],timeout=timeout,input=('set -o pipefail\n'+script+'\n').encode(),required=required)

    def copy(self,local,remote,download=False):
        opts=['scp','-q','-i',str(self.key),'-P',str(self.port),'-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null']
        target='root@127.0.0.1:'+remote
        return self.run('copy-'+str(len(self.receipt['steps'])),opts+([target,str(local)] if download else [str(local),target]),timeout=180)

    def start(self,index,maintenance=False,boot=None):
        self.capacity()
        with socket.socket() as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.serial=self.evidence/f'boot-{index}-serial.log'
        argv=['qemu-system-x86_64','-accel','kvm','-machine','q35','-cpu','host','-smp','2','-m','3072',
              '-display','none','-serial','file:'+str(self.serial),'-no-reboot',
              '-drive',f'file={self.disk},if=virtio,format=qcow2',
              '-drive',f'file={self.secondary},if=virtio,format=qcow2',
              '-drive',f'file={self.evidence/"seed.iso"},if=virtio,format=raw,readonly=on']
        if maintenance:
            argv+=['-kernel',str(self.evidence/'vmlinuz'),'-initrd',str(self.evidence/'initrd'),
                   '-append','root=UUID='+boot['root_uuid']+' rw console=ttyS0 init=/root/storage-proof/init.sh']
            # No network interface in maintenance, not even a guest SSH server.
            argv+=['-nic','none']
        else:argv+=['-netdev',f'user,id=n0,hostfwd=tcp:127.0.0.1:{self.port}-:22','-device','virtio-net-pci,netdev=n0']
        self.log=(self.evidence/f'boot-{index}-qemu.log').open('wb')
        self.proc=subprocess.Popen(argv,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        self.receipt['boots'].append({'index':index,'pid':self.proc.pid,'maintenance':maintenance,'stopped':False,'power_cut':False})
        self.save()
        if not maintenance:
            end=time.monotonic()+420
            while time.monotonic()<end:
                assert self.proc.poll() is None,'guest exited before readiness'
                p=subprocess.run(self.ssh+['true'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                if p.returncode==0:return
                time.sleep(2)
            raise TimeoutError('guest SSH readiness')

    def stop(self,*,power_cut=False):
        if self.proc is None:return
        row=self.receipt['boots'][-1]
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL if power_cut else signal.SIGTERM)
            try:self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=30)
        row.update(stopped=self.proc.poll() is not None,power_cut=power_cut,exit=self.proc.returncode)
        if self.log:self.log.close()
        self.proc=None;self.log=None;self.save()

    def execute(self):
        self.capacity()
        self.run('key',['ssh-keygen','-q','-t','ed25519','-N','','-f',self.key])
        self.run('overlay',['qemu-img','create','-f','qcow2','-F','qcow2','-b',BASE,self.disk,'14G'])
        self.run('secondary',['qemu-img','create','-f','qcow2',self.secondary,'4G'])
        key=self.key.with_suffix('.pub').read_text().strip()
        (self.evidence/'user-data').write_text('#cloud-config\nusers:\n  - name: root\n    lock_passwd: true\n    ssh_authorized_keys:\n      - '+key+'\ndisable_root: false\nssh_pwauth: false\nwrite_files:\n  - path: /etc/ssh/sshd_config.d/99-storage-proof.conf\n    content: |\n      PermitRootLogin prohibit-password\n      PasswordAuthentication no\n')
        (self.evidence/'meta-data').write_text('instance-id: storage-'+self.doc['revision'][:12]+'\nlocal-hostname: storage-proof\n')
        self.run('seed',['cloud-localds',self.evidence/'seed.iso',self.evidence/'user-data',self.evidence/'meta-data'])
        self.start(0)
        self.guest('prerequisites','cloud-init status --wait\napt-get -o DPkg::Lock::Timeout=300 update -qq\nDEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -qq acl e2fsprogs python3-pytest python3-yaml git util-linux\ninstall -d -m700 /root/storage-proof /root/storage-proof/source\n',900)
        for name in ('source.tar','source.bundle','shallow','guest.py'):
            self.copy(self.stage/name,'/root/storage-proof/'+name)
        self.guest('exact-source',
            'umask 022\ntar --no-same-permissions -xf /root/storage-proof/source.tar -C /root/storage-proof/source\n'
            'mkdir -m700 /root/storage-proof/template\n'
            'if [ -s /root/storage-proof/shallow ]; then cp /root/storage-proof/shallow /root/storage-proof/template/shallow; fi\n'
            'git clone --template=/root/storage-proof/template /root/storage-proof/source.bundle /root/storage-proof/genuine\n'
            'mv /root/storage-proof/genuine/.git /root/storage-proof/source/.git\n'
            'cd /root/storage-proof/source\n'
            'test "$(git rev-parse HEAD)" = '+self.doc['revision']+'\n'
            'test "$(git rev-parse HEAD^{tree})" = '+self.doc['tree']+'\n'
            'git fsck --full\ntest -z "$(git status --porcelain)"\n',180)
        self.guest('focused-tests','cd /root/storage-proof/source\nPYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m pytest -q tests/test_agent_storage.py tests/test_privsep_alongside.py tests/test_privilege_migration.py tests/test_get_sh_any_user.py --junitxml=/root/storage-proof/pytest.xml\n',600)
        self.copy(self.evidence/'pytest.xml','/root/storage-proof/pytest.xml',True)
        self.guest('normal-refusal-and-controls','/usr/bin/python3 -I /root/storage-proof/guest.py prepare',360)
        self.copy(self.evidence/'normal-cases.json','/root/storage-proof/cases.json',True)
        self.guest('boot-metadata',"python3 -c 'import json,os,subprocess; from pathlib import Path; k=os.uname().release; print(json.dumps({\"kernel\":\"/boot/vmlinuz-\"+k,\"initrd\":\"/boot/initrd.img-\"+k,\"root_uuid\":subprocess.check_output([\"findmnt\",\"-no\",\"UUID\",\"/\"],text=True).strip()}))' > /root/storage-proof/boot.json")
        self.copy(self.evidence/'boot.json','/root/storage-proof/boot.json',True)
        boot=json.loads((self.evidence/'boot.json').read_text());assert re.fullmatch('[A-Fa-f0-9-]+',boot['root_uuid'])
        self.copy(self.evidence/'vmlinuz',boot['kernel'],True);self.copy(self.evidence/'initrd',boot['initrd'],True)
        self.receipt['boot_inputs']={k:digest(self.evidence/k) for k in ('vmlinuz','initrd')};self.save()
        self.guest('shutdown-initial','sync\nsystemctl poweroff\n',60,False)
        try:self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:raise AssertionError('initial guest did not shut down')
        self.stop()
        seen=[]
        for index in range(1,16):
            self.start(index,True,boot)
            deadline=time.monotonic()+360
            completed=False
            while time.monotonic()<deadline:
                text=self.serial.read_text(errors='replace') if self.serial.exists() else ''
                assert 'STORAGE_PROOF_FAILED' not in text,'guest proof failed; preserve first error on readback'
                barriers=re.findall(r'STORAGE_CRASH_BARRIER:([a-z0-9_-]+)',text)
                if barriers:
                    assert len(barriers)==1 and barriers[0] not in seen,'unexpected/repeated crash barrier'
                    seen.append(barriers[0]);self.stop(power_cut=True);break
                if 'STORAGE_PROOF_COMPLETE' in text:
                    completed=True
                    self.proc.wait(timeout=60);self.stop();break
                assert self.proc.poll() is None,'maintenance guest exited before result'
                time.sleep(0.25)
            else:raise TimeoutError('bounded maintenance boot')
            if completed:break
        else:raise AssertionError('too many recovery boots')
        expected_crashes=[n for n in self.doc['expected_cases'] if '-crash-' in n]
        assert set(seen)==set(expected_crashes) and len(seen)==len(expected_crashes)
        self.receipt['actual_power_cuts']=seen;self.save()
        self.start(16)
        for name in ('cases.json','complete.json'):
            self.copy(self.evidence/name,'/root/storage-proof/'+name,True)
        rows=json.loads((self.evidence/'cases.json').read_text());names=[r['name'] for r in rows]
        assert len(names)==len(set(names))==len(self.doc['expected_cases'])
        assert set(names)==set(self.doc['expected_cases']) and all(r['passed'] is True for r in rows)
        complete=json.loads((self.evidence/'complete.json').read_text());assert complete['passed'] is True
        import xml.etree.ElementTree as ET
        tree=ET.parse(self.evidence/'pytest.xml');cases=tree.findall('.//testcase')
        assert cases and not tree.findall('.//failure') and not tree.findall('.//error') and not tree.findall('.//skipped')
        self.receipt.update(passed=True,actual_case_count=len(rows),python_cases=len(cases),runtime_acceptance='storage seam only')
        self.guest('shutdown-final','sync\nsystemctl poweroff\n',60,False)
        self.proc.wait(timeout=60);self.stop()

    def diagnostics(self):
        # Read actual guest state after an init failure by booting the same owned
        # disk normally. It does not rerun/relabel the failing proof.
        if not self.disk.exists():return
        self.stop()
        try:
            self.start(99)
            for name in ('cases.json','first-error.json','crash-state.json'):
                try:self.copy(self.evidence/('failure-'+name),'/root/storage-proof/'+name,True)
                except Exception as exc:self.receipt['cleanup_errors'].append('diagnostic '+name+':'+type(exc).__name__)
        except Exception as exc:self.receipt['cleanup_errors'].append('diagnostic-boot:'+type(exc).__name__)
        finally:self.stop()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--preflight',action='store_true');parser.add_argument('--execute',action='store_true')
    args=parser.parse_args();assert args.preflight != args.execute
    stage=args.stage.absolute();doc,result=preflight(stage)
    if args.preflight:print(json.dumps(result,sort_keys=True));return 0
    native_lock();assert result['capacity_admissible'],'protected reserve + pending footprint unavailable'
    temp=stage/'tmp';temp.mkdir(mode=0o700)
    os.environ['TMPDIR']=str(temp);os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.umask(0o077)
    runner=Runner(stage,doc)
    def interrupt(sig,frame):raise TimeoutError('bounded native signal '+str(sig))
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):signal.signal(sig,interrupt)
    signal.alarm(7000)
    try:runner.execute()
    except BaseException as exc:
        runner.receipt['first_error']={'type':type(exc).__name__,'reason':str(exc) if isinstance(exc,(AssertionError,TimeoutError)) else 'fixed phase exception',
            'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]}
        runner.receipt['passed']=False;runner.save();runner.diagnostics()
    finally:
        try:runner.stop()
        except BaseException as exc:runner.receipt['cleanup_errors'].append(type(exc).__name__)
        runner.receipt['all_owned_vms_stopped']=all(row['stopped'] for row in runner.receipt['boots'])
        runner.receipt['passed']=runner.receipt['passed'] and runner.receipt['all_owned_vms_stopped'] and not runner.receipt['cleanup_errors']
        runner.save()
    print(json.dumps(runner.receipt,sort_keys=True));return 0 if runner.receipt['passed'] else 1


if __name__=='__main__':sys.exit(main())
