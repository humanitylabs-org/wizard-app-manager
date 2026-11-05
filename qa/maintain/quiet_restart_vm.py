#!/usr/bin/env python3
"""Destructive quiet-restart QA. ONLY wos-maint-qa (disposable Lima guest).
Invoked by the durable Thales worker, never on the host or an owner's install.
"""
import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wam import maintain as m, os_patch as op

BASE = Path('/var/lib/wizard-maint-qa')
HOME = BASE / 'wizard'
HH = BASE / 'hermes'
OUT = BASE / 'evidence'
UNIT = 'wam-qa-quiet.service'
BIN = Path('/usr/local/libexec/wam-qa-sleep')

def cmd(*args, check=True):
    p = subprocess.run(args, text=True, capture_output=True, timeout=1800)
    if check and p.returncode:
        raise RuntimeError(f'{args!r}: {p.returncode}: {p.stdout[-2000:]} {p.stderr[-2000:]}')
    return p.stdout.strip()


def save(name, doc):
    m._save(OUT / (name + '.json'), doc)
    print(json.dumps({'gate': name, 'result': doc}), flush=True)


def settings(**kw):
    m._save(HOME / 'maintenance.json', {'os_security': 'off', **kw})


def run():
    rec = m.run(HOME, wait=0, with_updates=False)
    return rec


def pid(unit=UNIT):
    return cmd('systemctl', 'show', '-p', 'MainPID', '--value', unit)


def stale():
    # The live process retains its deleted executable; real needrestart detects it.
    new = BIN.with_suffix('.new')
    shutil.copyfile('/usr/bin/sleep', new)
    new.chmod(0o755)
    os.replace(new, BIN)


def services():
    names = cmd('systemctl', 'list-units', '--type=service', '--state=running', '--no-legend', '--plain')
    return {line.split()[0]: cmd('systemctl', 'show', '-p', 'MainPID,ExecMainStartTimestampMonotonic', line.split()[0])
            for line in names.splitlines() if line.strip()}


def prepare():
    assert os.geteuid() == 0
    assert cmd('hostname') in ('wos-maint-qa', 'lima-wos-maint-qa'), 'Refuse any other machine'
    os.umask(0o022)  # The suite owner must traverse BASE and the verified Node directory.
    OUT.mkdir(parents=True, exist_ok=True)
    HH.mkdir(parents=True, exist_ok=True)
    os.environ['WIZARD_OS_HOME'] = str(HOME)
    os.environ['HERMES_HOME'] = str(HH)
    os.environ['TMPDIR'] = str(BASE / 'tmp')
    (BASE / 'tmp').mkdir(exist_ok=True)


NATIVE_APT = ('apt-daily.timer', 'apt-daily-upgrade.timer', 'apt-daily.service',
              'apt-daily-upgrade.service', 'unattended-upgrades.service')
NODE_VERSION = '22.22.0'
NODE_SHA256 = '1bf1eb9ee63ffc4e5d324c0b9b62cf4a289f44332dfef9607cea1a0d9596ba6f'


def isolate_native_apt():
    # A native unattended-upgrade restarted the fixture during cycle 27. Stop
    # both queued timers and in-flight services before preparing the baseline.
    cmd('systemctl', 'mask', '--now', *NATIVE_APT)
    for unit in NATIVE_APT:
        assert cmd('systemctl', 'is-enabled', unit, check=False) == 'masked', unit
        assert cmd('systemctl', 'is-active', unit, check=False) in ('inactive', 'failed'), unit
    save('native-apt-isolation', {'masked': list(NATIVE_APT)})


def install_test_node():
    # Match CI's Node 22 (Ubuntu's nodejs is too old for strip-types).
    assert os.uname().machine == 'aarch64', 'This disposable Thales rig is arm64 only'
    name = f'node-v{NODE_VERSION}-linux-arm64'
    archive = BASE / (name + '.tar.xz')
    with urllib.request.urlopen(f'https://nodejs.org/dist/v{NODE_VERSION}/{archive.name}', timeout=120) as response:
        with archive.open('wb') as target:
            shutil.copyfileobj(response, target)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == NODE_SHA256, 'Node archive digest mismatch'
    with tarfile.open(archive) as source:
        source.extractall(BASE, filter='data')
    node_bin = BASE / name / 'bin'
    # Tests build their own PATH from this one; env is also preserved in runuser.
    os.environ['PATH'] = str(node_bin) + ':' + os.environ['PATH']
    version = cmd('node', '--version')
    assert version == 'v' + NODE_VERSION
    cmd('node', '--experimental-strip-types', '--input-type=module', '-e', 'const x = 1;')
    return version


