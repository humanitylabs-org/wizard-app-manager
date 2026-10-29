#!/usr/bin/env python3
"""Two sequential disposable KVM guests. Parent admission only; preflight is read-only.

Reuse accepted security image/source evidence. Never build, submit, update core
Hermes, weaken bind ancestry, authenticate a tailnet or touch an installed host.
"""
import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

sys.dont_write_bytecode = True
MIN_FREE = 200_000_000_000 + 16 * 1024**3
CLOUD = Path('/srv/work/images/debian-12-genericcloud-amd64.qcow2')
CLOUD_SHA = '24036a8b79cbb11d809752c60f2f885b1a3b02b1d663faa0b648eb87bd7a256e'
PHASE = 'manifest'


def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def validate(manifest, expected):
    assert sha(manifest) == expected, 'Manifest drift'
    d = json.loads(manifest.read_text()); stage = manifest.parent
    assert d['schema'] == 'privsep-compat-v1' and d['stage'] == str(stage)
    assert d['minimum_free_bytes'] == MIN_FREE and MIN_FREE >= 208589934592
    assert d['scenarios'] == ['fresh', 'migration']
    for name, h in d['files'].items():
        p = stage / name
        assert p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(stage)
        assert sha(p) == h['sha256'] and p.stat().st_mode & 0o777 == h['mode'], name
        if p.suffix == '.py': compile(p.read_bytes(), str(p), 'exec')
    for path, h in d['inherited_files'].items(): assert sha(path) == h, 'Inherited proof drift'
    prior = Path(d['security_stage'])
    mod = load_module('accepted_security', prior / 'private/acceptance_vm.py')
    _, previous = mod.source(prior / 'acceptance-manifest.json', d['inherited_files'][str(prior / 'acceptance-manifest.json')])
    binding = mod.image_binding(prior, sha(prior / 'acceptance-manifest.json'), previous)
    assert binding['host_source_revision'] == d['production_revision']
    assert binding['backend_image'] == d['backend_image']
    proof = json.loads((prior / 'credential-rotation-evidence/rotation-receipt.json').read_text())
    acceptance = json.loads((prior / 'credential-rotation-evidence/acceptance.json').read_text())
    boundaries = ['no-crash', 'journal:db_intent', 'effect:db', 'effect:queue', 'effect:env', 'effect:restart', 'journal:verified']
    assert [r['boundary'] for r in proof['cases']] == boundaries
    assert all(r['phase'] == 'verified' and r['old_bearer'] == 401 and r['new_bearer'] == 200
               and r['old_database_password'] == 'rejected' and r['old_queue_password'] == 'rejected'
               and r['app_bytes_uid_gid_mode'] == 'unchanged' and r['legacy_uid_protected_read'] == 'denied'
               for r in proof['cases'])
    assert acceptance['passed'] and acceptance['private_artifacts_removed']
    assert acceptance['focused_tests']['passed'] == 103 and acceptance['focused_tests']['skipped'] == 0
    assert all(v['stopped'] for v in acceptance['vms'])
    # Exact old/new archive closure. Only this harness and handoff docs may differ.
    def inventory(path):
        result = {}
        with tarfile.open(path) as archive:
            for m in archive:
                if m.isdir(): continue
                assert m.name not in result and not Path(m.name).is_absolute() and '..' not in Path(m.name).parts
                assert m.isfile() or m.issym()
                result[m.name] = {'mode': m.mode & 0o777, 'type': 'symlink' if m.issym() else 'file',
                                  'value': m.linkname if m.issym() else hashlib.sha256(archive.extractfile(m).read()).hexdigest()}
        return result
    old = inventory(prior / 'archives/host-source.tar')
    new = inventory(stage / 'source.tar')
    changes = sorted(n for n in old.keys() | new.keys() if old.get(n) != new.get(n))
    assert changes and changes == d['source_delta']
    allowed = lambda n: n.startswith('qa/privsep-compat/') or n in ('PRIVSEP-HANDOFF.md', 'qa/privsep-slice6/RECEIPT.md')
    assert all(allowed(n) for n in changes), 'Production source changed during independent review'
    for name, h in d['source_members'].items():
        assert new[name]['type'] == 'file' and new[name]['value'] == h
    assert sha(d['backend_image']['archive']) == d['backend_image']['sha256']
    assert CLOUD.is_file() and sha(CLOUD) == CLOUD_SHA
    for tool in ('qemu-system-x86_64', 'qemu-img', 'cloud-localds', 'ssh', 'scp', 'ssh-keygen'):
        assert shutil.which(tool), tool
    return d


