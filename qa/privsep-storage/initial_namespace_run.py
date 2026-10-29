#!/usr/bin/env python3
"""Focused initial-PID fence proof; parent admission ONLY.

Read-only --preflight; --execute creates only fresh synthetic KVM overlays.
Exit 0 requires eight exact fence oracles, one guard-removal negative control,
focused units and complete VM cleanup. NOT storage/install/full acceptance.
"""
import argparse
import ast
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
import xml.etree.ElementTree as ET

ORIGINAL = '18d6203f4a3128a91117900ffdc5514180ee8b82'
RESERVE = 217179869184
PENDING = 21474836480
EXPECTED = ('ordinary-initial-process', 'ordinary-original-nested-check',
            'ordinary-nested-pid1', 'cold-initial-sole-init',
            'cold-initial-foreign-userspace', 'cold-original-nested-check',
            'cold-nested-hidden-foreign', 'cold-initial-after-reap')
REQUIRED = ('kernel', 'pid', 'ppid', 'uid', 'euid', 'uid_map', 'capabilities',
            'cmdline', 'namespaces', 'self_pid_parent', 'userspace_census',
            'task_ids', 'fence', 'controls_absent', 'errors')
PROOF_FILES = ('qa/privsep-storage/initial_namespace_guest.py',
               'qa/privsep-storage/initial_namespace_run.py',
               'tests/test_initial_pid_namespace.py',
               'docs/storage-initial-pid-namespace.md',
               'qa/privsep-storage/initial_namespace_sources.json')
BASELINE = 'dd15870c02dfbe7ece0b98f1cffee83cba270ab6'
TEST_MODULES = ('tests/test_agent_storage.py', 'tests/test_initial_pid_namespace.py')


def test_inventory(sources):
    rows = []
    for path in TEST_MODULES:
        tree = ast.parse(sources[path])
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith('test_'): continue
            suffixes = ['']
            assert len(node.decorator_list) <= 1
            for decorator in node.decorator_list:
                assert isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == 'pytest.mark.parametrize'
                arg = ast.literal_eval(decorator.args[0]); values = ast.literal_eval(decorator.args[1])
                assert isinstance(arg, str) and ',' not in arg and not decorator.keywords
                ids = [str(v).encode('unicode_escape').decode('ascii') if isinstance(v, (str, int, bool))
                       else arg+str(i) for i, v in enumerate(values)]
                assert len(ids) == len(set(ids))
                suffixes = ['['+v+']' for v in ids]
            rows.extend(path+'::'+node.name+suffix for suffix in suffixes)
    assert len(rows) == len(set(rows)) and rows
    return sorted(rows)


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive_inventory(path):
    rows = []
    with tarfile.open(path) as archive:
        for member in archive:
            if member.isdir(): continue
            assert not member.name.startswith('/') and '..' not in Path(member.name).parts
            assert member.isfile() or member.issym()
            if member.isfile():
                stream = archive.extractfile(member); assert stream is not None
                data = stream.read()
            else: data = os.fsencode(member.linkname)
            rows.append({'path': member.name, 'kind': 'file' if member.isfile() else 'symlink',
                         'mode': member.mode, 'sha256': hashlib.sha256(data).hexdigest()})
    assert len(rows) == len({row['path'] for row in rows})
    return sorted(rows, key=lambda row: row['path'])


