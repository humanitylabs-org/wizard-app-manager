#!/usr/bin/env python3
"""Slice 5 guest assertions. Only run as the owner in a disposable Debian guest."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

assert os.getuid() == 0
CHECKOUT = Path('/var/lib/wizard-os/wizard-app-manager')
HOME = Path('/var/lib/wizard-os/.wizard-os')
HELPER = '/usr/local/libexec/wizard-os/server-access'
POLICY = Path('/etc/sudoers.d/91-wizard-agent-access')
ENV = {**os.environ, 'WIZARD_OS_HOME': str(HOME), 'HOME': '/var/lib/wizard-os',
       'RUNTIPI_ROOT': '/var/lib/wizard-os/runtipi'}
RESULTS = []


def run(argv, *, code: int | None = 0, env=None, data=None):
    p = subprocess.run(argv, cwd=CHECKOUT, env=env, input=data, capture_output=True, text=True, timeout=120)
    row = {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    RESULTS.append(row); print(json.dumps(row), flush=True)
    if code is None: assert p.returncode != 0, row
    else: assert p.returncode == code, row
    return p.stdout


def owner(*args):
    # This root-entered CLI is the documented box-local human fallback, not an
    # identity header, service token, network credential or agent-owned script.
    return json.loads(run(['/usr/bin/python3', '-m', 'wam.cli', 'developer', *args], env=ENV))


def sudo(expected):
    run(['runuser', '-u', 'hermes', '--', 'sudo', '-n', 'true'], code=0 if expected else None)


def status():
    return json.loads(run(['runuser', '-u', 'wizard-system', '--', 'sudo', '-n', HELPER, 'status']))


def denied_agent_requests():
    script = r'''
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
h=Path.home()/'.wizard-os'; token=(h/'agent-token').read_text().strip()
for path,body in [('/api/developer', {'enabled': True, 'repo': 'https://github.com/humanitylabs-org/wizard-app-manager', 'ref': 'main'}),
                  ('/api/developer/server-access', {'action':'grant'}), ('/api/developer/apply', {})]:
 req=Request('http://127.0.0.1:7790'+path, data=json.dumps(body).encode(), headers={
   'Authorization':'Bearer '+token, 'Content-Type':'application/json', 'Tailscale-User-Login':'owner@example.test',
   'Origin':'https://box.test','X-Wizard-Store':'1'})
 try: urlopen(req)
 except HTTPError as exc: assert exc.code==403 and json.load(exc)['error']=='owner_only'
 else: raise AssertionError('agent token accepted on '+path)
# No bearer shortcut on the owner Approve listener, even forged owner headers.
req=Request('http://127.0.0.1:7791/_approve/developer', data=b'{"action":"grant"}', headers={
 'Content-Type':'application/json','Tailscale-User-Login':'owner@example.test','Origin':'https://box.test','X-Wizard-Store':'1'})
try: urlopen(req)
except HTTPError as exc: assert exc.code==403
else: raise AssertionError('forged Approve headers accepted')
print('PASS agent token and forged owner headers refused')
'''
    run(['runuser', '-u', 'hermes', '--', 'env', 'HOME=/home/hermes', 'python3', '-c', script])


phase = sys.argv[1]
if phase == 'before':
    sudo(False); denied_agent_requests()
    run(['/usr/bin/python3', '-m', 'wam.cli', 'developer', 'grant'], env=ENV, code=None)
    assert not POLICY.exists()
    for action in ('grant', 'revoke', 'status'):
        run(['runuser', '-u', 'wizard-system', '--', 'sudo', '-n', HELPER, action, 'extra'], code=None)
    assert not POLICY.exists()
elif phase == 'grant':
    assert owner('on', '--ref', 'feature/agent-privilege-separation')['enabled']
    doc = owner('grant', '--seconds', '120')
    assert doc['active'] and doc['severity'] == 'red' and not doc['drift']
    sudo(True)
    assert 'hermes' in Path('/var/log/wizard-os-agent-sudo.log').read_text()
    assert 'COMMAND=' in Path('/var/log/wizard-os-agent-sudo.log').read_text()
    assert POLICY.stat().st_mode & 0o777 == 0o440
    run(['/usr/sbin/visudo', '-c'])
    # Manager API, not a locally invented row, supplies Health severity.
    script = '''import json; from urllib.request import Request,urlopen
from pathlib import Path
h=Path.home()/'.wizard-os'
r=Request('http://127.0.0.1:7790/api/doctor',headers={'Authorization':'Bearer '+(h/'agent-token').read_text().strip()})
doc=json.load(urlopen(r)); print(json.dumps(doc))
assert any(x['check'].startswith('Agent has full server access until ') and not x['ok'] and x['severity']=='red' for x in doc)
'''
    run(['runuser', '-u', 'hermes', '--', 'env', 'HOME=/home/hermes', 'python3', '-c', script])
    baseline = status()
    driftfile = Path('/etc/sudoers.d/89-slice5-drift')
    driftfile.write_text('# deliberate root-owned drift probe\n'); driftfile.chmod(0o440)
    try:
        assert str(driftfile) in status()['drift']
    finally: driftfile.unlink()
    assert status()['drift'] == baseline['drift']
    # Layout is unchanged, even while the agent deliberately holds root access.
    for unit in ('wizard-runtipi', 'wizard-app-manager-api'):
        assert run(['systemctl', 'show', unit, '-p', 'User', '--value']).strip() == 'wizard-system'
    denied_agent_requests()
elif phase == 'expiry':
    # Exercise the actual persistent root timer, not helper status on a fake clock.
    end = time.monotonic() + 160
    while POLICY.exists() and time.monotonic() < end: time.sleep(2)
    assert not POLICY.exists(), 'root timer did not remove the expired grant'
    sudo(False)
    assert not status()['active']
elif phase == 'off':
    assert owner('grant', '--seconds', '120')['active']
    sudo(True)
    doc = owner('off', '--setting-only')
    assert not doc['enabled'] and doc['source']['ref'].startswith('releases/')
    assert not doc['server_access']['active'] and not POLICY.exists()
    sudo(False); denied_agent_requests()
else:
    raise AssertionError('unknown phase')
print(json.dumps({'phase': phase, 'passed': True}))
