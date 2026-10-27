#!/usr/bin/env python3
"""David-case Debian 12/KVM acceptance, admitted Linux worker only.

Usage: run.py EVIDENCE_DIR SOURCE_EXPORT BASELINE_SHA CANDIDATE_SHA [--smoke]
SOURCE_EXPORT contains the exact candidate tree; baseline is fetched by SHA in
its own guest. No model/tailnet credential, host mounts or live box operations.
The smoke flag boots only the candidate and skips the full suite/comparison.
"""
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

# Main's documented box-only alternative isolates the proven missing user-bus
# handoff bug from unauthenticated Tailscale operator state. Candidate keeps the
# real daemon/operator denial under test. Both use documented start/status.
INSTALL_PRIVATE_ACCESS = {'baseline': 'none', 'candidate': 'tailscale'}
INSTALL_INPUT_DIFFERENCE = (
    'Baseline uses documented WIZARD_PRIVATE_ACCESS=none; candidate keeps '
    'WIZARD_PRIVATE_ACCESS=tailscale for real operator provisioning and agent '
    'mutation denial. Both use pinned get.sh via bash -s start/status with '
    'WIZARD_AGENT_SAFE=1. Authenticated private links are not tested.'
)
KNOWN_BASELINE_SHA = '67141228f6f73516ab68ece1c4c72bf3f67aebe0'
BASELINE_REASON = 'named_host_ready requires /run/user/1000/bus absent after successful host preparation'



root, source = (Path(v).resolve() for v in sys.argv[1:3])
baseline, candidate = sys.argv[3:5]
smoke = '--smoke' in sys.argv[5:]
assert all(re.fullmatch('[0-9a-f]{40}', v) for v in (baseline, candidate))
assert str(root).startswith('/srv/work/privsep/slice3/') and not root.exists()
assert json.loads((source.parent / 'source-export.json').read_text())['head_sha'] == candidate
root.mkdir(mode=0o700, parents=True)
private = root / 'private'; private.mkdir(mode=0o700)
receipt = {'started': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'baseline': baseline,
           'candidate': candidate, 'smoke': smoke, 'steps': [], 'vms': [], 'failures': [],
           'candidate_passed': False, 'baseline_status': 'not run',
           'baseline_failure_matches_documented': False,
           'install_private_access': INSTALL_PRIVATE_ACCESS,
           'install_input_difference': INSTALL_INPUT_DIFFERENCE,
           'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
# Reuse the proven Slice 1 registry mirror, install retry, SSH/QEMU boot and
# finally-stop skeleton. Only its function definitions are loaded, never its main.
lib = source / 'qa/privsep-slice1/run.py'
tree = ast.parse(lib.read_text())
names = {'save', 'run', 'free_port', 'guest_cloud_config', 'guest_install_command'}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), str(lib), 'exec'))


def require_step(name, code):
    if code:
        receipt['failures'].append(name)
    save()