def error_info(error):
    frames = []
    tb = error.__traceback__
    while tb:
        known = Path(tb.tb_frame.f_code.co_filename).resolve() == Path(__file__).resolve()
        frames.append({'file': 'run.py' if known else '<external>',
                       'function': tb.tb_frame.f_code.co_name if known else '<external>', 'line': tb.tb_lineno})
        tb = tb.tb_next
    return {'phase': PHASE, 'class': type(error).__name__, 'frames': frames[-16:]}


class VM:
    def __init__(self, stage, name, doc, receipt, save):
        self.stage, self.name, self.doc = stage, name, doc
        self.out = stage / 'evidence' / name
        self.out.mkdir(mode=0o700)
        self.private = self.out / 'private'; self.private.mkdir(mode=0o700)
        self.state = {'name': name, 'stopped': False, 'steps': [], 'passed': False}
        self.receipt, self.save = receipt, save
        receipt['vms'].append(self.state); save()
        self.proc = None; self.log = None; self.ssh = None

    def run(self, label, argv, data=None, timeout=600, required=True):
        global PHASE
        PHASE = self.name + ':' + label
        print(json.dumps({'phase': PHASE, 'status': 'started'}), flush=True)
        # Raw installer output never enters host logs. Root-only guest logs die with VM.
        target = self.out / (label + '.log')
        try:
            with target.open('wb') as f:
                r = subprocess.run(argv, input=data, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            self.state['steps'].append({'name': label, 'returncode': r.returncode,
                                        'log': target.name, 'sha256': sha(target)})
            self.save()
            print(json.dumps({'phase': PHASE, 'returncode': r.returncode}), flush=True)
            if required and r.returncode: raise RuntimeError('Isolated phase failed')
            return r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            self.state['steps'].append({'name': label, 'error': error_info(e),
                                        'log': target.name, 'sha256': sha(target) if target.exists() else None})
            self.save(); raise

    def guest(self, name, text, timeout=900, required=True):
        return self.run(name, self.ssh + ['bash -se'],
                        ('set -o pipefail\numask 022\n' + text + '\n').encode(), timeout, required)

    def boot(self):
        key = self.private / 'id_ed25519'
        self.run('key', ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)])
        public = key.with_suffix('.pub').read_text().strip()
        user = self.private / 'user-data'
        user.write_text('#cloud-config\nusers:\n  - name: root\n    lock_passwd: true\n    ssh_authorized_keys:\n      - ' + public + '\n'
                        'disable_root: false\nssh_pwauth: false\nwrite_files:\n'
                        '  - path: /etc/ssh/sshd_config.d/99-compat.conf\n    content: |\n      PermitRootLogin prohibit-password\n      PasswordAuthentication no\n'
                        '  - path: /etc/docker/daemon.json\n    content: |\n      {"registry-mirrors": ["https://mirror.gcr.io"]}\n')
        meta = self.private / 'meta-data'; meta.write_text('instance-id: ' + self.doc['run_id'] + '-' + self.name + '\n')
        disk = self.private / 'disk.qcow2'; seed = self.private / 'seed.iso'
        self.run('disk', ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(CLOUD), str(disk), '64G'])
        self.run('seed', ['cloud-localds', str(seed), str(user), str(meta)])
        with socket.socket() as s: s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
        argv = ['qemu-system-x86_64', '-accel', 'kvm', '-machine', 'q35', '-cpu', 'host', '-smp', '4', '-m', '6144',
                '-display', 'none', '-serial', 'file:' + str(self.private / 'serial.log'),
                '-drive', 'file=' + str(disk) + ',if=virtio,format=qcow2',
                '-drive', 'file=' + str(seed) + ',if=virtio,format=raw,readonly=on',
                '-netdev', 'user,id=n0,hostfwd=tcp:127.0.0.1:' + str(port) + '-:22', '-device', 'virtio-net-pci,netdev=n0']
        self.log = (self.private / 'qemu.log').open('wb')
        self.proc = subprocess.Popen(argv, stdout=self.log, stderr=subprocess.STDOUT)
        self.state.update(pid=self.proc.pid, argv=argv); self.save()
        options = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                   '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'LogLevel=ERROR']
        self.ssh = ['ssh', '-i', str(key), '-p', str(port), *options, 'root@127.0.0.1']
        self.scp = ['scp', '-i', str(key), '-P', str(port), *options]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            assert self.proc.poll() is None
            if subprocess.run(self.ssh + ['true'], capture_output=True, timeout=10).returncode == 0: break
            time.sleep(3)
        else: raise TimeoutError('VM SSH deadline')
        self.guest('clean-guest', 'cloud-init status --wait; test ! -d /var/lib/wizard-os; test ! -d /root/.hermes; '
                   'test ! -d /var/lib/docker/containers; test ! -e /home/hermes; test ! -e /home/wizard')
        self.guest('tools', 'apt-get -o DPkg::Lock::Timeout=600 update -qq; '
                   'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq '
                   'docker.io python3-yaml ca-certificates curl sudo git dbus-user-session; systemctl enable --now docker; '
                   'test -z "$(docker ps -aq)"; install -d -m 0755 /opt/privsep-source', timeout=1200)
        self.run('copy-source', self.scp + [str(self.stage / 'source.tar'), 'root@127.0.0.1:/opt/privsep-source.tar'])
        self.run('copy-config', self.scp + [str(self.stage / 'guest-config.json'), 'root@127.0.0.1:/opt/privsep-compat.json'])
        self.run('copy-image', self.scp + [self.doc['backend_image']['archive'], 'root@127.0.0.1:/opt/backend.tar.gz'], timeout=900)
        self.guest('load-bound-inputs',
                   f"echo '{self.doc['files']['source.tar']['sha256']}  /opt/privsep-source.tar' | sha256sum -c -; "
                   f"echo '{self.doc['backend_image']['sha256']}  /opt/backend.tar.gz' | sha256sum -c -; "
                   'tar -xf /opt/privsep-source.tar -C /opt/privsep-source; docker load -i /opt/backend.tar.gz; '
                   'rm /opt/backend.tar.gz /opt/privsep-source.tar; touch /run/privsep-compat-disposable', timeout=900)
        image = self.doc['backend_image']['id']
        self.guest('compose-from-accepted-image',
                   'mkdir -p /usr/local/lib/docker/cli-plugins; '
                   f'docker run --rm --network=none --entrypoint cat {image} /usr/local/bin/docker-compose > /usr/local/lib/docker/cli-plugins/docker-compose; '
                   f"echo '{self.doc['compose_sha256']}  /usr/local/lib/docker/cli-plugins/docker-compose' | sha256sum -c -; "
                   'chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose; docker compose version')

    def checked(self, label, action, scenario):
        return self.guest(label, '/usr/bin/python3 -I -B /opt/privsep-source/qa/privsep-compat/guest.py ' +
                          action + ' ' + scenario + ' /opt/privsep-compat.json', timeout=1800)

    def installer(self, label, script, timeout=2400):
        # Installer logs can contain bootstrap auth. Keep them in disposable guest;
        # emit only exit and fixed source-bound diagnostic signatures.
        program = ('import subprocess,json,pathlib\np=pathlib.Path("/root/compat-install.log")\n'
                   'with p.open("wb") as f:\n r=subprocess.run(["bash","-se"],input=' + repr(script.encode()) +
                   ',stdout=f,stderr=subprocess.STDOUT,timeout=' + str(timeout - 30) + ')\n'
                   'raw=p.read_bytes()\nprint(json.dumps({"event":"installer","returncode":r.returncode,'
                   '"host_guard_refusal":b"Wizard host policy" in raw,'
                   '"session_bus_missing":b"Failed to connect to bus" in raw,'
                   '"permission_denied":b"Permission denied" in raw}))\nraise SystemExit(r.returncode)\n')
        return self.run(label, self.ssh + ['/usr/bin/python3', '-'], program.encode(), timeout)

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                try:
                    if self.ssh: subprocess.run(self.ssh + ['shutdown -h now'], capture_output=True, timeout=15)
                    self.proc.wait(timeout=60)
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.terminate()
                    try: self.proc.wait(timeout=30)
                    except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait(timeout=30)
            self.state.update(stopped=self.proc.poll() is not None, qemu_exit=self.proc.returncode)
        else: self.state['stopped'] = True
        if self.log: self.log.close()
        if self.state['stopped']: shutil.rmtree(self.private)
        self.state['private_artifacts_removed'] = not self.private.exists()
        self.save()


