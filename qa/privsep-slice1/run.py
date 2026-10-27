#!/usr/bin/env python3
"""Isolated Debian 12/KVM proof. Run only on the admitted Linux worker.

argv: evidence_dir staged_git_repo baseline_sha candidate_sha
Owns only its evidence directory, disposable VM processes and guest state. Never
mounts host paths or supplies tailnet/model credentials. Stops every owned VM.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

root, source = map(lambda p: Path(p).resolve(), sys.argv[1:3])
baseline, candidate = sys.argv[3:5]
assert all(re.fullmatch(r'[0-9a-f]{40}', v) for v in (baseline, candidate))
assert str(root).startswith('/srv/work/privsep/slice1/')
assert not root.exists(), 'one-use evidence directory; preserve previous results'
root.mkdir(parents=True, mode=0o700)
private = root / 'private'
private.mkdir(mode=0o700)
receipt = {'started': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'baseline': baseline, 'candidate': candidate, 'steps': [], 'vms': [],
           'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def save():
    (root / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


def run(name, argv, timeout=180, required=True, cwd=None, stdin=None):
    path = root / (name + '.log')
    if stdin is not None:
        (root / (name + '.command.sh')).write_bytes(stdin)
    with path.open('w') as log:
        log.write(json.dumps(argv) + '\n'); log.flush()
        try:
            p = subprocess.run(argv, cwd=cwd, input=stdin, stdout=log,
                               stderr=subprocess.STDOUT, timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = 124
    receipt['steps'].append({'name': name, 'exit': code, 'log': path.name,
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    save()
    print(name, code, flush=True)
    if required and code:
        raise RuntimeError(f'{name} failed ({code}); see {path.name}')
    return code


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def guest_vm(label, ref, image, key):
    vm = root / label
    vm.mkdir()
    run(label + '-overlay', ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(image), str(vm / 'disk.qcow2'), '48G'])
    public = key.with_suffix('.pub').read_text().strip()
    user_data = vm / 'user-data'
    user_data.write_text('#cloud-config\nusers:\n  - name: root\n    lock_passwd: true\n'
                         '    ssh_authorized_keys:\n      - ' + public + '\n'
                         'disable_root: false\nssh_pwauth: false\n'
                         'write_files:\n  - path: /etc/ssh/sshd_config.d/99-privsep-test.conf\n'
                         '    content: |\n      PermitRootLogin prohibit-password\n      PasswordAuthentication no\n')
    meta = vm / 'meta-data'
    meta.write_text('instance-id: privsep-' + label + '\nlocal-hostname: privsep-' + label + '\n')
    run(label + '-seed', ['cloud-localds', str(vm / 'seed.iso'), str(user_data), str(meta)])
    port = free_port()
    argv = ['qemu-system-x86_64', '-accel', 'kvm', '-machine', 'q35', '-cpu', 'host',
            '-smp', '4', '-m', '6144', '-display', 'none', '-serial', 'file:' + str(vm / 'serial.log'),
            '-drive', 'file=' + str(vm / 'disk.qcow2') + ',if=virtio,format=qcow2',
            '-drive', 'file=' + str(vm / 'seed.iso') + ',if=virtio,format=raw,readonly=on',
            '-netdev', f'user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22', '-device', 'virtio-net-pci,netdev=n0']
    log = (vm / 'qemu.log').open('w')
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
    state = {'name': label, 'pid': proc.pid, 'argv': argv, 'stopped': False}
    receipt['vms'].append(state); save()
    ssh = ['ssh', '-i', str(key), '-p', str(port), '-o', 'BatchMode=yes',
           '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
           '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=3', '-o', 'LogLevel=ERROR', 'root@127.0.0.1']
    def guest(name, command, timeout=180, required=False):
        return run(label + '-' + name, ssh + ['bash -se'], timeout=timeout,
                   stdin=('set -o pipefail\n' + command + '\n').encode(), required=required)
    try:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('QEMU exited before SSH: ' + (vm / 'qemu.log').read_text()[-2000:])
            p = subprocess.run(ssh + ['true'], capture_output=True, timeout=10)
            if p.returncode == 0:
                break
            time.sleep(3)
        else:
            raise RuntimeError('Guest SSH did not become ready; serial.log retained')
        guest('fresh', 'cloud-init status --wait; cat /etc/os-release; id; '
              'test ! -d /home/wizard; test ! -d /root/.hermes; echo FRESH_NO_WIZARD_OR_HERMES', 600, True)
        guest('bootstrap-tools', 'apt-get -o DPkg::Lock::Timeout=600 update -qq; '
              'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq curl sudo', 600, True)
        url = f'https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/{ref}/get.sh'
        install = (f'curl -fsSL {url} | sudo WAM_REF={ref} WIZARD_TAILSCALE_WAIT=1 bash')
        (vm / 'install-command.txt').write_text(install + '\n')
        guest('install', 'umask 022\n' + install, 3300)
        want = candidate if label == 'candidate' else baseline
        guest('installed-source', 'got="$(sudo -iu wizard git -C /home/wizard/wizard-app-manager rev-parse HEAD)"; '
              'echo "installed_source=$got"; test "$got" = ' + want)
        guest('sudo-list', 'sudo -l -U wizard')
        guest('sudoers', 'ls -l /etc/sudoers.d; grep -R -n NOPASSWD /etc/sudoers.d || true; '
              'if grep -R -E "NOPASSWD:[[:space:]]*ALL" /etc/sudoers.d; then exit 1; fi; '
              'test ! -e /etc/sudoers.d/90-wizard-wizard; '
              'stat -c "%U %G %a %n" /usr/local/libexec/wizard-os /usr/local/libexec/wizard-os/os-patch /etc/sudoers.d/wizard-os-helpers')
        guest('wizard-denied', 'if sudo -iu wizard sudo -n true; then exit 1; else echo PASS_WIZARD_GENERAL_SUDO_DENIED; fi')
        guest('agent', '''eng="$(docker ps -q --filter label=com.docker.compose.service=hermes-agent-engine)"
test -n "$eng"; test "$(printf '%s\n' "$eng" | wc -l)" -eq 1
docker exec -u 1000:1000 "$eng" sh -c 'id; test ! -S /var/run/docker.sock; test ! -S /run/containerd/containerd.sock; if sudo -n true; then exit 1; else echo PASS_AGENT_SUDO_DENIED; fi'
docker inspect --format '{{json .Mounts}}' "$eng"
''')
        guest('doctor', 'sudo -iu wizard bash -lc \'export PATH="$HOME/.local/bin:$PATH"; wam doctor\'', 300)
        guest('builder-doctor', 'sudo -iu wizard bash -lc \'export PATH="$HOME/.local/bin:$PATH"; wam builder-bundle doctor\'', 300)
        guest('links', 'sudo -iu wizard bash -lc \'export PATH="$HOME/.local/bin:$PATH"; wam links\'', 180)
        guest('operator', '''tailscale debug prefs | python3 -c 'import json,sys; op=json.load(sys.stdin).get("OperatorUser"); print("OperatorUser="+str(op)); sys.exit(0 if op=="wizard" else 1)'
sudo -iu wizard tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin).get("BackendState"); print("BackendState="+str(s)); print("No tailnet credentials supplied; authenticated app-link publication is NOT PROVEN")'
''')
        if label == 'candidate':
            guest('patch', 'sudo -iu wizard sudo -n /usr/local/libexec/wizard-os/os-patch', 1200)
            guest('patch-args-denied', 'if sudo -iu wizard sudo -n /usr/local/libexec/wizard-os/os-patch --help; then exit 1; else echo PASS_SUDO_ARGUMENTS_DENIED; fi; '
                  'if /usr/local/libexec/wizard-os/os-patch apply; then exit 1; else echo PASS_HELPER_ARGUMENTS_DENIED; fi')
            # Exercise the exact old grant's retirement on a root-prepared rerun.
            guest('legacy-rerun', f'''printf 'wizard ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/90-wizard-wizard
chmod 0440 /etc/sudoers.d/90-wizard-wizard
curl -fsSL https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/{ref}/scripts/prepare-fresh-user.sh -o /root/prepare-fresh-user.sh
bash /root/prepare-fresh-user.sh wizard https://github.com/humanitylabs-org/wizard-app-manager {ref}
test ! -e /etc/sudoers.d/90-wizard-wizard
sudo -l -U wizard
''', 600)
        guest('containers', "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'")
    finally:
        if proc.poll() is None:
            # Graceful ACPI/SSH shutdown first; bounded terminate/kill fallback.
            try:
                subprocess.run(ssh + ['shutdown -h now'], capture_output=True, timeout=15)
                proc.wait(timeout=60)
            except (subprocess.TimeoutExpired, OSError):
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=30)
        state['stopped'] = proc.poll() is not None
        state['qemu_exit'] = proc.returncode
        log.close(); save()


try:
    run('host-preflight', ['bash', '-lc', 'df -h /srv/work; free -m; test -r /dev/kvm; test -w /dev/kvm; uname -m'])
    if shutil.disk_usage(root).free < 200 * 1024**3:
        raise RuntimeError('Refusing: keep at least 200 GiB free on worker scratch')
    # Test both exact source commits before creating guests. Git worktrees retain
    # tracked-file semantics for portable-path checks (not a bare source export).
    run('venv', ['python3', '-m', 'venv', str(root / 'venv')])
    py = str(root / 'venv/bin/python')
    run('test-deps', [py, '-m', 'pip', 'install', 'pytest', 'pyyaml'], 300)
    for label, ref in [('baseline', baseline), ('candidate', candidate)]:
        tree = root / (label + '-source')
        run(label + '-worktree', ['git', '-C', str(source), 'worktree', 'add', '--detach', str(tree), ref])
        run(label + '-pytest', [py, '-m', 'pytest', '-q', 'tests', '--junitxml=' + str(root / (label + '-pytest.xml'))],
            timeout=1200, required=False, cwd=tree)
        xml = root / (label + '-pytest.xml')
        if xml.exists():
            suites = ET.parse(xml).getroot()
            receipt.setdefault('test_totals', {})[label] = [dict(s.attrib) for s in suites.iter('testsuite')]
            save()
        run(label + '-node', ['node', '--test', 'tests/runtipi-open.test.mjs'], timeout=180, required=False, cwd=tree)
    # Prove the two audit regressions fail on the unmodified baseline, after
    # its original suite's count is recorded. Never count injected tests there.
    regression = root / 'baseline-source/tests/test_fresh_privilege_audit.py'
    shutil.copyfile(root / 'candidate-source/tests/test_fresh_privilege_audit.py', regression)
    try:
        run('baseline-new-audit-regressions', [py, '-m', 'pytest', '-q',
            'tests/test_fresh_privilege_audit.py::test_fresh_install_has_no_passwordless_sudo',
            'tests/test_fresh_privilege_audit.py::test_wam_modules_never_call_sudo'],
            timeout=120, required=False, cwd=root / 'baseline-source')
    finally:
        regression.unlink()
    run('ssh-key', ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(private / 'key')])
    cache = root / 'cache'; cache.mkdir()
    image = cache / 'debian-12-genericcloud-amd64.qcow2'
    mirror = 'https://cloud.debian.org/images/cloud/bookworm/latest/'
    run('image-sums', ['curl', '-fsSL', '--retry', '3', mirror + 'SHA512SUMS', '-o', str(cache / 'SHA512SUMS')], 120)
    run('image-download', ['curl', '-fL', '--retry', '3', '--speed-limit', '100000', '--speed-time', '60',
                           mirror + image.name, '-o', str(image)], 1800)
    sums = (cache / 'SHA512SUMS').read_text().splitlines()
    expected = next(line.split()[0] for line in sums if line.split()[-1].lstrip('*./') == image.name)
    with image.open('rb') as f:
        actual = hashlib.file_digest(f, 'sha512').hexdigest()
    if actual != expected:
        raise RuntimeError('Cloud image checksum mismatch')
    receipt['cloud_image'] = {'url': mirror + image.name, 'sha512': actual}; save()
    guest_vm('baseline', baseline, image, private / 'key')
    guest_vm('candidate', 'feature/agent-privilege-separation', image, private / 'key')
    receipt['state'] = 'executed; inspect individual exits, baseline comparison and limitations'
except BaseException:
    receipt['state'] = 'blocked-or-failed'
    receipt['error'] = traceback.format_exc()
finally:
    # No guest credentials are published or retained in the evidence pack.
    shutil.rmtree(private)
    receipt['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save()
    print(json.dumps(receipt, indent=2))
if receipt['state'] == 'blocked-or-failed':
    sys.exit(1)