def preflight(stage):
    assert stage.is_absolute() and stage.parent == Path('/srv/work/privsep/storage-initial-pid')
    doc = json.loads((stage/'manifest.json').read_text())
    assert re.fullmatch('[0-9a-f]{40}', doc['revision']) and stage.name == doc['revision']
    assert doc['baseline_revision'] == BASELINE and doc['original_revision'] == ORIGINAL
    assert doc['branch'] == 'fix/storage-initial-pid-identity'
    assert doc['reserve_bytes'] == RESERVE and doc['pending_bytes'] == PENDING
    assert doc['expected_cases'] == list(EXPECTED)
    assert doc['storage_acceptance'] is False and doc['review_required'] is True
    required = {'runner.py', 'guest.py', 'original-runner.py', 'source.tar', 'source-members.json',
                'baseline-source.tar', 'baseline-source-members.json',
                'inherited-receipt.json', 'inherited-pytest.xml',
                'baseline-diagnostic-receipt.json', 'baseline-cold.json', 'baseline-prepare.json'}
    assert set(doc['files']) == required
    for name, value in doc['files'].items():
        path = stage/name; st = path.lstat()
        assert path.is_file() and not path.is_symlink() and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0
        assert digest(path) == value, name
    assert digest(__file__) == doc['files']['runner.py']
    old = archive_inventory(stage/'baseline-source.tar'); new = archive_inventory(stage/'source.tar')
    assert old == json.loads((stage/'baseline-source-members.json').read_text())
    assert new == json.loads((stage/'source-members.json').read_text())
    old_by = {row['path']: row for row in old}; new_by = {row['path']: row for row in new}
    assert set(new_by)-set(old_by) == set(PROOF_FILES) and not set(old_by)-set(new_by)
    changed = {key for key, row in old_by.items() if new_by[key] != row}
    assert changed == {'scripts/protected-agent-storage.py'}, changed
    assert new_by['scripts/protected-agent-storage.py']['sha256'] == doc['product_sha256']
    for source, staged in zip(PROOF_FILES[:2], ('guest.py', 'runner.py')):
        assert new_by[source]['sha256'] == doc['files'][staged]
    assert old_by['qa/privsep-storage/run.py']['sha256'] == doc['files']['original-runner.py']
    # Exact changed-function scope: no transaction/R1/R2/F2 implementation delta.
    with tarfile.open(stage/'baseline-source.tar') as archive:
        before = ast.parse(archive.extractfile('scripts/protected-agent-storage.py').read().decode())
    with tarfile.open(stage/'source.tar') as archive:
        after = ast.parse(archive.extractfile('scripts/protected-agent-storage.py').read().decode())
        sources = {path: archive.extractfile(path).read().decode() for path in TEST_MODULES}
        provenance = json.loads(archive.extractfile(PROOF_FILES[-1]).read())
        assert provenance['identity_rule'] == 'nsfs+CLONE_NEWPID+PID_NS_INIT_INO'
    old_nodes = {getattr(n, 'name', 'old-node-'+str(i)): ast.dump(n) for i, n in enumerate(before.body)}
    after_dumps = {ast.dump(n) for n in after.body}
    assert all(value in after_dumps for name, value in old_nodes.items() if name != 'require_fence')
    assert test_inventory(sources) == doc['expected_pytest_cases']
    assert doc['expected_pytest_count'] == len(doc['expected_pytest_cases'])
    guest_tree = ast.parse((stage/'guest.py').read_text()); literals = {}
    for node in guest_tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ('EXPECTED', 'REQUIRED'):
            literals[node.targets[0].id] = ast.literal_eval(node.value)
    assert literals == {'EXPECTED': EXPECTED, 'REQUIRED': REQUIRED}
    # Original passed bytes stay historical: product changed, not a fresh 135 pass.
    assert doc['files']['inherited-receipt.json'] == '5799311e167dc962c3741d702d456fff7d5969ccb15850823a85c5e6128775c2'
    assert doc['files']['inherited-pytest.xml'] == 'a1896c224465e650edf94fc4b89cc0908bca4a0ce12a036ec7627eb35af3cfbe'
    junit = ET.parse(stage/'inherited-pytest.xml'); cases = junit.findall('.//testcase')
    assert len(cases) == 135 and all(not junit.findall('.//'+key) for key in ('failure', 'error', 'skipped'))
    diagnostic = json.loads((stage/'baseline-diagnostic-receipt.json').read_text())
    assert doc['files']['baseline-diagnostic-receipt.json'] == '614cf420210ffc04cb7c7ff47dae28fb83594523e212af9d8bf932d4095b6b3c'
    assert diagnostic['diagnostic_complete'] is True and diagnostic['storage_acceptance'] is False
    assert diagnostic['cleanup_errors'] == [] and diagnostic['all_owned_vms_stopped'] is True
    for report in diagnostic['reports']:
        if report['name'] == 'cold-serial-report.json':
            assert report['sha256'] == doc['files']['baseline-cold.json']
        if report['name'] == 'prepare-namespace-diagnostic.json':
            assert report['sha256'] == doc['files']['baseline-prepare.json']
    baseline_cold = json.loads((stage/'baseline-cold.json').read_text())
    bypass = next(row for row in baseline_cold['rows'] if row['name'] == 'cold-nested-hidden-foreign')
    assert bypass['snapshot']['fence']['admitted'] is True and all(bypass['fixture'].values())
    assert bypass['foreign_alive_after_nested'] is True
    inherited = json.loads((stage/'inherited-receipt.json').read_text())
    for receipt in (diagnostic, inherited):
        assert receipt['all_owned_vms_stopped'] is True and receipt['cleanup_errors'] == []
        assert all(not Path('/proc/'+str(row['pid'])).exists() for row in receipt['boots'])
    base = Path('/srv/work/images/debian-12-genericcloud-amd64.qcow2')
    assert digest(base) == '24036a8b79cbb11d809752c60f2f885b1a3b02b1d663faa0b648eb87bd7a256e'
    for exe in ('qemu-system-x86_64', 'qemu-img', 'cloud-localds', 'ssh', 'scp', 'ssh-keygen'):
        assert shutil.which(exe), exe
    assert os.access('/dev/kvm', os.R_OK | os.W_OK)
    free = shutil.disk_usage(stage).free
    return doc, {'static_integrity': True, 'heavy_executed': False,
        'focused_fence_acceptance': False, 'storage_acceptance': False, 'review_required': True,
        'free_bytes': free, 'reserve_bytes': RESERVE, 'pending_bytes': PENDING,
        'required_bytes': RESERVE+PENDING, 'shortfall_bytes': max(0, RESERVE+PENDING-free),
        'capacity_admissible': free >= RESERVE+PENDING, 'expected_cases': list(EXPECTED),
        'focused_pytest_expected_count': doc['expected_pytest_count'],
        'historical_pytest_count': 135, 'historical_suite_not_rerun': True}