def fresh(vm, d):
    image = d['backend_image']['id']
    vm.installer('fresh-installer', 'set -euo pipefail\numask 022\n'
                 f'export WAM_REF={d["revision"]} WAM_IMAGE={image} WIZARD_RELEASE=none WIZARD_MODE=fresh WIZARD_USER=wizard '
                 'WIZARD_PRIVATE_ACCESS=none WIZARD_TAILSCALE_WAIT=1 WIZARD_GITHUB=0\n'
                 'bash /opt/privsep-source/get.sh\n')
    vm.checked('fresh-backend-identity', 'backend', 'fresh')
    vm.checked('fresh-stock-apps', 'seed', 'fresh')
    vm.installer('fresh-post-import-restart', 'set -euo pipefail\nrunuser -u wizard -- env HOME=/home/wizard PATH=/home/wizard/.local/bin:/usr/local/bin:/usr/bin:/bin wam app stop wizard-calendar\n'
                 'runuser -u wizard -- env HOME=/home/wizard PATH=/home/wizard/.local/bin:/usr/local/bin:/usr/bin:/bin wam app start wizard-calendar\n', timeout=600)
    vm.checked('fresh-restart-preservation', 'data', 'fresh')
    # Containerized fresh agents are checked with their real live container identity.
    vm.guest('fresh-agent-boundary', '/usr/bin/python3 - <<\'PY\'\n'
             'import json,subprocess\n'
             'ids=subprocess.check_output(["docker","ps","-q","--filter","label=runtipi.appurn=hermes-agent:wizardapps"],text=True).split()\n'
             'assert ids\n'
             'for cid in ids:\n'
             ' x=json.loads(subprocess.check_output(["docker","inspect",cid]))[0]\n'
             ' assert not x["HostConfig"]["Privileged"]\n'
             ' assert not any(m.get("Source")=="/var/run/docker.sock" for m in x["Mounts"])\n'
             ' assert not any(m.get("Source")=="/" for m in x["Mounts"])\n'
             'print(json.dumps({"fresh_agent_no_host_docker_socket":True,"privileged":False}))\nPY')