def suite_command(root, home, temporary):
    # Installer unit fixtures assume an ordinary owner, not root. Running them
    # as root called the real usermod and system-scope systemd-run in the VM.
    return ['runuser', '-u', 'wam-suite-qa', '--', 'env', '-i',
            'HOME=' + str(home), 'TMPDIR=' + str(temporary), 'LANG=C.UTF-8',
            'PATH=' + os.environ['PATH'], 'PYTHONPATH=' + str(root),
            'python3', '-m', 'pytest', '-q', str(root / 'tests')]


def run_suite(root, name, log):
    p = subprocess.run(suite_command(root, BASE / 'suite-home', BASE / 'suite-tmp'),
                       cwd=root, text=True, capture_output=True, timeout=1200)
    (OUT / log).write_text(p.stdout + p.stderr)
    save(name, {'returncode': p.returncode, 'tail': (p.stdout + p.stderr)[-5000:]})
    return p.returncode


def pre():
    # All package mutations and systemctl actions run INSIDE this asserted VM.
    os.umask(0o022)
    isolate_native_apt()
    with op._no_service_restarts():
        # A fresh cloud image's first update can exit 0 with index fetches missing (slow ports mirror):
        # retry until every package we install has a candidate, so the install never races stale lists.
        want = ['needrestart', 'python3-pytest', 'python3-yaml', 'python3-jwt', 'sudo', 'git', 'curl', 'xz-utils']
        for _ in range(4):
            cmd('apt-get', '-o', 'Acquire::Retries=5', 'update', '-qq')
            policy = {n: subprocess.run(['apt-cache', 'policy', n], text=True, capture_output=True).stdout for n in want}
            missing = [n for n, out in policy.items() if not out.strip() or 'Candidate: (none)' in out]
            if not missing:
                break
            time.sleep(10)
        else:
            raise RuntimeError(f'apt candidates still missing after retries: {missing}')
        cmd('env', 'DEBIAN_FRONTEND=noninteractive', 'NEEDRESTART_SUSPEND=1', 'apt-get', 'install', '-y', '-q',
            'needrestart', 'python3-pytest', 'python3-yaml', 'python3-jwt', 'sudo', 'git', 'curl', 'xz-utils')
    node_version = install_test_node()
    root = str(Path(__file__).resolve().parents[2])
    bundle = Path(root) / 'source.bundle'
    if bundle.exists():
        # Preserve real tracked-file/HEAD semantics for the repository's portable-path
        # guard, without contacting GitHub or using credentials inside the guest.
        cmd('git', '-C', root, 'init', '-q')
        cmd('git', '-C', root, 'fetch', '-q', str(bundle), 'refs/heads/feature/system-maintenance')
        cmd('git', '-C', root, 'reset', '--mixed', 'FETCH_HEAD')
        combined_root = Path(root) / 'backup-integration'
        if combined_root.exists():
            cmd('git', '-C', str(combined_root), 'init', '-q')
            cmd('git', '-C', str(combined_root), 'fetch', '-q', str(bundle), 'refs/heads/feature/system-maintenance')
            cmd('git', '-C', str(combined_root), 'reset', '--mixed', 'FETCH_HEAD')
            cmd('git', '-C', str(combined_root), 'add', '-A', '-f', '.')
            save('source', {'candidate': cmd('git', '-C', root, 'rev-parse', 'HEAD'),
                            'integration_tree': cmd('git', '-C', str(combined_root), 'write-tree')})
    cmd('useradd', '-m', '-d', str(BASE / 'suite-home'), 'wam-suite-qa')
    (BASE / 'suite-tmp').mkdir()
    cmd('chown', '-R', 'wam-suite-qa:wam-suite-qa', root, str(BASE / 'suite-home'), str(BASE / 'suite-tmp'))
    save('suite-environment', {'user': cmd('runuser', '-u', 'wam-suite-qa', '--', 'id', '-u'),
                               'node': node_version, 'clean_environment': True})
    tests_failed = run_suite(Path(root), 'full-suite', 'pytest.log')
    integration = Path(root) / 'backup-integration'
    assert integration.is_dir(), 'Missing backup integration export; both suites are required'
    integration_failed = run_suite(integration, 'backup-integration-suite', 'backup-integration-pytest.log')
    # Test failures must not be treated as ancillary evidence or proceed to reboot.
    assert not tests_failed and not integration_failed, 'Candidate/integration test suite failed; see retained logs'
    # Seed a real reboot-required condition without consuming the held platform
    # candidates. Do not bulk-upgrade non-held reverse dependencies of systemd.
    with op._no_service_restarts():
        cmd('env', 'DEBIAN_FRONTEND=noninteractive', 'NEEDRESTART_SUSPEND=1', 'apt-get', 'install',
            '-y', '-q', '--only-upgrade', 'linux-virtual', 'linux-image-virtual', 'linux-headers-virtual')
    baseline = op.check()
    save('security-baseline', baseline)
    assert baseline.get('held') and baseline.get('reboot_required'), 'Fresh image lacks held/reboot baseline'
    # Keep package setup out of the fixture's controlled observation interval.
    isolate_native_apt()
    BIN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile('/usr/bin/sleep', BIN); BIN.chmod(0o755)
    Path('/etc/systemd/system/' + UNIT).write_text('[Unit]\nDescription=Disposable quiet maintenance proof\n'
        '[Service]\nExecStart=' + str(BIN) + ' infinity\nRestart=no\n[Install]\nWantedBy=multi-user.target\n')
    cmd('systemctl', 'daemon-reload'); cmd('systemctl', 'enable', '--now', UNIT)
    stale()
    scan = op.restart_check()
    save('needrestart-discovery', scan)
    assert UNIT in scan['services'], 'real needrestart did not discover the stale executable'

    m._save(HOME / 'maintenance.json', {})  # actual defaults: check OS, report-only restart lane
    before = {'boot': cmd('cat', '/proc/sys/kernel/random/boot_id'), 'services': services(),
              'packages': cmd('dpkg-query', '-W', '-f=${Package} ${Version}\n')}
    report = run()
    after = {'boot': cmd('cat', '/proc/sys/kernel/random/boot_id'), 'services': services(),
             'packages': cmd('dpkg-query', '-W', '-f=${Package} ${Version}\n')}
    save('report-only', {'unchanged': before == after, 'before': before, 'after': after, 'run': report})
    assert before == after, 'default report-only mutated services/packages/boot'

    db = sqlite3.connect(HH / 'state.db')
    db.execute('create table if not exists session_turn_leases(expires_at real)')
    db.execute('delete from session_turn_leases')
    db.execute('insert into session_turn_leases values (?)', (time.time() + 3600,))
    db.commit()
    settings(quiet_restarts='auto', quiet_reboot='auto', held_platform='patch', os_security='patch')
    before_pid = pid()
    before_boot = cmd('cat', '/proc/sys/kernel/random/boot_id')
    blocked = run()
    save('active-turn-block', {'pid_before': before_pid, 'pid_after': pid(), 'busy': m.busy_reasons(HOME), 'run': blocked})
    assert blocked['state'] == 'deferred' and pid() == before_pid
    assert before_boot == cmd('cat', '/proc/sys/kernel/random/boot_id')
    assert blocked['restarts']['held']['state'] == 'deferred'
    assert all(r['state'] == 'deferred' for r in blocked['restarts']['services']['results'])
    db.execute('delete from session_turn_leases'); db.commit(); db.close()

    # Isolate service restarts first; do not reboot until every other proof is durable.
    settings(quiet_restarts='auto')
    idle = run()
    save('idle-service-restart', {'pid_before': before_pid, 'pid_after': pid(), 'run': idle})
    assert pid() not in ('0', before_pid)
    assert any(r.get('service') == UNIT and r['state'] == 'restarted' for r in idle['restarts']['services']['results'])
    assert UNIT not in op.restart_check()['services']

    cycles = []
    for index in range(51):
        old = pid(); stale()
        result = run()
        new = pid()
        entry = {'index': index + 1, 'before': old, 'after': new,
                 'results': result['restarts']['services'].get('results', []),
                 'active': cmd('systemctl', 'is-active', UNIT, check=False)}
        cycles.append(entry)
        save('restart-cycles', {'completed': len(cycles), 'cycles': cycles})
        valid = (old != new and new != '0' and entry['active'] == 'active'
                 and any(r.get('service') == UNIT and r['state'] == 'restarted' for r in entry['results']))
        if not valid:
            save('cycle-failure', {'run': result, 'unit': cmd('systemctl', 'show', UNIT),
                 'journal': cmd('journalctl', '-u', UNIT, '-u', 'apt-daily-upgrade.service', '-n', '100', '--no-pager'),
                 'fresh_scan': op.restart_check()})
        assert valid, f'Restart cycle {index + 1} failed; see cycle-failure.json'

    # Real failure: a stale executable whose unit's next ExecStart fails once; recovery
    # starts the original executable again. Later services still get their own attempt.
    fail_unit = 'wam-qa-a-fail.service'
    fail_bin = Path('/usr/local/libexec/wam-qa-fail-sleep')
    shutil.copyfile('/usr/bin/sleep', fail_bin); fail_bin.chmod(0o755)
    Path('/etc/systemd/system/' + fail_unit).write_text('[Service]\nExecStart=' + str(fail_bin) + ' infinity\nRestart=no\n')
    cmd('systemctl', 'daemon-reload'); cmd('systemctl', 'start', fail_unit)
    new = fail_bin.with_suffix('.new'); shutil.copyfile('/usr/bin/sleep', new); new.chmod(0o755); os.replace(new, fail_bin)
    # ExecStartPre consumes the marker, fails exactly once; systemctl start recovery succeeds.
    hook = BASE / 'fail-once.sh'
    hook.write_text('#!/bin/sh\nif [ -f ' + str(BASE / 'fail-once') + ' ]; then rm ' + str(BASE / 'fail-once') + '; exit 1; fi\n')
    hook.chmod(0o755)
    drop = Path('/etc/systemd/system/' + fail_unit + '.d'); drop.mkdir(exist_ok=True)
    (drop / 'proof.conf').write_text('[Service]\nExecStartPre=' + str(hook) + '\n')
    (BASE / 'fail-once').touch(); cmd('systemctl', 'daemon-reload'); stale()
    failure = run()
    save('failure-and-recovery', {'run': failure, 'health': m.health(HOME),
        'failed_service_active': cmd('systemctl', 'is-active', fail_unit, check=False)})
    results = failure['restarts']['services']['results']
    assert any(r.get('service') == fail_unit and r['state'] == 'error' and r.get('recovered') for r in results)
    assert any(r.get('service') == UNIT and r['state'] == 'restarted' for r in results)
    cmd('systemctl', 'stop', fail_unit)
    cmd('systemctl', 'reset-failed', fail_unit, check=False)

    # Run the real held security transaction separately. VM still has the platform
    # packages deliberately held by slice 1; retain exact before/after candidates.
    pending = op.check(refresh=True)
    settings(os_security='patch', held_platform='patch')
    held = run()
    save('held-platform', {'before': pending, 'run': held, 'after': op.check(),
                           'guard_removed': not op.POLICY_RC.exists()})
    assert pending.get('held'), 'VM no longer has held candidates; recreate seeded baseline for this gate'
    assert held['restarts']['held']['state'] == 'applied'
    assert held['restarts']['held']['installed']
    assert not op.POLICY_RC.exists()

    # Exercise the installed fixed helper as a restricted non-root owner too.
    user = 'wam-quiet-qa'
    if subprocess.run(['id', user], capture_output=True).returncode:
        cmd('useradd', '-m', user)
    setup = m.setup(user=user)
    allowed = cmd('runuser', '-u', user, '--', 'sudo', '-n', str(m.HELPER), 'restart-check')
    denied = subprocess.run(['runuser', '-u', user, '--', 'sudo', '-n', str(m.HELPER), 'restart-service',
                             'wam-qa-quiet.service', '--extra'], capture_output=True, text=True)
    arbitrary = subprocess.run(['runuser', '-u', user, '--', 'sudo', '-n', '/bin/true'], capture_output=True, text=True)
    save('privilege-boundary', {'setup': setup, 'check': json.loads(allowed), 'extra_arg_exit': denied.returncode,
                               'arbitrary_exit': arbitrary.returncode})
    assert denied.returncode != 0 and arbitrary.returncode != 0

    assert op.detect().reboot_required(), 'No reboot-required condition available for real reboot proof'
    settings(quiet_reboot='auto')
    reboot = run()
    save('reboot-request', {'run': reboot, 'receipt': m._load(HOME / 'maintenance/reboot.json'),
                            'health': m.health(HOME)})
    assert reboot['restarts']['reboot']['state'] == 'scheduled'
    (OUT / 'pre-complete').write_text('ready for boot verification\n')