HERMES_SETUP = r'''useradd -m -s /bin/bash hermes
loginctl enable-linger hermes
uid=$(id -u hermes)
systemctl start user@$uid.service
# Use a stock, unmodified checkout; uv/pip installs it at the unprivileged uid.
# docs/getting-started/installation + docs/reference/package-management.
runuser -u hermes -- env HOME=/home/hermes bash -se <<'AGENT'
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME"
mkdir -p "$HOME/.hermes" "$HOME/.local/bin"
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$HOME/hermes-agent"
# Current upstream docs require PM's uv-backed activation to record extras.
# A raw external uv venv works once, then the canonical gateway launcher loses
# messaging on restart because it uses PM's distinct selected environment.
cd "$HOME/hermes-agent"
source ./activate
python - <<'PM'
from pm import sync_venv
sync_venv(['messaging', 'mcp'], explicit=True)
PM
# [all] intentionally excludes messaging. Re-activate the newly selected
# generation rather than adding its site-packages to a running interpreter.
source ./activate
python -c "import aiohttp,mcp; print('stock API/MCP dependencies available')"
ln -s "$HOME/hermes-agent/.hermes/bin/hermes" "$HOME/.local/bin/hermes"
cat > "$HOME/.hermes/config.yaml" <<'CFG'
model:
  default: qa-no-inference
  provider: custom
  base_url: http://127.0.0.1:9/v1
platforms:
  api_server:
    enabled: true
    host: 127.0.0.1
    port: 8642
CFG
python3 - <<'PY'
import pathlib,secrets
h=pathlib.Path.home()/'.hermes'
p=h/'.env'; p.write_text('API_SERVER_ENABLED=true\nAPI_SERVER_KEY='+secrets.token_urlsafe(32)+'\nOPENAI_API_KEY=qa-not-a-real-key\nOPENAI_BASE_URL=http://127.0.0.1:9/v1\n')
p.chmod(0o600)
PY
hermes --version
git -C "$HOME/hermes-agent" rev-parse HEAD
AGENT
uid=$(id -u hermes)
# The stock user service is the one `wizard link` can restart at the same uid.
# An unrelated QA system unit is killed as a manual gateway and not restarted.
runuser -u hermes -- env HOME=/home/hermes HERMES_HOME=/home/hermes/.hermes \
  PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin \
  XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus \
  bash -c 'cd "$HOME"; hermes gateway install </dev/null'
for n in $(seq 1 90); do curl -fsS http://127.0.0.1:8642/health && break; sleep 2; done
curl -fsS http://127.0.0.1:8642/health
id hermes
if runuser -u hermes -- sudo -n true; then exit 1; fi
'''

DENIALS = r'''id hermes
id -nG hermes | tr ' ' '\n' | grep -Ex 'docker|sudo|wheel|admin' && exit 1 || true
runuser -u hermes -- python3 - <<'PY'
import os,subprocess,socket
assert not os.access('/var/run/docker.sock',os.R_OK|os.W_OK)
assert not os.access('/var/lib/wizard-os',os.W_OK)
assert not os.access('/var/lib/wizard-os/wizard-app-manager',os.W_OK)
from pathlib import Path
probe=Path('/var/lib/wizard-os/runtipi/media/.privsep-agent-write-proof')
probe.write_text('files-root-only'); probe.unlink()
assert not os.access('/var/lib/wizard-os/.wizard-os/api-token',os.R_OK)
assert not os.access('/run/containerd/containerd.sock',os.R_OK|os.W_OK)
for port in (2375,2376):
 try:
  connection=socket.create_connection(('127.0.0.1',port),timeout=2)
 except OSError: pass
 else:
  connection.close()
  raise AssertionError('Docker TCP API reachable on '+str(port))
assert subprocess.run(['sudo','-n','true'],capture_output=True).returncode != 0
p=subprocess.run(['docker','ps'],capture_output=True,text=True)
assert p.returncode != 0 and 'permission denied' in p.stderr.lower(),p.stderr
s=socket.socket(socket.AF_UNIX)
try:
 s.connect('/var/run/docker.sock')
except PermissionError: pass
else: raise AssertionError('socket connected')
p=subprocess.run(['tailscale','set','--accept-dns=false'],capture_output=True,text=True)
assert p.returncode != 0 and any(x in p.stderr.lower() for x in ('access denied','permission','must be root','sudo')),p.stderr
print('PASS real kernel denials: sudo, Docker API, protected files, Tailscale mutation')
PY
tailscale debug prefs | python3 -c 'import json,sys; assert json.load(sys.stdin)["OperatorUser"]=="wizard-system"'
systemctl show wizard-app-manager-api -p User -p ExecStart
systemctl show wizard-runtipi -p User -p ExecStart
'''