def migration(vm, d):
    # Stock pinned host Hermes fixture; never change installed Hermes source or
    # point it at real credentials. The historical baseline needs a session bus.
    # Historical runner has top-level execution. Read only its literal fixture;
    # importing it would execute an unrelated suite, not reuse a safe helper.
    fixture_tree = ast.parse((vm.stage / 'helpers/privsep-slice4/run.py').read_bytes())
    setup = next(ast.literal_eval(n.value) for n in fixture_tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'HERMES_SETUP' for t in n.targets))
    clone = 'git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "$HOME/hermes-agent"'
    assert isinstance(setup, str) and setup.count(clone) == 1
    pinned_clone = ('git init "$HOME/hermes-agent"\n'
                    'git -C "$HOME/hermes-agent" remote add origin https://github.com/NousResearch/hermes-agent.git\n'
                    'git -C "$HOME/hermes-agent" fetch --depth 1 origin ' + d['hermes_commit'] + '\n'
                    'git -C "$HOME/hermes-agent" checkout --detach FETCH_HEAD')
    vm.installer('stock-hermes-fixture', setup.replace(clone, pinned_clone), timeout=1800)
    vm.guest('baseline-bus-workaround', 'loginctl enable-linger hermes; uid=$(id -u hermes); systemctl start user@$uid.service; test -S /run/user/$uid/bus')
    vm.installer('legacy-install', 'set -euo pipefail\numask 022\n'
                 f'export WAM_REF={d["baseline_revision"]} WAM_IMAGE={d["baseline_image"]} WIZARD_RELEASE=none '
                 'WIZARD_MODE=alongside WIZARD_HERMES_USER=hermes HERMES_HOME=/home/hermes/.hermes '
                 'WIZARD_PRIVATE_ACCESS=none WIZARD_TAILSCALE_WAIT=1 WIZARD_GITHUB=0\n'
                 f'curl -fsSL https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/{d["baseline_revision"]}/get.sh -o /root/legacy-get.sh\n'
                 'bash /root/legacy-get.sh\n')
    vm.checked('legacy-stock-apps', 'seed', 'legacy')
    # The root migration helper intentionally drops WAM_IMAGE. Bind its expected
    # local default tag to the exact accepted image; do not edit production code.
    vm.guest('pin-migration-backend', f'docker tag {d["backend_image"]["id"]} ghcr.io/humanitylabs-org/wizard-app-manager:latest')
    vm.installer('protected-migration-source', 'set -euo pipefail\ninstall -d -m 0700 /var/lib/wizard-os-migration\n'
                 'git clone --no-checkout https://github.com/humanitylabs-org/wizard-app-manager.git /var/lib/wizard-os-migration/source\n'
                 f'git -C /var/lib/wizard-os-migration/source checkout --detach {d["revision"]}\n'
                 f'test "$(git -C /var/lib/wizard-os-migration/source rev-parse HEAD)" = {d["revision"]}\n')
    protected = '/usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-privilege'
    prepare = protected + ' migrate --agent hermes --hermes-home /home/hermes/.hermes --ref ' + d['revision']
    vm.installer('pre-invalidation-checkpoint', prepare + ' --stop-after relocate\n', timeout=1800)
    vm.checked('stopped-relocation-copy', 'copy', 'migrated')
    vm.installer('pre-invalidation-rollback', protected + ' rollback\n', timeout=1200)
    vm.checked('rollback-apps-and-data', 'data', 'legacy')
    # Remigrate through real invalidation; pause AFTER verified credentials and
    # app restart, before the final complete marker. Interrupted-password boundaries are
    # already covered by the retained seven-boundary proof, not repeated here.
    vm.installer('post-invalidation-checkpoint', prepare + ' --authorize-invalidation --stop-after credentials\n', timeout=1800)
    vm.checked('credential-readback-before-resume', 'credentials', 'migrated')
    # Capture the exact fixed refusal, and prove no rollback side effect occurred.
    vm.guest('post-invalidation-rollback-refused', '/usr/bin/python3 - <<\'PY\'\n'
             'import subprocess,json,pathlib\n'
             'p=pathlib.Path("/var/lib/wizard-os/runtipi/.env"); before=p.read_bytes()\n'
             f'r=subprocess.run({["/usr/bin/python3", "-I", "/var/lib/wizard-os-migration/source/scripts/wam-privilege", "rollback"]!r},capture_output=True)\n'
             'assert r.returncode != 0\n'
             'assert b"snapshot rollback would revive leaked secrets" in r.stderr+r.stdout\n'
             'assert p.read_bytes()==before\n'
             'print(json.dumps({"rollback_refused":True,"live_credentials_unchanged":True,"forward_resume_required":True}))\nPY')
    vm.installer('forward-resume', protected + ' migrate --authorize-invalidation\n', timeout=2400)
    vm.checked('migrated-backend-identity', 'backend', 'migrated')
    vm.checked('migrated-apps-and-data', 'data', 'migrated')
    vm.checked('migrated-real-identity', 'identity', 'migrated')
    vm.checked('credential-readback-after-resume', 'credentials', 'migrated')
    # Actual thin client app-scoped lifecycle on the resulting box, no reinstall.
    vm.installer('thin-client-calendar-lifecycle',
                 'set -euo pipefail\nrunuser -u hermes -- /usr/local/bin/wam app stop wizard-calendar\n'
                 'runuser -u hermes -- /usr/local/bin/wam app start wizard-calendar\n', timeout=600)
    vm.checked('resumed-calendar-persistence', 'data', 'migrated')
    vm.installer('idempotent-migration-resume', protected + ' migrate --authorize-invalidation\n', timeout=900)
    vm.checked('idempotent-credential-readback', 'credentials', 'migrated')


