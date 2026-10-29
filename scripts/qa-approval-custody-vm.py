#!/usr/bin/env python3
"""STAGED ONLY: execute as root INSIDE a fresh disposable systemd VM.

No host admission, VM launch, Docker mutation, real tailscale login, or personal
credential is performed here. The parent must queue this inside its owned VM and
stop that VM afterwards. This proves kernel/systemd custody with a synthetic
Serve configuration and explicitly synthetic identity, NOT authenticated tailnet
acceptance. Requires python3-yaml for the actual CLI negative startup case.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
FIXTURE = Path('/var/lib/wizard-custody-fixture')
STATE = Path('/var/lib/wizard-custody-manager/.wizard-os')
ROOT_STAGE = Path('/root/wizard-custody-fixture')
SOCKET = 'wizard-owner-approval.socket'
SERVICE = 'wizard-app-manager-api.service'
REPORT = []
CHILDREN = []


def run(*argv, ok=True, user=None):
    args = [str(a) for a in argv]
    if user:
        args = ['runuser', '-u', user, '--', 'env', '-i', 'PATH=/usr/bin:/bin',
                'HOME=/var/lib/wizard-custody-manager', 'PYTHONPATH=' + str(SOURCE),
                'WIZARD_OS_HOME=' + str(STATE), *args]
    r = subprocess.run(args, capture_output=True, text=True, timeout=45, cwd=SOURCE)
    if ok and r.returncode:
        # Synthetic fixture only, no secrets. Bound diagnostics for real failures.
        raise RuntimeError(f'{args[0]} exit={r.returncode}: {r.stderr[-1500:]}')
    return r


def check(name, condition):
    REPORT.append({'case': name, 'ok': bool(condition)})
    if FIXTURE.exists():
        (FIXTURE / 'report.json').write_text(json.dumps(REPORT, indent=2))
    if not condition:
        raise AssertionError(name)


def wait(pred, seconds=12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred(): return
        time.sleep(.05)
    raise TimeoutError('bounded fixture wait failed')


def prop(unit, key):
    return run('systemctl', 'show', unit, '-p', key, '--value').stdout.strip()


def route():
    return json.loads((FIXTURE / 'serve.json').read_text()).get('Web', {})


def manager(code):
    return run('/usr/bin/python3', '-c', code, user='wizard-system')


def listener_inode():
    return int(manager('from wam.approval_custody import require_custody; print(require_custody())').stdout)


def low_bind():
    code = '''import errno,socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEPORT,1)
try: s.bind(('127.0.0.1',7792)); s.listen(); print('BOUND')
except OSError as e:
 if e.errno != errno.EADDRINUSE: raise
 print('HELD')
'''
    return run('/usr/bin/python3', '-c', code, user='nobody').stdout.strip()


def dropin(execstart, restart='no'):
    p = Path('/etc/systemd/system') / (SERVICE + '.d/90-fixture.conf')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[Service]\nExecStart=\nExecStart=' + execstart + '\nRestart=' + restart + '\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'reset-failed', SERVICE)


def main():
    if (os.getuid() != 0 or not Path('/run/wizard-disposable-privsep').is_file() or
            run('systemd-detect-virt', '--vm', ok=False).returncode != 0):
        raise SystemExit('refused: parent-marked disposable VM required')
    if FIXTURE.exists() or ROOT_STAGE.exists() or Path('/etc/systemd/system/' + SOCKET).exists():
        raise SystemExit('refused: fresh isolated fixture required')
    ts = Path('/var/lib/tailscale/tailscaled.state')
    if ts.exists() and ts.stat().st_size:
        raise SystemExit('refused: possible real Tailscale state')
    # Do not replace a real installed node/service in a candidate-used VM.
    if Path('/etc/systemd/system/' + SERVICE).exists():
        raise SystemExit('refused: existing App Manager; use a fresh disposable guest')
    run('useradd', '--system', '--create-home', '--home-dir', '/var/lib/wizard-custody-manager', 'wizard-system')
    u = pwd.getpwnam('wizard-system')
    check('manager_and_probe_are_distinct_nonroot_uids', u.pw_uid not in (0, pwd.getpwnam('nobody').pw_uid))
    FIXTURE.mkdir(mode=0o700); os.chown(FIXTURE, u.pw_uid, u.pw_gid)
    STATE.mkdir(mode=0o700, parents=True); os.chown(STATE.parent, u.pw_uid, u.pw_gid); os.chown(STATE, u.pw_uid, u.pw_gid)
    ROOT_STAGE.mkdir(mode=0o700)
    module = ROOT_STAGE / 'approval_custody.py'
    shutil.copyfile(SOURCE / 'wam/approval_custody.py', module)
    # Explicit synthetic Tailscale API, no tailnet connectivity or identity claim.
    stub = Path('/usr/bin/tailscale')
    stub.write_text('''#!/usr/bin/python3
import json,os,sys,time
from pathlib import Path
p=Path('/var/lib/wizard-custody-fixture/serve.json')
a=sys.argv[1:]
d=json.loads(p.read_text()) if p.exists() else {'Web':{}}
if a==['status','--json']:
 print(json.dumps({'Self':{'UserID':1},'User':{'1':{'LoginName':'synthetic-owner@example.invalid'}}}))
elif a==['serve','status','--json']:
 with p.with_name('events.jsonl').open('a') as f: f.write(json.dumps({'event':'status','empty':not d['Web'],'ns':time.monotonic_ns()})+'\\n')
 print(json.dumps(d))
elif a and a[0]=='serve':
 port=next(x.split('=',1)[1] for x in a if x.startswith('--https='))
 host='fixture.example.ts.net:'+port
 if a[-1]=='off':
  if not p.with_name('refuse-removal').exists(): d['Web'].pop(host,None)
 else: d['Web'][host]={'Handlers':{'/':{'Proxy':a[-1]}}}
 p.write_text(json.dumps(d))
else: raise SystemExit(2)
'''); stub.chmod(0o755)
    (FIXTURE / 'serve.json').write_text('{"Web":{}}')
    (FIXTURE / 'events.jsonl').touch()
    for f in ('serve.json', 'events.jsonl'):
        os.chown(FIXTURE / f, u.pw_uid, u.pw_gid)
        (FIXTURE / f).chmod(0o600)
    for name in ('tailscaled', 'docker'):
        unit = Path('/etc/systemd/system/' + name + '.service')
        unit.write_text('[Unit]\nDescription=Disposable synthetic fixture\n[Service]\nExecStart=/usr/bin/sleep infinity\n')
    run('systemctl', 'daemon-reload'); run('systemctl', 'start', 'tailscaled', 'docker')
    installer = SOURCE / 'scripts/install-approval-custody.py'
    args = ['/usr/bin/python3', '-I', str(installer), 'install', 'wizard-system', str(STATE), str(SOURCE), str(module)]
    # Before enable: a local UID already holding the address must cause refusal,
    # not adoption. No approval Serve mutation may occur.
    hostile = subprocess.Popen(['runuser', '-u', 'nobody', '--', '/usr/bin/python3', '-c',
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',7792)); s.listen(); print('READY',flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    CHILDREN.append(hostile)
    check('occupied_probe_ready', hostile.stdout.readline().strip() == 'READY')
    check('occupied_before_enable_refused', run(*args, ok=False).returncode != 0 and not route())
    denied = run('/usr/bin/python3', '-c',
                 "from wam.owner_confirm import enable; from pathlib import Path; enable(Path('" + str(STATE) + "'), 'fixture.example.ts.net')",
                 user='wizard-system', ok=False)
    check('occupied_address_publication_refused', denied.returncode != 0 and not route() and hostile.poll() is None)
    os.killpg(hostile.pid, signal.SIGKILL); hostile.wait(timeout=5)
    run('systemctl', 'reset-failed', SOCKET)
    run(*args)
    check('custody_before_publication', low_bind() == 'HELD' and not route())
    inode = listener_inode()
    worker = Path('/opt/wizard-custody-fixture-worker.py')
    worker.write_text('''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from wam import approval_custody as ac
from wam.cli import ApprovalHandler
ApprovalHandler.cfg=SimpleNamespace(home=Path(os.environ['WIZARD_OS_HOME']))
s=ac.http_server(ApprovalHandler)
(ApprovalHandler.cfg.home/'worker.json').write_text(json.dumps({'pid':os.getpid(),'uid':os.getuid(),'inode':ac.require_custody()}))
s.serve_forever()
''')
    prefix = '/usr/bin/env PYTHONPATH=' + str(SOURCE) + ' /usr/bin/python3 '
    dropin(prefix + str(worker))
    # Continuous capture-negative probe runs throughout all worker transitions.
    hammer = subprocess.Popen(['runuser', '-u', 'nobody', '--', '/usr/bin/python3', '-c', '''import errno,json,socket,time
from pathlib import Path
attempts=0; captured=[]; end=time.monotonic()+120
while time.monotonic()<end and not Path('/run/wizard-custody-probe-done').exists():
 s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEPORT,1)
 try: s.bind(('127.0.0.1',7792)); s.listen(); captured.append(time.monotonic_ns())
 except OSError as e:
  if e.errno!=errno.EADDRINUSE: raise
 finally: s.close()
 attempts+=1; time.sleep(.005)
print(json.dumps({'attempts':attempts,'captured':captured,'stopped_by_parent':Path('/run/wizard-custody-probe-done').exists()}))
'''], stdout=subprocess.PIPE, text=True, start_new_session=True)
    CHILDREN.append(hammer)
    run('systemctl', 'start', SERVICE)
    wait(lambda: (STATE / 'worker.json').exists())
    first = json.loads((STATE / 'worker.json').read_text())
    check('nonroot_worker_inherits_root_socket', first['uid'] == u.pw_uid and first['inode'] == inode)
    manager("from wam.owner_confirm import enable; enable(__import__('pathlib').Path('" + str(STATE) + "'), 'fixture.example.ts.net')")
    check('publication_after_custody', bool(route()))
    run('systemctl', 'kill', '--signal=KILL', '--kill-whom=main', SERVICE)
    wait(lambda: prop(SERVICE, 'ActiveState') == 'failed')
    check('abrupt_exit_retains_address', low_bind() == 'HELD' and bool(route()) and listener_inode() == inode)
    run('systemctl', 'reset-failed', SERVICE); run('systemctl', 'start', SERVICE)
    wait(lambda: json.loads((STATE / 'worker.json').read_text())['pid'] != first['pid'])
    check('restart_same_socket_inode', listener_inode() == inode and low_bind() == 'HELD')
    run('systemctl', 'stop', SERVICE)
    check('intentional_worker_stop_keeps_route_and_socket', low_bind() == 'HELD' and bool(route()))
    # Update-shaped replacement: worker bytes change while PID 1 retains custody.
    worker.write_text(worker.read_text() + '\n# synthetic worker update revision\n')
    dropin(prefix + str(worker), restart='always')
    run('systemctl', 'start', SERVICE)
    before_update = int(prop(SERVICE, 'MainPID'))
    manager('from wam.approval_custody import restart_worker; restart_worker()')
    wait(lambda: int(prop(SERVICE, 'MainPID') or '0') not in (0, before_update))
    check('same_uid_update_restart_retains_address', listener_inode() == inode and low_bind() == 'HELD')
    check('manual_socket_stop_refused', run('systemctl', 'stop', SOCKET, ok=False).returncode != 0 and low_bind() == 'HELD')
    # Actual CLI startup must fail before the generic manager listeners on a
    # duplicate-FD activation declaration. No mocked activation function.
    run('systemctl', 'stop', SERVICE)
    dropin(prefix.replace(' /usr/bin/python3 ', ' LISTEN_FDS=2 /usr/bin/python3 ') + '-m wam serve')
    run('systemctl', 'start', SERVICE, ok=False)
    wait(lambda: prop(SERVICE, 'ActiveState') == 'failed')
    check('failed_worker_still_cannot_be_captured', low_bind() == 'HELD' and listener_inode() == inode and bool(route()))
    check('invalid_activation_exit_nonzero', prop(SERVICE, 'ExecMainStatus') != '0')
    check('start_and_trigger_limits_cannot_release_custody', prop(SERVICE, 'StartLimitIntervalUSec') == '0' and prop(SOCKET, 'TriggerLimitIntervalUSec') == '0')
    for _ in range(8):
        run('systemctl', 'start', SERVICE, ok=False)
        wait(lambda: prop(SERVICE, 'ActiveState') == 'failed')
        check('repeated_failed_start_keeps_address_' + str(_), low_bind() == 'HELD' and listener_inode() == inode)
    listeners = [int(row.split()[1].split(':')[1], 16)
                 for row in Path('/proc/net/tcp').read_text().splitlines()[1:]
                 if row.split()[3] == '0A']
    check('invalid_activation_starts_no_generic_listener', not ({7790, 7791} & set(listeners)))
    # A correct name/count with the WRONG live FD still fails kernel identity.
    badfd = Path('/opt/wizard-custody-wrongfd.py')
    badfd.write_text('import os,runpy,socket\ns=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); os.dup2(s.fileno(),3)\nrunpy.run_path(' + repr(str(worker)) + ',run_name="__main__")\n')
    dropin(prefix + str(badfd)); run('systemctl', 'start', SERVICE, ok=False)
    wait(lambda: prop(SERVICE, 'ActiveState') == 'failed')
    check('wrong_actual_activation_fd_refused', prop(SERVICE, 'ExecMainStatus') != '0' and low_bind() == 'HELD' and listener_inode() == inode)
    # A changed loaded socket definition is not accepted as the exact unit.
    socket_dropin = Path('/etc/systemd/system/' + SOCKET + '.d/90-wrong.conf')
    socket_dropin.parent.mkdir(parents=True, exist_ok=True)
    socket_dropin.write_text('[Socket]\nBacklog=128\n')
    run('systemctl', 'daemon-reload')
    r = run('/usr/bin/python3', '-c', 'from wam.approval_custody import require_custody; require_custody()', user='wizard-system', ok=False)
    check('changed_loaded_socket_identity_refused', r.returncode != 0 and low_bind() == 'HELD')
    socket_dropin.unlink(); run('systemctl', 'daemon-reload')
    # Restore the exact fixture worker and test synthetic identity negatives.
    dropin(prefix + str(worker)); run('systemctl', 'start', SERVICE)
    http = '''import http.client
c=http.client.HTTPConnection('127.0.0.1',7792,timeout=3)
c.request('GET','/confirm/'+'a'*32,headers={'Host':'fixture.example.ts.net:8446','Tailscale-User-Login':'synthetic-owner@example.invalid'})
r=c.getresponse(); print(r.status); r.read()
'''
    check('low_uid_forged_identity_refused', run('/usr/bin/python3', '-c', http, user='nobody').stdout.strip() == '403')
    check('root_wrong_synthetic_login_refused', run('/usr/bin/python3', '-c', http.replace('synthetic-owner@example.invalid', 'other@example.invalid')).stdout.strip() == '403')
    # failed removal acknowledgement must leave custody and route untouched.
    flag = FIXTURE / 'refuse-removal'; flag.touch()
    remove_args = [*args]; remove_args[3] = 'remove'
    check('unacknowledged_removal_retains_custody', run(*remove_args, ok=False).returncode != 0 and bool(route()) and low_bind() == 'HELD')
    flag.unlink()
    # End continuous negative probe before explicit authorized socket removal.
    Path('/run/wizard-custody-probe-done').touch()
    wait(lambda: hammer.poll() is not None, seconds=5)
    h = json.loads(hammer.stdout.read()); check('continuous_capture_never_succeeds', h['attempts'] > 100 and not h['captured'] and h['stopped_by_parent'])
    run(*remove_args)
    check('route_absent_before_custody_release', not route())
    check('tailscaled_stopped_before_release', prop('tailscaled.service', 'ActiveState') == 'inactive')
    check('bind_positive_control_after_explicit_safe_release', low_bind() == 'BOUND')
    check('no_agent_root_assignment', u.pw_uid != 0)
    print(json.dumps({'scope':'synthetic-systemd-kernel-only','authenticated_tailnet_tested':False,
                      'source_custody_sha256':hashlib.sha256(module.read_bytes()).hexdigest(),
                      'cases':REPORT,'case_count':len(REPORT),'hammer':h}, indent=2))


if __name__ == '__main__':
    try:
        main()
    finally:
        for p in CHILDREN:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=5)
        # VM destruction belongs to the parent; never remove a host resource.