ACTIONS = r'''runuser -u hermes -- env HOME=/home/hermes PATH=/usr/local/bin:/usr/bin:/bin bash -se <<'AGENT'
wam apps --json
wam search calendar --json
wam install wizard-calendar
wam restart wizard-calendar
wam stop wizard-calendar
wam start wizard-calendar
wam update wizard-calendar
wam logs wizard-calendar --tail 30 --json > "$HOME/log-proof.json"
python3 - <<'PY'
import json,pathlib
v=json.loads((pathlib.Path.home()/'log-proof.json').read_text())
assert v['containers']>0 and isinstance(v['logs'],str),v
print('PASS installed app logs retrieved; a silent app may have an empty log')
PY
wam uninstall wizard-calendar
python3 - <<'PY'
import json,pathlib,urllib.request,urllib.error
h=pathlib.Path.home()/'.wizard-os'; token=(h/'agent-token').read_text().strip()
r=urllib.request.Request('http://127.0.0.1:7790/api/grants',data=b'{"app":"wizard-calendar","to":"files"}',headers={'Authorization':'Bearer '+token,'Tailscale-User-Login':'forged@example.test','Content-Type':'application/json'})
try: urllib.request.urlopen(r)
except urllib.error.HTTPError as e:
 assert e.code==403 and json.load(e)['error']=='owner_only'
else: raise AssertionError('owner route accepted agent')
print('PASS agent token cannot approve even with forged owner headers')
PY
for cmd in backup links builder; do
 if wam "$cmd" >"$HOME/deferred.txt" 2>&1; then exit 1; fi
 grep -q 'not yet available without server access' "$HOME/deferred.txt"
done
AGENT
'''


def guest_bootstrap_command(ref, label):
    """Pinned version of the documented owner start/status route, bounded."""
    private_access = INSTALL_PRIVATE_ACCESS[label]
    url = f'https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/{ref}/get.sh'
    return (f'curl -fsSL {url} -o /root/get.sh && '
            f'WAM_REF={ref} WIZARD_MODE=alongside WIZARD_HERMES_USER=hermes '
            f'HERMES_HOME=/home/hermes/.hermes WIZARD_PRIVATE_ACCESS={private_access} '
            'WIZARD_AGENT_SAFE=1 WIZARD_TAILSCALE_WAIT=1 bash -s start < /root/get.sh\n'
            'start_code=$?; [ "$start_code" -eq 0 ] || exit "$start_code"\n'
            'deadline=$((SECONDS + 3000)); code=124\n'
            'while [ "$SECONDS" -lt "$deadline" ]; do\n'
            '  status=$(bash -s status < /root/get.sh); printf "%s\\n" "$status"\n'
            '  state=$(python3 -c \'import json; print(json.load(open("/root/.wizard-os/install-status.json"))["state"])\')\n'
            '  case "$status" in *"Wizard OS install: stopped"*|*"Wizard OS install: interrupted"*) code=125; break ;; esac\n'
            '  case "$state" in\n'
            '    done) code=0; break ;;\n'
            '    failed|needs_admin) code=$(python3 -c \'import json; print(json.load(open("/root/.wizard-os/install-status.json")).get("exit_code",3) or 3)\'); break ;;\n'
            '  esac\n'
            '  sleep 3\n'
            'done\n'
            'cat /root/.wizard-os/install-status.json\n'
            'cat /root/.wizard-os/install.log\n'
            'exit "$code"')


def baseline_failure_matches(ref, code, probe, log):
    """Accept only the exact observed main bug; all other failures stay red."""
    status = probe.get('status', {})
    return (ref == KNOWN_BASELINE_SHA and code == 3
            and status.get('state') == 'failed' and status.get('exit_code') == 3
            and status.get('failure_reason') == 'Administrator rights are needed once for: host preparation'
            and probe.get('runtime_env') is True and probe.get('user_bus') is False
            and probe.get('docker_info') is True and probe.get('sg_docker_info') is True
            and probe.get('named_host_ready_exit') == 1
            and probe.get('user_manager') == 'active' and probe.get('linger') == 'yes'
            and probe.get('checkout_exists') is False
            and probe.get('sudo_available') is False
            and 'Administrator host preparation complete for hermes; no passwordless sudo was granted.' in log
            and '[wizard-os] owner-admin: The account hermes is not in this server' in log)


def finalize_receipt(value):
    required = {'candidate-' + n for n in ('fresh', 'tools', 'stock-hermes', 'install',
                'mirror', 'source', 'doctor', 'denials', 'actions', 'service-uid')}
    steps = {v['name']: v['exit'] for v in value['steps']}
    candidate_failures = [v for v in value['failures'] if v.startswith('candidate-')]
    vms = [v for v in value['vms'] if v['name'] == 'candidate']
    value['candidate_passed'] = (value.get('candidate_doctor_passed') is True
                                 and not candidate_failures and len(vms) == 1 and vms[0]['stopped']
                                 and all(steps.get(n) == 0 for n in required))
    baseline_ok = (value['smoke'] or value['baseline_status'] == 'completed'
                   or value['baseline_failure_matches_documented'])
    value['passed'] = value['candidate_passed'] and baseline_ok and not value['failures']