def preflight_workflows(stage, doc):
    """Render actual workflows, syntax-check without running their commands."""
    class Dry:
        installer = VM.installer
        checked = VM.checked
        def __init__(self):
            self.stage = stage
            self.ssh = []
            self.shell = []
            self.python = []
            self.labels = []
        def run(self, label, argv, data=None, timeout=600, required=True):
            assert argv == ['/usr/bin/python3', '-'] and data is not None
            program = ast.parse(data)
            compile(program, label, 'exec')
            self.python.append(label)
            calls = [n for n in ast.walk(program) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == 'run']
            assert len(calls) == 1
            shell = next(ast.literal_eval(k.value) for k in calls[0].keywords if k.arg == 'input')
            return self.guest(label, shell.decode(), timeout)
        def guest(self, label, script, timeout=900, required=True):
            syntax = subprocess.run(['bash', '-n'], input=script.encode(), capture_output=True, timeout=10)
            assert syntax.returncode == 0, 'Rendered workflow shell syntax: ' + label
            self.shell.append(script); self.labels.append(label)
            # Check Python here-doc payloads too; bash -n does not parse them.
            import re
            for code in re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, re.S):
                compile(code, label, 'exec')
            return 0
    dry = Dry()
    fresh(dry, doc); migration(dry, doc)
    joined = '\n'.join(dry.shell)
    assert '--stop-after relocate' in joined and '--stop-after credentials' in joined
    assert 'snapshot rollback would revive leaked secrets' in joined
    assert 'prepare-privilege-migration.py' not in joined  # helper has no --stop-after flag
    assert 'wam stop ' not in joined and 'wam update ' not in joined
    assert len(dry.labels) == len(set(dry.labels))
    return {'shell_steps': len(dry.shell), 'installer_python_wrappers': len(dry.python),
            'workflows_rendered_only': True, 'steps': dry.labels}