def post():
    state = cmd('systemctl', 'is-system-running', check=False)
    health = m.health(HOME)
    row = next(r for r in health['items'] if r['key'] == 'maintenance-reboot')
    receipt = m._load(HOME / 'maintenance/reboot.json')
    save('reboot-return', {'system_state': state, 'boot_id': cmd('cat', '/proc/sys/kernel/random/boot_id'),
                          'receipt': receipt, 'health': health,
                          'qa_service': cmd('systemctl', 'is-active', UNIT, check=False),
                          'failed_units': cmd('systemctl', '--failed', '--no-legend', '--plain')})
    assert receipt['state'] == 'rebooted' and receipt['new_boot_id'] != receipt['boot_id']
    assert row['value'] == 'rebooted' and row['status'] == 'ok'
    assert state == 'running' and cmd('systemctl', 'is-active', UNIT) == 'active'
    for name in ('full-suite', 'backup-integration-suite'):
        assert json.loads((OUT / (name + '.json')).read_text())['returncode'] == 0, name
    cycles = json.loads((OUT / 'restart-cycles.json').read_text())
    assert cycles['completed'] == len(cycles['cycles']) == 51
    save('acceptance', {'passed': True, 'restart_cycles': cycles['completed'], 'attended_enablement': 'not performed'})


if __name__ == '__main__':
    prepare()
    if sys.argv[1:] == ['pre']:
        pre()
    elif sys.argv[1:] == ['post']:
        post()
    else:
        raise SystemExit('pre|post required')