BASELINE_PROBE = r'''python3 - <<'PY'
import json, pathlib, subprocess
s=pathlib.Path('/root/get.sh').read_text()
functions='\n'.join(s[s.index(n+'() {'):s.index('\n}',s.index(n+'() {'))+2]
                    for n in ('docker_group_member','named_host_ready'))
def owner(command):
    return subprocess.run(['sudo','-iu','hermes','env','WIZARD_PRIVATE_ACCESS=none','bash','-s'],
                          input=command+'\n',capture_output=True,text=True)
def root(*args):
    return subprocess.check_output(args,text=True).strip()
uid=root('id','-u','hermes')
v={'status':json.loads(pathlib.Path('/root/.wizard-os/install-status.json').read_text()),
   'runtime_env':owner('test -f "$HOME/runtipi/.env"').returncode == 0,
   'user_bus':owner('test -S /run/user/'+uid+'/bus').returncode == 0,
   'docker_info':owner('docker info').returncode == 0,
   'sg_docker_info':owner("sg docker -c 'docker info'").returncode == 0,
   'named_host_ready_exit':owner(functions+'\nnamed_host_ready').returncode,
   'user_manager':root('systemctl','is-active','user@'+uid+'.service'),
   'linger':root('loginctl','show-user','hermes','-p','Linger','--value'),
   'checkout_exists':pathlib.Path('/home/hermes/wizard-app-manager').exists(),
   'sudo_available':owner('sudo -n true').returncode == 0}
print(json.dumps(v))
PY
'''