def focused_tests(runner, doc):
    rc = runner.guest('focused-tests', 'cd /root/storage-proof/source\n'
        '/usr/bin/python3 -m pytest -q '+' '.join(TEST_MODULES)+
        ' --junitxml=/root/storage-proof/focused-pytest.xml\n', 240, False)
    path = runner.evidence/'focused-pytest.xml'
    copy_rc = runner.copy(path, '/root/storage-proof/focused-pytest.xml', True, required=False)
    record = {'exit': rc, 'copy_exit': copy_rc, 'sha256': None, 'actual_cases': [],
              'failures': None, 'errors': None, 'skipped': None, 'collection_error': None}
    try:
        if copy_rc != 0: raise ValueError('focused junit not exported')
        record['sha256'] = digest(path); tree = ET.parse(path)
        record['actual_cases'] = sorted(case.get('classname', '').replace('.', '/')+'.py::'+case.get('name', '')
                                        for case in tree.findall('.//testcase'))
        for key in ('failures', 'errors', 'skipped'):
            record[key] = len(tree.findall('.//'+{'failures': 'failure', 'errors': 'error', 'skipped': 'skipped'}[key]))
    except Exception as exc: record['collection_error'] = type(exc).__name__
    runner.receipt['focused_pytest'] = record; runner.save()
    assert rc == 0 and copy_rc == 0 and record['collection_error'] is None
    assert record['actual_cases'] == doc['expected_pytest_cases']
    assert all(record[key] == 0 for key in ('failures', 'errors', 'skipped'))


