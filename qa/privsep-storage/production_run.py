#!/usr/bin/python3
"""Parent-admitted production vertical slice; static --preflight never executes.

Extends the accepted storage runner's VM/process/phase collection. All boot,
Docker, install and test actions occur only inside its disposable synthetic VM.
No original proof disks/images/evidence are changed or deleted. No host Docker.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
import xml.etree.ElementTree as ET

RESERVE = 217179869184
PENDING = 68719476736  # one 56GiB guest + 4GiB spare + packaging/stage/log peak
BOOT = '/usr/local/lib/wizard-storage/maintenance-init'
GUEST = '/usr/local/lib/wizard-storage/production-guest.py'
STOCK = '5938f04aacee521c07e6e896f68b9e33f33a3697'
BACKEND = 'sha256:1c10570477becada8c613a55ff402f12b49d54e74fd9e16fefba678d57ead426'
BACKEND_ARCHIVE = Path('/srv/work/privsep/integrated/20261029T161239Z-595523fb-context-proof/evidence/new-backend-image.tar.gz')
BACKEND_SHA = 'a953c4ce3590bc1448bacb0b5b259334a99a608fb0377af425daf87c888d49c0'
COLD_NEGATIVE = ('cold-invalid-json', 'cold-wrong-field', 'cold-wrong-type', 'cold-invalid-action',
                 'cold-symlink-request', 'cold-writable-request', 'cold-readonly-root', 'cold-foreign-writer')
CASES = ('ordinary-and-real-nested-entry-refused',) + COLD_NEGATIVE + (
    'cold-production-entry-real-request', 'normal-boot-stock-gateway-original-environment',
    'retired-request-does-not-reapply', 'installed-compiled-backend-physical-service-consumer',
    'stale-and-unavailable-mapping-before-update-restart')
MODULES = ('tests/test_storage_production_integration.py','tests/test_agent_storage.py',
           'tests/test_privsep_alongside.py','tests/test_privilege_migration.py',
           'tests/test_get_sh_any_user.py','tests/test_initial_pid_namespace.py','tests/test_migration_paths.py')


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def load_old(stage):
    spec=importlib.util.spec_from_file_location('accepted_storage_runner',stage/'storage-runner.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    # Only configuration of this isolated invocation, not editing the old file.
    module.PENDING=PENDING;module.PYTEST_MODULES=MODULES
    return module


# Audited effective inherited methods: ProductionRunner replaces execute,
# capacity and diagnostics. In particular, base __init__ still consumes product
# identity and focused_tests still consumes the complete pytest inventory.
RUNNER_FIELDS = ('revision', 'product_sha256', 'service_revision',
                 'expected_pytest_count', 'expected_pytest_cases')
PRODUCT_PATH = 'scripts/protected-agent-storage.py'


class ManifestContractError(ValueError):
    """Fixed metadata-only refusal, safe to persist before any VM exists."""


def runner_manifest_reads(base_source, production_source):
    """Audit doc[...] reads in every effective method, plus super().__init__."""
    inherited = next(n for n in ast.parse(base_source).body
                     if isinstance(n, ast.ClassDef) and n.name == 'Runner')
    derived = next(n for n in ast.walk(ast.parse(production_source))
                   if isinstance(n, ast.ClassDef) and n.name == 'ProductionRunner')
    methods = {n.name: ('base', n) for n in inherited.body if isinstance(n, ast.FunctionDef)}
    methods.update({n.name: ('production', n) for n in derived.body if isinstance(n, ast.FunctionDef)})
    selected = list(methods.values()) + [('base', next(n for n in inherited.body
                       if isinstance(n, ast.FunctionDef) and n.name == '__init__'))]
    reads = {}
    for owner, method in selected:
        keys = set()
        for node in ast.walk(method):
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) in ('doc', 'self.doc'):
                if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
                    raise ManifestContractError('dynamic runner manifest read requires explicit review')
                keys.add(node.slice.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) in ('doc', 'self.doc'):
                raise ManifestContractError('new runner manifest access requires explicit review')
        if keys: reads[owner + '.' + method.name] = sorted(keys)
    return reads


def validate_runner_contract(stage, doc):
    missing = sorted(set(RUNNER_FIELDS + ('product_path', 'source_members')) - set(doc))
    if missing:
        raise ManifestContractError('missing runner manifest fields: ' + ','.join(missing))
    for field in ('revision', 'service_revision'):
        if not isinstance(doc[field], str) or not re.fullmatch('[0-9a-f]{40}', doc[field]):
            raise ManifestContractError('invalid runner revision: ' + field)
    if doc['product_path'] != PRODUCT_PATH:
        raise ManifestContractError('invalid product source path')
    if not isinstance(doc['product_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', doc['product_sha256']):
        raise ManifestContractError('invalid product SHA256')
    cases = doc['expected_pytest_cases']
    if (not isinstance(cases, list) or not cases or not all(isinstance(v, str) for v in cases)
            or len(cases) != len(set(cases)) or type(doc['expected_pytest_count']) is not int
            or doc['expected_pytest_count'] != len(cases)):
        raise ManifestContractError('invalid exact pytest inventory')
    if type(doc['source_members']) is not int or doc['source_members'] < 1:
        raise ManifestContractError('invalid source member count')
    reads = runner_manifest_reads((stage/'storage-runner.py').read_text(), (stage/'runner.py').read_text())
    if set().union(*(set(v) for v in reads.values())) != set(RUNNER_FIELDS):
        raise ManifestContractError('effective inherited runner fields changed; review contract')
    rows = json.loads((stage/'source-members.json').read_text())
    if len(rows) != doc['source_members'] or len({r['path'] for r in rows}) != len(rows):
        raise ManifestContractError('source member inventory mismatch')
    product = [r for r in rows if r['path'] == PRODUCT_PATH]
    if len(product) != 1 or product[0]['kind'] != 'file' or product[0]['sha256'] != doc['product_sha256']:
        raise ManifestContractError('product SHA256 differs from exact source inventory')
    with tarfile.open(stage/'source.tar') as archive:
        members = [m for m in archive if m.name == PRODUCT_PATH]
        stream = archive.extractfile(members[0]) if len(members) == 1 and members[0].isfile() else None
        if (len(members) != 1 or not members[0].isfile() or members[0].mode != product[0]['mode']
                or stream is None or hashlib.sha256(stream.read()).hexdigest() != doc['product_sha256']):
            raise ManifestContractError('product SHA256 differs from archived source')
    return reads


def validate(stage, manifest_hash, *, local=False):
    assert re.fullmatch('[0-9a-f]{64}',manifest_hash)
    assert digest(stage/'manifest.json')==manifest_hash
    doc=json.loads((stage/'manifest.json').read_text())
    contract = validate_runner_contract(stage, doc)
    assert doc['schema']=='storage-production-v1' and doc['reserve_bytes']==RESERVE and doc['pending_bytes']==PENDING
    assert doc['expected_cases']==list(CASES) and len(CASES)==len(set(CASES))
    assert doc['expected_pytest_count']==len(doc['expected_pytest_cases'])
    assert re.fullmatch('[0-9a-f]{40}',doc['revision']) and re.fullmatch('[0-9a-f]{40}',doc['service_revision'])
    if not local:
        assert stage==Path('/srv/work/privsep/storage-production')/doc['revision']
    assert stage.stat().st_uid==os.getuid() and not stage.stat().st_mode & 0o022
    expected_files={'runner.py','guest.py','storage-runner.py','source.tar','source.bundle','shallow',
        'source-members.json','service.tar','service-members.json','stock-fixture.py','inherited-product.py',
        'backend-inputs.json','backend-context.json','inherited-storage-receipt.json',
        'prior-install-receipt.json','static-checks.json'}
    assert set(doc['files'])==expected_files
    for name,sha in doc['files'].items():
        assert '/' not in name and not (stage/name).is_symlink() and digest(stage/name)==sha
        st=(stage/name).stat();assert st.st_uid==os.getuid() and not st.st_mode & 0o022
    assert digest(__file__)==doc['files']['runner.py']
    old=load_old(stage)
    assert old.inventory(stage/'source.tar')==json.loads((stage/'source-members.json').read_text())
    assert old.inventory(stage/'service.tar')==json.loads((stage/'service-members.json').read_text())
    with tarfile.open(stage/'source.tar') as archive:
        sources={name:archive.extractfile(name).read().decode() for name in (*MODULES,'wam/privilege_migration.py')}
        for name,standalone in [('qa/privsep-storage/production_run.py','runner.py'),
                                ('qa/privsep-storage/production_guest.py','guest.py'),
                                ('qa/privsep-storage/run.py','storage-runner.py'),
                                ('qa/privsep-slice3/run.py','stock-fixture.py')]:
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest()==doc['files'][standalone]
        after=ast.parse(archive.extractfile('scripts/protected-agent-storage.py').read())
        # Complete backend input closure is inherited only when every tracked
        # image input from the accepted immutable 595523fb archive is unchanged.
        by={r['path']:r for r in old.inventory(stage/'source.tar')}
        closure=json.loads((stage/'backend-inputs.json').read_text())
        assert len(closure)==doc['backend_input_count']==622
        assert all(by[r['path']]==r for r in closure)
    assert old.pytest_inventory(sources)==doc['expected_pytest_cases']
    before=ast.parse((stage/'inherited-product.py').read_text())
    def nodes(tree): return {n.name:ast.dump(n,include_attributes=False) for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    a,b=nodes(before),nodes(after)
    assert set(b)==set(a)|{'wait_exited_child','reap_exited_children'}
    assert {n for n in a if a[n]!=b[n]}=={'main','require_fence'}
    old_fence=next(n for n in before.body if isinstance(n,ast.FunctionDef) and n.name=='require_fence')
    new_fence=next(n for n in after.body if isinstance(n,ast.FunctionDef) and n.name=='require_fence')
    calls=[n for n in new_fence.body if isinstance(n,ast.Expr) and ast.unparse(n)=='reap_exited_children()']
    assert len(calls)==1
    new_fence.body.remove(calls[0])
    assert ast.dump(old_fence,include_attributes=False)==ast.dump(new_fence,include_attributes=False)
    assert digest(stage/'inherited-product.py')=='3ac25e1b2cbd79e452b3161920571ce2fd83b8d4084f363ead54a614812c47a7'
    assert digest(stage/'inherited-storage-receipt.json')=='bd498441bbbe7b156bd11a50cfbe852c3c1f8a5a6d6d85ea2fe3d9ef4754cc35'
    # No execution/collection/import of tests or guest actions in preflight.
    for name in ('runner.py','guest.py','storage-runner.py'):
        ast.parse((stage/name).read_text(),filename=name)
    heads=subprocess.check_output(['git','bundle','list-heads',str(stage/'source.bundle')],text=True,timeout=30)
    assert doc['revision']+' refs/heads/'+doc['branch'] in heads
    if not local:
        assert old.digest(old.BASE)==old.BASE_SHA and digest(BACKEND_ARCHIVE)==BACKEND_SHA
        assert os.access('/dev/kvm',os.R_OK|os.W_OK)
        for executable in ('qemu-system-x86_64','qemu-img','cloud-localds','ssh','scp','git','ssh-keygen'):
            assert shutil.which(executable),executable
    free=shutil.disk_usage(stage).free
    return doc,old,{'static_source_integrity':True,'primitive_ast_delta':'main plus bounded reaper call; existing fence AST otherwise exact',
        'effective_runner_manifest_reads':contract,'product_sha256':doc['product_sha256'],
        'expected_runtime_cases':len(CASES),'expected_python_cases':len(doc['expected_pytest_cases']),
        'heavy_executed':False,'local_only':local,'forum_artifacts_checked':not local,
        'free_bytes':free,'required_bytes':RESERVE+PENDING,'capacity_admissible':None if local else free>=RESERVE+PENDING}


def make_runner(base):
    class ProductionRunner(base.Runner):
        def __init__(self,stage,doc):
            # Shared with static preflight; refuse BEFORE base creates evidence.
            validate_runner_contract(stage,doc)
            super().__init__(stage,doc)
            self.receipt.update(scope='real packaged cold maintenance + physical Service vertical slice only',
                full_integration_acceptance=False,actual_cases=[],service_source_revision=doc['service_revision'])
            self.boot_index=0;self.save()

        def mark(self,name,fn):
            assert name in CASES and not any(r['name']==name for r in self.receipt['actual_cases'])
            row={'name':name,'passed':False}
            try: fn();row['passed']=True
            except BaseException as exc: row['error_type']=type(exc).__name__;raise
            finally:
                self.receipt['actual_cases'].append(row);self.save();self.write_junit()

        def capacity(self):
            # Free already excludes our current allocations. Keep the reserve
            # fixed and budget only the unallocated remainder of the SAME peak.
            allocated=sum(p.stat().st_blocks*512 for p in self.evidence.rglob('*') if p.is_file() and not p.is_symlink())
            pending=max(0,PENDING-allocated)
            free=shutil.disk_usage(self.stage).free
            self.receipt['last_capacity']={'free_bytes':free,'reserve_bytes':RESERVE,
                'owned_allocated_bytes':allocated,'remaining_pending_bytes':pending}
            assert free>=RESERVE+pending,'floor plus remaining bounded guest peak unavailable'

        def write_junit(self):
            suite=ET.Element('testsuite',name='production-vertical-slice',tests=str(len(self.receipt['actual_cases'])))
            for row in self.receipt['actual_cases']:
                case=ET.SubElement(suite,'testcase',classname='production',name=row['name'])
                if not row['passed']: ET.SubElement(case,'failure',message=row.get('error_type','failed'))
            ET.ElementTree(suite).write(self.evidence/'vertical.xml',encoding='utf-8',xml_declaration=True)

        def start_normal(self):
            self.boot_index+=1;self.start(self.boot_index)
            self.guest('marker-'+str(self.boot_index),'touch /run/wizard-storage-production-synthetic')

        def shutdown(self):
            self.guest('shutdown-'+str(self.boot_index),'sync\nsystemctl poweroff',60,False)
            assert self.proc is not None
            self.proc.wait(timeout=90);self.stop()

        def collect_json(self,name,*,required=True):
            destination=self.evidence/(str(self.boot_index)+'-'+name+'.json')
            remote='/root/storage-production/'+name+'.json'
            rc=self.copy(destination,remote,True,required=False)
            row={'artifact_name':name,'boot':self.boot_index,'copy_exit':rc}
            if rc==0:
                try:
                    value=json.loads(destination.read_text());row.update(status='collected',sha256=digest(destination))
                except Exception as exc:
                    row.update(status='collection_error',error_type=type(exc).__name__)
                    self.checkpoint('guest-readback-'+name,**row);raise
                self.checkpoint('guest-readback-'+name,**row);return value
            # SCP alone cannot distinguish an optional unproduced report from
            # transport failure. Probe only after a failed copy, as in the base
            # collector; unexpected copy/probe errors remain disqualifying.
            path=shlex.quote(remote)
            probe=self.guest('probe-production-'+name,
                f'if [ -f {path} ] && [ ! -L {path} ]; then exit 0; '
                f'elif [ ! -e {path} ] && [ ! -L {path} ]; then exit 3; else exit 4; fi',required=False)
            row['probe_exit']=probe
            row['status']=('missing_expected' if required else 'missing_optional') if probe==3 else ('transport_error' if probe==0 else 'probe_error')
            self.checkpoint('guest-readback-'+name,**row)
            if probe==3 and not required:return None
            raise AssertionError('missing expected guest artifact: '+name if probe==3 else 'guest artifact readback failed: '+name)

        def cold(self,name,*,success=False,replay=False):
            self.shutdown();self.boot_index+=1
            init='/root/storage-production/foreign-init' if name=='cold-foreign-writer' else BOOT
            self.start(self.boot_index,True,self.boot,init=init,root_rw=name!='cold-readonly-root')
            deadline=time.monotonic()+360
            outcome=None
            while time.monotonic()<deadline:
                assert self.proc is not None and self.proc.poll() is None,'maintenance init exited'
                raw=self.serial.read_text(errors='replace') if self.serial.exists() else ''
                assert 'QA_PRIVATE_CANARY' not in raw,'private malformed request bytes leaked to serial'
                values=[]
                for line in raw.splitlines():
                    try: value=json.loads(line.strip())
                    except (ValueError,TypeError):continue
                    if isinstance(value,dict):values.append(value)
                if success:
                    outcomes=[v for v in values if v.get('phase')=='committed']
                    assert not any(v.get('phase')=='refused' or v.get('state')=='refused' for v in values),'production entry refused valid request'
                    if outcomes:
                        assert any(v.get('entry')=='wizard-storage-maintenance-v1' and v.get('state')=='admitted' for v in values)
                        outcome=outcomes[-1];break
                else:
                    refused=[v for v in values if v.get('phase')=='refused' or v.get('state')=='refused']
                    if refused:
                        candidate=refused[-1]
                        expected = {
                            'cold-invalid-json': ('JSONDecodeError', 'fixed maintenance exception'),
                            'cold-wrong-field': ('Refusal', 'unknown maintenance request field'),
                            'cold-wrong-type': ('Refusal', 'invalid maintenance request value'),
                            'cold-invalid-action': ('Refusal', 'invalid maintenance request value'),
                            'cold-symlink-request': ('OSError', 'fixed maintenance exception'),
                            'cold-writable-request': ('Refusal', 'unsafe control record'),
                            'retired-request-does-not-reapply': ('FileNotFoundError', 'fixed maintenance exception')}
                        if name in ('cold-readonly-root', 'cold-foreign-writer'):
                            assert candidate.get('code') == ('readonly-root' if name=='cold-readonly-root' else 'another-userspace-process'), 'wrong bootstrap refusal reason'
                        else:
                            assert (candidate.get('error_type'), candidate.get('reason')) == expected[name], 'wrong request refusal reason'
                            assert any(v.get('state')=='admitted' for v in values), 'request guard never reached'
                        outcome=candidate;break
                time.sleep(0.25)
            assert outcome is not None,'cold entry outcome timeout'
            # Still maintenance after result, no auto systemd/services/writers.
            time.sleep(1)
            assert self.proc.poll() is None
            self.checkpoint('cold-outcome',case=name,init=init,outcome=outcome,serial_sha256=digest(self.serial))
            self.stop()  # bounded hypervisor reset only; not a claimed new power-cut acceptance
            self.start_normal()
            if not success and not replay:
                self.guest('refusal-readback-'+name,'/usr/bin/python3 -I '+GUEST+' refusal '+name)
                if name=='cold-foreign-writer':
                    self.guest('foreign-writer-liveness','test -s /home/storageqa/.hermes/foreign-writer')
            return outcome

        def execute(self):
            self.capacity();self.phase('create-synthetic-guest')
            self.run('key',['ssh-keygen','-q','-t','ed25519','-N','','-f',self.key])
            self.run('overlay',['qemu-img','create','-f','qcow2','-F','qcow2','-b',base.BASE,self.disk,'56G'])
            self.run('secondary',['qemu-img','create','-f','qcow2',self.secondary,'4G'])
            key=self.key.with_suffix('.pub').read_text().strip()
            (self.evidence/'user-data').write_text('#cloud-config\nusers:\n  - name: root\n    lock_passwd: true\n    ssh_authorized_keys:\n      - '+key+'\ndisable_root: false\nssh_pwauth: false\nwrite_files:\n  - path: /etc/ssh/sshd_config.d/99-storage-proof.conf\n    content: |\n      PermitRootLogin prohibit-password\n      PasswordAuthentication no\n')
            (self.evidence/'meta-data').write_text('instance-id: storage-production-'+self.doc['revision'][:12]+'\nlocal-hostname: storage-production\n')
            self.run('seed',['cloud-localds',self.evidence/'seed.iso',self.evidence/'user-data',self.evidence/'meta-data'])
            self.start_normal()
            self.guest('prerequisites','cloud-init status --wait\napt-get -o DPkg::Lock::Timeout=600 update -qq\nDEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq acl e2fsprogs python3-pytest python3-yaml git util-linux curl sudo python3-venv build-essential libatomic1 ca-certificates\ninstall -d -m700 /root/storage-proof /root/storage-proof/source /root/storage-production\n',1200)
            for name in ('source.tar','source.bundle','shallow','guest.py','service.tar'):
                self.copy(self.stage/name,'/root/storage-proof/'+name)
            self.copy(self.stage/'service-members.json','/root/storage-production/service-members.json')
            self.guest('exact-source','umask 022\ntar --no-same-permissions -xf /root/storage-proof/source.tar -C /root/storage-proof/source\n'
                'mkdir -m700 /root/storage-proof/template\nif [ -s /root/storage-proof/shallow ]; then cp /root/storage-proof/shallow /root/storage-proof/template/shallow; fi\n'
                'git clone --template=/root/storage-proof/template /root/storage-proof/source.bundle /root/storage-proof/genuine\n'
                'mv /root/storage-proof/genuine/.git /root/storage-proof/source/.git\ncd /root/storage-proof/source\n'
                'test "$(git rev-parse HEAD)" = '+self.doc['revision']+'\ngit fsck --full\ntest -z "$(git status --porcelain)"\n'
                '/usr/bin/python3 -I scripts/install-storage-maintenance.py\n'
                'install -m644 /root/storage-proof/guest.py '+GUEST+'\n')
            self.focused_tests()
            tree=ast.parse((self.stage/'stock-fixture.py').read_text())
            setup=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
                and any(isinstance(t,ast.Name) and t.id=='HERMES_SETUP' for t in n.targets))
            clone='git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$HOME/hermes-agent"'
            assert setup.count(clone)==1
            setup=setup.replace(clone,'git init "$HOME/hermes-agent"\ngit -C "$HOME/hermes-agent" remote add origin https://github.com/NousResearch/hermes-agent.git\ngit -C "$HOME/hermes-agent" fetch --depth 1 origin '+STOCK+'\ngit -C "$HOME/hermes-agent" checkout --detach FETCH_HEAD')
            # Supported synthetic leaf is born private; never chmod/chown a
            # permissive legacy leaf into acceptance. Stock source is unchanged.
            setup=setup.replace('mkdir -p "$HOME/.hermes"','umask 077\nmkdir -p "$HOME/.hermes"')
            self.guest('stock-hermes-fixture',setup,2400)
            self.guest('seed-stock-history','runuser -u hermes -- env HOME=/home/hermes HERMES_HOME=/home/hermes/.hermes bash -se <<\'AGENT\'\ncd /home/hermes/hermes-agent\nsource ./activate\npython '+GUEST+' seed-agent\nAGENT\n'
                'useradd -m -s /bin/bash storageqa\nrunuser -u storageqa -- mkdir -m700 /home/storageqa/.hermes\n'
                'runuser -u storageqa -- sh -c \'umask 077; printf "synthetic refused request bytes\\n" > /home/storageqa/.hermes/sentinel\'\n'
                '/usr/bin/python3 -I '+GUEST+' snapshot-agent\n',180)
            self.mark('ordinary-and-real-nested-entry-refused',lambda:(
                self.guest('ordinary-and-nested','/usr/bin/python3 -I '+GUEST+' online'),self.collect_json('online')))
            self.guest('boot-metadata',"python3 -c 'import json,os,subprocess; from pathlib import Path;k=os.uname().release;print(json.dumps({\"kernel\":\"/boot/vmlinuz-\"+k,\"initrd\":\"/boot/initrd.img-\"+k,\"root_uuid\":subprocess.check_output([\"findmnt\",\"-no\",\"UUID\",\"/\"],text=True).strip()}))' > /root/storage-proof/boot.json")
            self.copy(self.evidence/'boot.json','/root/storage-proof/boot.json',True)
            self.boot=json.loads((self.evidence/'boot.json').read_text());assert re.fullmatch('[A-Fa-f0-9-]+',self.boot['root_uuid'])
            self.copy(self.evidence/'vmlinuz',self.boot['kernel'],True);self.copy(self.evidence/'initrd',self.boot['initrd'],True)
            self.receipt['boot_inputs']={k:digest(self.evidence/k) for k in ('vmlinuz','initrd')};self.save()
            self.continue_production()

        def continue_production(self):
            """Resume only after fresh synthetic durable-state + source checks."""
            self.phase('actual-packaged-production-cold-entry')
            for name in COLD_NEGATIVE:
                self.guest('request-'+name,'/usr/bin/python3 -I '+GUEST+' request '+name)
                self.mark(name,lambda name=name:self.cold(name))
            self.guest('real-selected-request','/usr/bin/python3 -I '+GUEST+' valid-request')
            self.mark('cold-production-entry-real-request',lambda:self.cold('cold-production-entry-real-request',success=True))
            self.mark('normal-boot-stock-gateway-original-environment',lambda:(
                self.guest('return-normal','/usr/bin/python3 -I '+GUEST+' return-normal'),self.collect_json('return-normal')))
            self.guest('retired-before','/usr/bin/python3 -I '+GUEST+' retired-before')
            self.mark('retired-request-does-not-reapply',lambda:(
                self.cold('retired-request-does-not-reapply',replay=True),
                self.guest('retired-after','/usr/bin/python3 -I '+GUEST+' retired-after')))
            # Source-bound Service packaging only because no retained current-head
            # image receipt is available. This is inside the native admitted guest.
            self.phase('real-source-bound-service-and-installed-backend')
            repo_code = "from pathlib import Path;Path('/etc/apt/sources.list.d/docker.list').write_text(" + repr('deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable\n') + ')'
            # Same official signed Docker apt route as the shipped installer;
            # no Debian-only guessed compose-plugin package prerequisite.
            self.guest('docker-prerequisite','install -m0755 -d /etc/apt/keyrings\n'
                'curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc\nchmod a+r /etc/apt/keyrings/docker.asc\n'
                'python3 -c '+shlex.quote(repo_code)+'\n'
                'apt-get -o DPkg::Lock::Timeout=600 update -qq\nDEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin\n',1200)
            self.copy(BACKEND_ARCHIVE,'/root/storage-production/backend.tar.gz')
            self.guest('load-backend','docker load -i /root/storage-production/backend.tar.gz\ndocker tag '+BACKEND+' privsep-wam:595523fb\n',300)
            self.guest('package-exact-service','mkdir /root/storage-production/service\ntar -xf /root/storage-proof/service.tar -C /root/storage-production/service\n'
                'docker build --label org.opencontainers.image.revision='+self.doc['service_revision']+' --iidfile /root/storage-production/service-image-id '
                '-t privsep-service:'+self.doc['service_revision'][:12]+' /root/storage-production/service\n',1800)
            self.copy(self.evidence/'service-image-id','/root/storage-production/service-image-id',True)
            service_image=(self.evidence/'service-image-id').read_text().strip();assert re.fullmatch('sha256:[0-9a-f]{64}',service_image)
            self.receipt['service_image']=service_image;self.save()
            self.guest('preserve-service-image',
                'test "$(docker image inspect --format \'{{.Size}}\' '+service_image+')" -le 2147483648\n'
                'set -o pipefail\ndocker save '+service_image+' | gzip -1 > /root/storage-production/service-image.tar.gz\n'
                'gzip -t /root/storage-production/service-image.tar.gz\n'
                'test "$(stat -c %s /root/storage-production/service-image.tar.gz)" -le 2147483648',300)
            self.copy(self.evidence/'service-image.tar.gz','/root/storage-production/service-image.tar.gz',True)
            self.receipt['service_archive_sha256']=digest(self.evidence/'service-image.tar.gz');self.save()
            self.guest('normal-alongside-production-install','umask 022\nexport WAM_REF='+self.doc['revision']+' WAM_IMAGE=privsep-wam:595523fb WIZARD_RELEASE=none '
                'WIZARD_MODE=alongside WIZARD_HERMES_USER=hermes HERMES_HOME=/home/hermes/.hermes '
                'WIZARD_AGENT_SERVICE_IMAGE=privsep-service:'+self.doc['service_revision'][:12]+' WIZARD_PRIVATE_ACCESS=none WIZARD_GITHUB=0 WIZARD_AGENT_SAFE=0\n'
                'bash /root/storage-proof/source/get.sh\n',7200)
            self.mark('installed-compiled-backend-physical-service-consumer',lambda:(
                self.guest('installed-service','/usr/bin/python3 -I '+GUEST+' installed '+BACKEND+' '+service_image,300),self.collect_json('installed')))
            self.mark('stale-and-unavailable-mapping-before-update-restart',lambda:(
                self.guest('stale-mapping','/usr/bin/python3 -I '+GUEST+' stale-mapping',120),self.collect_json('stale-mapping')))
            self.receipt['service_image']=service_image
            rows=self.receipt['actual_cases'];actual=[r['name'] for r in rows]
            assert sorted(actual)==sorted(CASES) and len(actual)==len(set(actual)) and all(r['passed'] for r in rows)
            self.cleanup_guest()
            self.receipt['passed']=True;self.save();self.shutdown()

        def cleanup_guest(self):
            if 'real-source-bound-service-and-installed-backend' not in self.receipt['reached_phases']:return
            if self.receipt.get('guest_container_cleanup'):
                assert self.receipt['guest_container_cleanup'].get('passed') is True, 'prior exact cleanup failed; not replayed'
                return
            # A failed Docker prerequisite can have no daemon yet. Once the
            # source packaging phase reached a live daemon, exact cleanup is mandatory.
            rc=self.guest('docker-daemon-cleanup-readiness','test -S /var/run/docker.sock',30,False)
            if rc!=0:
                assert 'service_image' not in self.receipt,'expected Docker daemon unavailable at cleanup'
                return
            rc=self.guest('cleanup-guest-containers','/usr/bin/python3 -I '+GUEST+' cleanup-containers',180,False)
            self.receipt['guest_container_cleanup']=self.collect_json('container-cleanup');self.save()
            assert rc == 0 and self.receipt['guest_container_cleanup']['passed'], 'exact guest container cleanup failed'

        def diagnostics(self):
            # Serial and per-phase stdout already exist even before first SSH.
            # Export finite synthetic reports BEFORE cleanup, never broad home logs.
            if self.proc is not None and self.proc.poll() is None and not self.receipt['boots'][-1]['maintenance']:
                for name in ('original-agent','online','return-normal','installed','stale-mapping'):
                    try:self.collect_json(name,required=False)
                    except Exception as exc:self.receipt['diagnostic_errors'].append({'name':name,'error_type':type(exc).__name__})
            self.write_junit();self.save()
    return ProductionRunner


def startup_failure(stage, phase, exc):
    # Separate from evidence/receipt.json: even a half-constructed base object
    # may not have a usable receipt. Never overwrite a prior attempt's record.
    st = stage.lstat()
    assert not stage.is_symlink() and st.st_uid == os.getuid() and not st.st_mode & 0o022
    row = {'schema':'storage-production-startup-failure-v1','passed':False,
        'full_integration_acceptance':False,'execution_started':False,'phase':phase,
        'first_error':{'type':type(exc).__name__,
            'reason':str(exc)[:512] if isinstance(exc,(ManifestContractError,AssertionError,TimeoutError)) else 'fixed setup exception',
            'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno}
                      for f in traceback.extract_tb(exc.__traceback__)]}}
    fd = os.open(stage/'startup-failure.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(row,stream,sort_keys=True,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    return row


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',type=Path,required=True);p.add_argument('--manifest-sha256',required=True)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight',action='store_true');mode.add_argument('--execute',action='store_true')
    p.add_argument('--local-static',action='store_true')
    args=p.parse_args();assert not args.local_static or args.preflight
    stage=args.stage.absolute();phase='validate-manifest-and-inherited-contract'
    try:
        doc,old,result=validate(stage,args.manifest_sha256,local=args.local_static)
        if args.preflight: print(json.dumps(result,sort_keys=True));return 0
        phase='native-admission-and-capacity'
        old.native_lock();assert result['capacity_admissible'],'floor plus honest pending peak not available'
        phase='private-temporary-directory'
        os.umask(0o077);tmp=stage/'tmp';tmp.mkdir(mode=0o700);os.environ['TMPDIR']=str(tmp)
        os.environ['PYTHONDONTWRITEBYTECODE']='1'
        phase='genuine-production-runner-constructor'
        runner=make_runner(old)(stage,doc)
    except BaseException as exc:
        if args.preflight:raise
        print(json.dumps(startup_failure(stage,phase,exc),sort_keys=True));return 1
    def interrupt(sig,frame):raise TimeoutError('bounded native execution deadline')
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGALRM):signal.signal(sig,interrupt)
    signal.alarm(17400)
    try:runner.execute()
    except BaseException as exc:
        runner.receipt['first_error']={'type':type(exc).__name__, 'reason':str(exc) if isinstance(exc,(AssertionError,TimeoutError)) else 'fixed phase exception',
            'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]}
        runner.receipt['passed']=False;runner.save();runner.diagnostics()
    finally:
        if runner.proc is not None and runner.proc.poll() is None and not runner.receipt['boots'][-1]['maintenance']:
            try:runner.cleanup_guest()
            except BaseException as exc:runner.receipt['cleanup_errors'].append('guest-containers:'+type(exc).__name__)
        try:runner.stop()
        except BaseException as exc:runner.receipt['cleanup_errors'].append(type(exc).__name__)
        runner.receipt['all_owned_vms_stopped']=all(r['stopped'] for r in runner.receipt['boots'])
        runner.receipt['all_boot_pids_absent']=all(not Path('/proc',str(r['pid'])).exists() for r in runner.receipt['boots'])
        runner.receipt['passed']=runner.receipt['passed'] and runner.receipt['all_owned_vms_stopped'] and runner.receipt['all_boot_pids_absent'] and not runner.receipt['cleanup_errors'] and not runner.receipt['diagnostic_errors']
        runner.write_junit();runner.save()
    print(json.dumps(runner.receipt,sort_keys=True));return 0 if runner.receipt['passed'] else 1


if __name__=='__main__':sys.exit(main())