def guest_vm(label, ref, image, key):
    vm = root / label; vm.mkdir()
    run(label + '-overlay', ['qemu-img','create','-f','qcow2','-F','qcow2','-b',str(image),str(vm/'disk.qcow2'),'56G'])
    (vm/'user-data').write_text(guest_cloud_config(key.with_suffix('.pub').read_text().strip()))
    (vm/'meta-data').write_text('instance-id: privsep3-' + label + '\nlocal-hostname: privsep3-' + label + '\n')
    run(label+'-seed',['cloud-localds',str(vm/'seed.iso'),str(vm/'user-data'),str(vm/'meta-data')])
    port=free_port()
    argv=['qemu-system-x86_64','-accel','kvm','-machine','q35','-cpu','host','-smp','4','-m','6144',
          '-display','none','-serial','file:'+str(vm/'serial.log'),'-drive','file='+str(vm/'disk.qcow2')+',if=virtio,format=qcow2',
          '-drive','file='+str(vm/'seed.iso')+',if=virtio,format=raw,readonly=on',
          '-netdev',f'user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22','-device','virtio-net-pci,netdev=n0']
    log=(vm/'qemu.log').open('w'); proc=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT)
    state={'name':label,'pid':proc.pid,'argv':argv,'stopped':False}; receipt['vms'].append(state); save()
    ssh=['ssh','-i',str(key),'-p',str(port),'-o','BatchMode=yes','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null',
         '-o','ConnectTimeout=5','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-o','LogLevel=ERROR','root@127.0.0.1']
    def guest(name,command,timeout=180,required=False):
        rc=run(label+'-'+name,ssh+['bash -se'],timeout=timeout,stdin=('set -o pipefail\n'+command+'\n').encode(),required=False)
        if required: require_step(label+'-'+name,rc)
        return rc
    try:
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            if proc.poll() is not None: raise RuntimeError('QEMU exited before SSH')
            if subprocess.run(ssh+['true'],capture_output=True,timeout=10).returncode==0: break
            time.sleep(3)
        else: raise RuntimeError('guest SSH timeout')
        guest('fresh','cloud-init status --wait; cat /etc/os-release; test ! -d /home/hermes; cat /etc/docker/daemon.json',600,True)
        guest('tools','apt-get -o DPkg::Lock::Timeout=600 update -qq; DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq curl sudo git python3 python3-venv python3-yaml build-essential libatomic1',600,True)
        setup = HERMES_SETUP.replace('git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$HOME/hermes-agent"',
            'git init "$HOME/hermes-agent"; git -C "$HOME/hermes-agent" remote add origin https://github.com/NousResearch/hermes-agent.git; '
            'git -C "$HOME/hermes-agent" fetch --depth 1 origin ' + receipt['hermes_commit'] + '; '
            'git -C "$HOME/hermes-agent" checkout --detach FETCH_HEAD')
        if guest('stock-hermes',setup,1500,True): return
        install=guest_bootstrap_command(ref, label)
        code=guest('install',guest_install_command(install),9500,label == 'candidate')
        if label == 'baseline':
            if code:
                probe_code = guest('failure-probe', BASELINE_PROBE, 120, True)
                probe = {}
                if probe_code == 0:
                    probe = json.loads((root/'baseline-failure-probe.log').read_text().splitlines()[-1])
                matched = baseline_failure_matches(ref, code, probe, (root/'baseline-install.log').read_text())
                receipt['baseline_failure_probe'] = probe
                receipt['baseline_failure_matches_documented'] = matched
                receipt['baseline_status'] = ('cannot complete on main: ' + BASELINE_REASON if matched
                                              else 'unexpected install failure: ' + str(code))
                if not matched: receipt['failures'].append('baseline unexpected install failure')
                save()
                return
            receipt['baseline_status'] = 'completed'
            save()
        guest('mirror',"docker info --format '{{json .RegistryConfig.Mirrors}}' | python3 -c 'import json,sys; assert any(\"mirror.gcr.io\" in v for v in json.load(sys.stdin))'",60,True)
        manager='wizard-system' if label=='candidate' else 'hermes'
        home='/var/lib/wizard-os' if label=='candidate' else '/home/hermes'
        # Read back the exact installed source and save the entire doctor row set.
        guest('source',f'runuser -u {manager} -- git -C {home}/wizard-app-manager rev-parse HEAD | grep -Fx {ref}',60,True)
        doctor=f'cd {home}/wizard-app-manager; runuser -u {manager} -- env HOME={home} RUNTIPI_ROOT={home}/runtipi WIZARD_OS_HOME={home}/.wizard-os python3 -c \'import json; from wam.cli import Config,doctor; print(json.dumps([{{"check":n,"ok":ok,"detail":d}} for n,ok,d in doctor(Config())]))\''
        guest('doctor',doctor,300,True)
        guest('runtime-log','docker logs --tail 160 runtipi 2>&1',120)
        if label=='candidate':
            guest('denials',DENIALS,180,True)
            if not code: guest('actions',ACTIONS,1500,True)
            guest('service-uid',r'''cid=$(docker ps -q --filter label=com.docker.compose.service=wizard-agent-service)
test -n "$cid"
docker inspect "$cid" --format '{{json .Config.User}} {{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}}'
docker exec "$cid" id
docker exec "$cid" node -e 'if (process.getuid()!==Number(process.argv[1])) process.exit(1)' "$(id -u hermes)"
curl -fsS http://127.0.0.1:8642/health
curl -fsS http://127.0.0.1:8923/wizard/health | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["status"]=="ok" and v["upstream_reachable"],v'
''',120,True)
        else:
            guest('baseline-groups','id hermes; id -nG hermes | tr " " "\\n" | grep -Fx docker',60,True)
    finally:
        try:
            if proc.poll() is None:
                subprocess.run(ssh+['poweroff'],capture_output=True,timeout=15)
                try: proc.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try: proc.wait(timeout=15)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=15)
        finally:
            state['stopped']=proc.poll() is not None; state['exit']=proc.returncode
            log.close(); save()


def doctor_rows(label):
    text=(root/(label+'-doctor.log')).read_text()
    for line in reversed(text.splitlines()):
        if line.startswith('[{'): return json.loads(line)
    raise RuntimeError(label+' doctor did not emit rows')


