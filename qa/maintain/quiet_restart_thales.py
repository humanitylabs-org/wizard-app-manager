#!/usr/bin/env python3
"""Durable Thales worker entry point. Starts ONLY the disposable wos-maint-qa VM.
All package changes, restarts and reboots execute through limactl inside that VM.
"""
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'vm-evidence'
LIMA = '/opt/homebrew/bin/limactl'
VM = 'wos-maint-qa'
GUEST = '/opt/wam-quiet-qa'


def run(argv, *, check=True, timeout=1800, **kwargs):
    p = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, **kwargs)
    print(p.stdout, end='', flush=True)
    if p.stderr:
        print(p.stderr, file=sys.stderr, end='', flush=True)
    if check and p.returncode:
        raise RuntimeError(f'{argv!r} exited {p.returncode}')
    return p


def shell(*argv, **kwargs):
    return run([LIMA, 'shell', '--workdir=/', VM, '--', *argv], **kwargs)


def collect():
    p = subprocess.run([LIMA, 'shell', '--workdir=/', VM, '--', 'sudo', 'tar', '-C',
                        '/var/lib/wizard-maint-qa/evidence', '-cf', '-', '.'], capture_output=True, timeout=120)
    if p.returncode == 0:
        import io
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as archive:
            archive.extractall(OUT, filter='data')
    else:
        (OUT / 'collection-error.txt').write_bytes(p.stderr)


def main():
    assert str(ROOT).startswith('/Volumes/ThalesMedia/'), 'Heavy work must be on the external disk'
    OUT.mkdir(exist_ok=True)
    os.environ['PATH'] = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    os.environ['TMPDIR'] = str(ROOT / '.tmp')
    (ROOT / '.tmp').mkdir(exist_ok=True)
    run([LIMA, 'start', '--tty=false', VM], timeout=600)
    shell('sudo', 'mkdir', '-p', GUEST)
    tar = subprocess.Popen(['tar', '-C', str(ROOT), '--exclude=./vm-evidence', '--exclude=./.tmp', '-cf', '-', '.'], stdout=subprocess.PIPE)
    assert tar.stdout is not None
    copy = subprocess.run([LIMA, 'shell', '--workdir=/', VM, '--', 'sudo', 'tar', '-C', GUEST, '-xf', '-'], stdin=tar.stdout, timeout=180)
    tar.stdout.close()
    assert tar.wait() == 0 and copy.returncode == 0
    old_boot = shell('cat', '/proc/sys/kernel/random/boot_id').stdout.strip()
    pre = shell('sudo', 'python3', GUEST + '/qa/maintain/quiet_restart_vm.py', 'pre', check=False, timeout=3000)
    (OUT / 'pre.log').write_text(pre.stdout + pre.stderr)
    collect()
    if pre.returncode:
        raise RuntimeError('VM pre-reboot gates failed; retained evidence')
    # Bounded readiness check inside the durable worker, not an agent polling loop.
    deadline = time.monotonic() + 300
    new_boot = old_boot
    while time.monotonic() < deadline:
        time.sleep(5)
        probe = shell('cat', '/proc/sys/kernel/random/boot_id', check=False, timeout=25)
        if probe.returncode == 0 and probe.stdout.strip() != old_boot:
            new_boot = probe.stdout.strip()
            state = shell('systemctl', 'is-system-running', check=False, timeout=25)
            if state.stdout.strip() in ('running', 'degraded'):
                break
    assert new_boot != old_boot, 'VM reboot not observed within five minutes'
    post = shell('sudo', 'python3', GUEST + '/qa/maintain/quiet_restart_vm.py', 'post', check=False, timeout=180)
    (OUT / 'post.log').write_text(post.stdout + post.stderr)
    collect()
    print(json.dumps({'old_boot': old_boot, 'new_boot': new_boot, 'post_exit': post.returncode,
                      'evidence': str(OUT)}), flush=True)
    return post.returncode


if __name__ == '__main__':
    sys.exit(main())
