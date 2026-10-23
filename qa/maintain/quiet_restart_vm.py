#!/usr/bin/env python3
"""Destructive quiet-restart QA. ONLY wos-maint-qa (disposable Lima guest).
Invoked by the durable Thales worker, never on the host or an owner's install.
"""
import json
import os
import shutil
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
    OUT.mkdir(parents=True, exist_ok=True)
    HH.mkdir(parents=True, exist_ok=True)
    os.environ['WIZARD_OS_HOME'] = str(HOME)
    os.environ['HERMES_HOME'] = str(HH)
    os.environ['TMPDIR'] = str(BASE / 'tmp')
    (BASE / 'tmp').mkdir(exist_ok=True)


def pre():
    # All package mutations and systemctl actions run INSIDE this asserted VM.
    with op._no_service_restarts():
        cmd('apt-get', 'update', '-qq')
        cmd('env', 'DEBIAN_FRONTEND=noninteractive', 'NEEDRESTART_SUSPEND=1', 'apt-get', 'install', '-y', '-q',
            'needrestart', 'python3-pytest', 'python3-yaml', 'sudo')
    root = str(Path(__file__).resolve().parents[2])
    tests = subprocess.run(['python3', '-m', 'pytest', '-q', root + '/tests'], cwd=root,
                           text=True, capture_output=True, timeout=1200, env={**os.environ, 'PYTHONPATH': root})
    (OUT / 'pytest.log').write_text(tests.stdout + tests.stderr)
    save('full-suite', {'returncode': tests.returncode, 'tail': (tests.stdout + tests.stderr)[-5000:]})
    integration = Path(root) / 'backup-integration'
    integration_failed = False
    if integration.exists():
        combined = subprocess.run(['python3', '-m', 'pytest', '-q', str(integration / 'tests')], cwd=integration,
                                  text=True, capture_output=True, timeout=1200,
                                  env={**os.environ, 'PYTHONPATH': str(integration)})
        (OUT / 'backup-integration-pytest.log').write_text(combined.stdout + combined.stderr)
        save('backup-integration-suite', {'returncode': combined.returncode,
                                          'tail': (combined.stdout + combined.stderr)[-5000:]})
        integration_failed = combined.returncode != 0
    # Keep going after a test-suite failure to collect the independent VM gates.
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
        assert old != new and new != '0' and entry['active'] == 'active'
        assert any(r.get('service') == UNIT and r['state'] == 'restarted' for r in entry['results'])

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
    if tests.returncode or integration_failed:
        (OUT / 'suite-failed').write_text('See pytest.log and backup-integration-pytest.log\n')


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
    assert not (OUT / 'suite-failed').exists(), 'VM gates passed but full unit suite failed: see pytest.log'
    save('acceptance', {'passed': True, 'restart_cycles': 51, 'attended_enablement': 'not performed'})


if __name__ == '__main__':
    prepare()
    if sys.argv[1:] == ['pre']:
        pre()
    elif sys.argv[1:] == ['post']:
        post()
    else:
        raise SystemExit('pre|post required')