def suite():
    # A git archive has the exact candidate tree but no .git. Tests intentionally
    # exercise tracked-file/repository contracts, so give that tree a labelled QA
    # snapshot (not a claimed upstream commit or a network checkout).
    if not (source / '.git').exists():
        run('pytest-git-init', ['git', 'init'], cwd=source)
        run('pytest-git-add', ['git', 'add', '.'], cwd=source)
        run('pytest-git-snapshot', ['git', '-c', 'user.name=Privsep QA', '-c',
            'user.email=privsep-qa@example.invalid', '-c', 'commit.gpgsign=false',
            'commit', '-qm', 'QA source export of ' + candidate], cwd=source)
    os.environ['PYTEST_ADDOPTS']=''
    run('test-env',['python3','-m','venv',str(root/'test-venv')])
    py=str(root/'test-venv/bin/python')
    run('test-deps',[py,'-m','pip','install','pytest','pyyaml'])
    # Linux AF_UNIX socket names are short. The long evidence path makes the
    # rootless-Docker socket fixture fail before exercising product code.
    with tempfile.TemporaryDirectory(prefix='p3t-', dir='/srv/work') as basetemp:
        run('pytest',[py,'-m','pytest','-q','tests','--basetemp='+basetemp,'--junitxml='+str(root/'pytest.xml')],cwd=source,timeout=2400,required=False)
    xml=ET.parse(root/'pytest.xml')
    failures=[]; total=0
    for c in xml.iter('testcase'):
        total+=1
        if any(n.tag in ('failure','error') for n in c): failures.append(c.attrib.get('classname','')+'::'+c.attrib.get('name',''))
    receipt['pytest']={'count':total,'failures':failures}
    allowed=['tests.test_chats_views::test_typescript_public_index_path_has_identical_seal']
    if failures!=allowed: receipt['failures'].append('full pytest differs from known pre-existing failure')
    save()


def interrupt(signum, frame):
    raise RuntimeError('runner interrupted: '+str(signum))


signal.signal(signal.SIGTERM,interrupt)
signal.signal(signal.SIGINT,interrupt)
signal.signal(signal.SIGALRM,interrupt)
signal.alarm(10500)  # leave teardown time inside the admitted 10800-second job
try:
    hermes_commit = subprocess.check_output(['git', 'ls-remote', 'https://github.com/NousResearch/hermes-agent.git', 'HEAD'], text=True, timeout=60).split()[0]
    assert re.fullmatch('[0-9a-f]{40}', hermes_commit)
    receipt['hermes_commit'] = hermes_commit
    image=Path('/srv/work/images/debian-12-genericcloud-amd64.qcow2')
    if not image.is_file():
        image=root/'debian-12.qcow2'
        run('download',['curl','-fL','--retry','3','https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2','-o',str(image)],timeout=600)
    run('image-hash',['sha256sum',str(image)])
    key=private/'id_ed25519'; run('key',['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
    guest_vm('candidate',candidate,image,key)
    candidate_doctor = doctor_rows('candidate')
    receipt['candidate_doctor_passed'] = bool(candidate_doctor) and all(v['ok'] for v in candidate_doctor)
    save()
    if not smoke:
        guest_vm('baseline',baseline,image,key)
        try:
            cand = doctor_rows('candidate')
            if receipt['baseline_status'] == 'completed':
                base = doctor_rows('baseline')
                old = {v['check'] for v in base if not v['ok']}
                new = [v for v in cand if not v['ok'] and v['check'] not in old]
                receipt['doctor_comparison'] = {'baseline_failures': sorted(old), 'new_failures': new,
                                                 'candidate': cand, 'baseline': base}
            else:
                # No invented main doctor rows: an install that cannot finish has no comparison.
                new = [v for v in cand if not v['ok']]
                receipt['doctor_comparison'] = {'status': 'unavailable: baseline did not install',
                                                 'candidate': cand, 'baseline': None}
            if new: receipt['failures'].append('doctor regressions or candidate failures')
        except Exception as e: receipt['failures'].append(str(e))
        suite()
except Exception:
    receipt['exception']=traceback.format_exc(); receipt['failures'].append('runner exception')
finally:
    receipt['finished']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not all(v['stopped'] for v in receipt['vms']): receipt['failures'].append('VM teardown incomplete')
    finalize_receipt(receipt); save()
print(json.dumps(receipt,indent=2))
sys.exit(0 if receipt['passed'] else 1)