def load_original(stage):
    spec = importlib.util.spec_from_file_location('retained_storage_runner', stage/'original-runner.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def collect(runner, name):
    # Reports exist only in this fresh root-only synthetic guest.
    path = runner.evidence/name
    rc = runner.copy(path, '/root/storage-proof/'+name, True, required=False)
    record = {'name': name, 'copy_exit': rc, 'sha256': None, 'report': None, 'error': None}
    if rc == 0:
        record['sha256'] = digest(path)
        try: record['report'] = json.loads(path.read_text())
        except Exception as exc: record['error'] = type(exc).__name__
    runner.receipt['reports'].append(record); runner.save()
    return record


def complete_report(report, mode, doc):
    assert report is not None and report['schema'] == 'initial-pid-repair-proof-v1' and report['mode'] == mode
    assert report['storage_acceptance'] is False and report['review_required'] is True
    assert report['guest_sha256'] == doc['files']['guest.py'] and report['product_sha256'] == doc['product_sha256']
    assert report['mutation_sha256'] == doc['mutation_sha256']
    assert report['first_error'] is None, 'guest diagnostic error; preserve actual report'
    expected = EXPECTED[:3] if mode == 'prepare' else EXPECTED[3:]
    assert [row['name'] for row in report['rows']] == list(expected)
    for row in report['rows']:
        if 'original-nested-check' in row['name']:
            assert all(key in row for key in ('exit', 'stdout_b64', 'stderr_b64', 'timed_out', 'original_assertion_satisfied'))
            assert row['exit'] is not None and row['stdout_b64'] is not None and row['stderr_b64'] is not None and not row['timed_out']
        else:
            value = row.get('snapshot') if 'nested-' in row['name'] else row
            assert value is not None and all(key in value and value[key] is not None for key in REQUIRED)
            assert value['collection_complete'] is True and not value['errors']
            assert value['fence']['admitted'] in (True, False), 'fence exception is unknown, not refusal'
            assert value['product_sha256'] == doc['product_sha256'] and all(value['controls_absent'].values())
            pidns = value['namespaces']['pid']
            assert pidns['nsfs_magic'] == 0x6e736673 and pidns['nstype'] == 0x20000000
            assert (pidns['identity'][1] == 0xeffffffc) == (value is row)
            if value is not row:
                assert 0xf0000000 <= pidns['identity'][1] <= 0xffffffff
            if value is not row:
                assert row['exit'] == 0 and not row['timed_out'] and row['error'] is None
                assert row['parent_scope_ioctl'] is not None and value['pid'] == 1
                keys = {'pid_is_one', 'pid_namespace_differs_from_parent', 'same_user_namespace_as_parent',
                        'parent_scope_ioctl_reaches_initial', 'same_actual_kernel_cmdline',
                        'only_nested_userspace_pid_one_visible'}
                assert row['fixture'] is not None and set(row['fixture']) == keys
                assert all(row['fixture'].values()), 'nested fixture qualification failed; not a safety pass'
                if row['name'] == 'cold-nested-hidden-foreign':
                    assert row['foreign_alive_after_nested'] is True and row['foreign_outer_pid'] > 1
                    assert value['mutation_sha256'] == report['mutation_sha256'] == doc['mutation_sha256']
                    assert value['identity_guard_removed_fence']['admitted'] is True
                    assert value['identity_guard_removed_fence']['error_type'] is None
                    assert value['fence']['admitted'] is False and value['fence']['refusal'] == 'nested PID namespace'
    return True


def evaluate(reports):
    rows = {row['name']: row for report in reports if report is not None for row in report['rows']}
    checks = {name: {'observed': name in rows, 'safety_expectation_met': None} for name in EXPECTED}
    for name, row in rows.items():
        if 'original-nested-check' in name:
            checks[name]['safety_expectation_met'] = row['original_assertion_satisfied']
            continue
        value = row.get('snapshot') if 'nested-' in name else row
        if not value or value.get('fence') is None: continue
        if value is not row and (not row.get('fixture') or not all(row['fixture'].values())): continue
        admitted = value['fence']['admitted']
        if name in ('cold-initial-sole-init', 'cold-initial-after-reap') and admitted:
            assert value['fence']['value']['mechanism'] == 'sole-host-init-v2'
            assert value['fence']['value']['pid_namespace']['inode'] == 0xeffffffc
        if name == 'cold-initial-foreign-userspace' and admitted is False:
            assert value['fence']['refusal'] == 'maintenance contains another userspace process'
        if name == 'cold-initial-after-reap': assert row['foreign_reaped'] is True
        checks[name]['safety_expectation_met'] = admitted if name in ('cold-initial-sole-init', 'cold-initial-after-reap') else (not admitted if admitted is not None else None)
    return checks


def execute(stage, doc, module):
    runner = module.Runner(stage, doc)
    runner.receipt.update(diagnostic_only=False, focused_fence_acceptance=False, storage_acceptance=False, passed=False,
        full_integration_acceptance=False, diagnostic_complete=False, review_required=True,
        original_revision=ORIGINAL, manifest_sha256=digest(stage/'manifest.json'),
        reports=[], checks=evaluate([]), historical_pytest_count=135, historical_suite_not_rerun=True,
        scope='focused fence proof only; no transaction, storage acceptance, install, merge or release')
    runner.save()
    try:
        runner.capacity()
        runner.run('key', ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', runner.key])
        runner.run('overlay', ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', module.BASE, runner.disk, '14G'])
        runner.run('secondary', ['qemu-img', 'create', '-f', 'qcow2', runner.secondary, '4G'])
        key = runner.key.with_suffix('.pub').read_text().strip()
        (runner.evidence/'user-data').write_text('#cloud-config\nusers:\n  - name: root\n    lock_passwd: true\n    ssh_authorized_keys:\n      - '+key+'\ndisable_root: false\nssh_pwauth: false\nwrite_files:\n  - path: /etc/ssh/sshd_config.d/99-namespace-proof.conf\n    content: |\n      PermitRootLogin prohibit-password\n      PasswordAuthentication no\n')
        (runner.evidence/'meta-data').write_text('instance-id: namespace-'+doc['revision']+'\nlocal-hostname: namespace-proof\n')
        runner.run('seed', ['cloud-localds', runner.evidence/'seed.iso', runner.evidence/'user-data', runner.evidence/'meta-data'])
        runner.start('prepare')
        runner.guest('prerequisites', 'cloud-init status --wait\ncommand -v unshare\ntest -x /usr/bin/python3\ninstall -d -m700 /root/storage-proof /root/storage-proof/source\n', 180)
        runner.copy(stage/'source.tar', '/root/storage-proof/source.tar')
        runner.copy(stage/'guest.py', '/root/storage-proof/namespace-diagnostic.py')
        runner.guest('extract', 'umask 022\ntar --no-same-permissions -xf /root/storage-proof/source.tar -C /root/storage-proof/source\nchmod 600 /root/storage-proof/namespace-diagnostic.py\n')
        runner.guest('pytest-prerequisites', 'apt-get update -qq\nDEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pytest\n', 240)
        focused_tests(runner, doc)
        rc = runner.guest('ordinary-observations', '/usr/bin/python3 -I /root/storage-proof/namespace-diagnostic.py prepare\n', 150, False)
        normal = collect(runner, 'prepare-initial-pid-repair.json')
        runner.receipt['checks'] = evaluate([normal['report']]); runner.save()
        assert rc == 0 and complete_report(normal['report'], 'prepare', doc), 'ordinary diagnostic incomplete'
        runner.guest('boot-metadata', "python3 -c 'import json,os,subprocess; k=os.uname().release; print(json.dumps({\"kernel\":\"/boot/vmlinuz-\"+k,\"initrd\":\"/boot/initrd.img-\"+k,\"root_uuid\":subprocess.check_output([\"findmnt\",\"-no\",\"UUID\",\"/\"],text=True).strip()}))' > /root/storage-proof/boot.json\n")
        runner.copy(runner.evidence/'boot.json', '/root/storage-proof/boot.json', True)
        boot = json.loads((runner.evidence/'boot.json').read_text())
        assert re.fullmatch('[A-Fa-f0-9-]+', boot['root_uuid'])
        assert re.fullmatch('/boot/vmlinuz-[A-Za-z0-9.+_-]+', boot['kernel'])
        assert re.fullmatch('/boot/initrd.img-[A-Za-z0-9.+_-]+', boot['initrd'])
        runner.copy(runner.evidence/'vmlinuz', boot['kernel'], True)
        runner.copy(runner.evidence/'initrd', boot['initrd'], True)
        runner.receipt['boot_inputs'] = {key: digest(runner.evidence/key) for key in ('vmlinuz', 'initrd')}; runner.save()
        runner.guest('shutdown-prepare', 'sync\nsystemctl poweroff\n', 60, False)
        runner.proc.wait(timeout=60); runner.stop()
        runner.capacity(); runner.start('cold', True, boot)
        end = time.monotonic()+180
        while time.monotonic() < end and runner.proc.poll() is None: time.sleep(0.25)
        assert runner.proc.poll() is not None, 'bounded cold diagnostic did not shut down'
        runner.stop()
        text = runner.serial.read_text(errors='replace')
        tokens = re.findall(r'^INITIAL_PID_REPAIR_JSON:([A-Za-z0-9+/=]+)\r?$', text, re.M)
        assert len(tokens) == 1, 'cold report missing or duplicate; retain serial'
        cold = json.loads(base64.b64decode(tokens[0], validate=True))
        module.put_json(runner.evidence/'cold-serial-report.json', cold)
        runner.receipt['reports'].append({'name': 'cold-serial-report.json', 'copy_exit': None,
            'sha256': digest(runner.evidence/'cold-serial-report.json'), 'report': cold, 'error': None})
        runner.receipt['checks'] = evaluate([normal['report'], cold]); runner.save()
        assert complete_report(cold, 'cold', doc)
        # One readback boot exports the fsynced report independently of serial.
        runner.start('readback')
        disk_report = collect(runner, 'cold-initial-pid-repair.json')
        assert disk_report['report'] == cold, 'disk/serial diagnostic report mismatch'
        runner.guest('shutdown-readback', 'sync\nsystemctl poweroff\n', 60, False)
        runner.proc.wait(timeout=60); runner.stop()
        checks = runner.receipt['checks']
        assert set(checks) == set(EXPECTED) and all(v['observed'] and v['safety_expectation_met'] is True for v in checks.values()), 'focused fence oracle failed'
        runner.receipt['diagnostic_complete'] = True
        runner.receipt['identity_guard_removal_admitted_nested'] = True
        runner.receipt['focused_fence_acceptance'] = True; runner.save()
    except BaseException as exc:
        if runner.receipt['first_error'] is None:
            runner.receipt['first_error'] = {'type': type(exc).__name__, 'reason': str(exc),
                'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                           for frame in traceback.extract_tb(exc.__traceback__)]}
        runner.save()
    finally:
        try: runner.stop()
        except BaseException as exc: runner.receipt['cleanup_errors'].append(type(exc).__name__)
        runner.receipt['all_owned_vms_stopped'] = all(row['stopped'] for row in runner.receipt['boots'])
        runner.receipt['diagnostic_complete'] = (runner.receipt['diagnostic_complete'] and
            runner.receipt['all_owned_vms_stopped'] and not runner.receipt['cleanup_errors'] and
            runner.receipt['first_error'] is None)
        runner.receipt['focused_fence_acceptance'] = (runner.receipt['focused_fence_acceptance'] and
            runner.receipt['diagnostic_complete'])
        runner.receipt['passed'] = runner.receipt['storage_acceptance'] = False
        runner.save()
    print(json.dumps(runner.receipt, sort_keys=True))
    return 0 if runner.receipt['focused_fence_acceptance'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--preflight', action='store_true'); modes.add_argument('--execute', action='store_true')
    args = parser.parse_args(); stage = args.stage.absolute()
    assert re.fullmatch('[0-9a-f]{64}', args.manifest_sha256)
    assert digest(stage/'manifest.json') == args.manifest_sha256, 'submitted manifest binding changed'
    doc, facts = preflight(stage)
    if args.preflight: print(json.dumps(facts, sort_keys=True)); return 0
    assert facts['capacity_admissible'], 'protected reserve + pending footprint unavailable'
    module = load_original(stage); module.native_lock()
    os.umask(0o077); temp = stage/'tmp'; temp.mkdir(mode=0o700)
    os.environ['TMPDIR'] = str(temp); os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    def interrupt(sig, frame): raise TimeoutError('bounded native signal '+str(sig))
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM): signal.signal(sig, interrupt)
    signal.alarm(1500)
    return execute(stage, doc, module)


if __name__ == '__main__': sys.exit(main())