def main():
    global PHASE
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path); p.add_argument('manifest_sha256')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--preflight', action='store_true'); g.add_argument('--execute', action='store_true')
    a = p.parse_args(); manifest = a.manifest.resolve(); stage = manifest.parent
    d = validate(manifest, a.manifest_sha256)
    free = shutil.disk_usage(stage).free
    if a.preflight:
        rendered = preflight_workflows(stage, d)
        print(json.dumps({'source_integrity': True, 'production_unchanged': True, 'credential_proof_preserved': True,
                          'heavy_checks_executed': False, 'vms_started': 0, 'status': 'staged_not_run',
                          'free_bytes': free, 'required_free_bytes': MIN_FREE, 'capacity_ready_now': free >= MIN_FREE,
                          'shortfall_bytes': max(0, MIN_FREE-free), 'manifest_sha256': a.manifest_sha256,
                          'rendered_workflow_checks': rendered}, indent=2))
        return 0
    PHASE = 'capacity-admission'
    assert free >= MIN_FREE, '200GB shared reserve plus 16GiB sequential-VM headroom required'
    assert os.access('/dev/kvm', os.R_OK | os.W_OK)
    out = stage / 'evidence'; out.mkdir(mode=0o700)  # one-shot, never overwrite earlier evidence
    receipt = {'schema': 'privsep-compat-receipt-v1', 'status': 'running', 'passed': False,
               'release_security_green': False, 'manifest_sha256': a.manifest_sha256,
               'runner_sha256': sha(__file__), 'revision': d['revision'], 'production_revision': d['production_revision'],
               'image_source_revision': d['image_source_revision'], 'vms': [],
               'credential_proof': d['credential_proof'], 'limitations': d['limitations'],
               'started': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    def save(): (out / 'acceptance.json').write_text(json.dumps(receipt, indent=2) + '\n')
    def interrupt(sig, frame): raise TimeoutError('Bounded compatibility runner interrupted')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM): signal.signal(sig, interrupt)
    signal.alarm(13500); save()
    try:
        for name, workflow in [('fresh', fresh), ('migration', migration)]:
            assert shutil.disk_usage(stage).free >= MIN_FREE
            vm = VM(stage, name, d, receipt, save)
            try:
                vm.boot(); workflow(vm, d); vm.state['passed'] = True
            except Exception as error:
                vm.state['error'] = error_info(error)
            finally:
                try: vm.close()
                except BaseException as cleanup:
                    vm.state['cleanup_error'] = error_info(cleanup)
                    # A second bounded direct-child stop is mandatory even if
                    # SSH shutdown or filesystem cleanup itself failed.
                    if vm.proc and vm.proc.poll() is None:
                        vm.proc.kill(); vm.proc.wait(timeout=30)
                    vm.state['stopped'] = vm.proc is None or vm.proc.poll() is not None
                    vm.state['passed'] = False
            save()
        validate(manifest, a.manifest_sha256)
    except BaseException as error:
        receipt['error'] = error_info(error)
    finally:
        signal.alarm(0)
        receipt['all_vms_stopped'] = all(v['stopped'] for v in receipt['vms'])
        receipt['private_artifacts_removed'] = all(v.get('private_artifacts_removed') for v in receipt['vms'])
        receipt['passed'] = (len(receipt['vms']) == 2 and all(v['passed'] for v in receipt['vms'])
                             and receipt['all_vms_stopped'] and receipt['private_artifacts_removed'] and not receipt.get('error'))
        receipt['status'] = 'bounded_compatibility_passed' if receipt['passed'] else 'review_required'
        receipt['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat(); save()
    print(json.dumps(receipt, indent=2))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as error:
        print(json.dumps({'status': 'review_required', 'passed': False, 'error': error_info(error)}))
        sys.exit(1)
