#!/usr/bin/python3 -I
"""Owner-approved host phase. No agent-callable privileged helper is installed.

Fetch this and approval_custody.py from the same trusted revision into a root-
owned staging directory. Fresh and alongside use the same system socket/service.
"""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


def run(*argv):
    return subprocess.run([str(a) for a in argv], check=True, capture_output=True, text=True, timeout=45)


def protected(path):
    for p in (path, *path.parents):
        if p.exists() or p.is_symlink():
            s = p.lstat()
            if p.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
                raise RuntimeError('unsafe root host-preparation path')


def write(path, text):
    protected(path)
    if path.exists() and path.read_text() == text:
        return
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o755)
        path.parent.chmod(0o755)
    staged = path.with_name('.' + path.name + '.' + str(os.getpid()))
    fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o644)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    staged.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def status():
    return json.loads(run('tailscale', 'serve', 'status', '--json').stdout)


def routes():
    snapshot = status()
    result = []
    for authority, site in snapshot.get('Web', {}).items():
        for handler in (site.get('Handlers') or {}).values():
            if ':7792' in str((handler or {}).get('Proxy', '')):
                if (set(site.get('Handlers', {})) != {'/'} or
                        site['Handlers']['/'].get('Proxy') != 'http://127.0.0.1:7792' or
                        not re.fullmatch(r'[a-z0-9.-]+:\d{2,5}', authority)):
                    raise RuntimeError('ambiguous existing approval route; administrator review required')
                result.append(authority)
    return sorted(set(result))


def main():
    if os.getuid() != 0 or len(sys.argv) != 6 or sys.argv[1] not in ('install', 'remove'):
        raise SystemExit('administrator usage: install-approval-custody.py install|remove ACCOUNT STATE TREE MODULE')
    action, account, state_arg, tree_arg, module_arg = sys.argv[1:]
    os.environ.clear()
    os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME='/root', LANG='C')
    user = pwd.getpwnam(account)
    if user.pw_uid == 0:
        raise RuntimeError('the manager must never run at root or an agent/session account')
    state, tree, module = map(Path, (state_arg, tree_arg, module_arg))
    for p in (state, tree):
        if not p.is_absolute() or not re.fullmatch(r'/[A-Za-z0-9._/-]+', str(p)) or '..' in p.parts:
            raise RuntimeError('unsupported manager path')
    # Only root-staged code may execute in this root host phase.
    protected(module)
    spec = importlib.util.spec_from_file_location('approval_custody', module)
    if spec is None or spec.loader is None:
        raise RuntimeError('invalid trusted custody module')
    custody = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(custody)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(state, user.pw_uid, user.pw_gid)
    state.chmod(0o700)
    lock = os.open(state / 'approval-origin.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(lock, user.pw_uid, user.pw_gid)
    finally:
        os.close(lock)
    with custody.publication_lock(state):
        return apply(action, user, account, state, tree, custody)


def apply(action, user, account, state, tree, custody):
    unitdir = Path('/etc/systemd/system')
    socket_path = unitdir / custody.SOCKET
    dropin = unitdir / 'tailscaled.service.d/50-wizard-owner-approval.conf'
    current = routes()  # failure to inspect persistent Serve config is a refusal
    if action == 'remove':
        # Never release the address first, even when the worker is broken.
        retired = []
        config = state / 'approval-origin.json'
        if config.exists():
            doc = json.loads(config.read_text())
            retired.extend(doc.get('retired', []))
            if doc.get('origin'):
                origin = doc['origin']
                if not isinstance(origin, str) or not re.fullmatch(r'https://[a-z0-9.-]+:\d{2,5}', origin):
                    raise RuntimeError('invalid saved approval origin')
                retired.append(origin)
                current = sorted(set([*current, origin.removeprefix('https://')]))
        for authority in current:
            run('tailscale', 'serve', '--https=' + authority.rsplit(':', 1)[1], 'off')
            if authority in status().get('Web', {}):
                raise RuntimeError('route removal was not acknowledged; socket retained')
            retired.append('https://' + authority)
        if routes():
            raise RuntimeError('approval routes remain; socket retained')
        # Persist retirement before releasing anything; old pages cannot be evicted.
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(state, user.pw_uid, user.pw_gid)
        staged = state / ('.approval-retirement-' + str(os.getpid()))
        fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            os.fchown(f.fileno(), user.pw_uid, user.pw_gid)
            json.dump({'custody': 1, 'retired': sorted(set(retired))}, f)
            f.flush(); os.fsync(f.fileno())
        staged.replace(config)
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        run('systemctl', 'stop', 'tailscaled.service')
        if custody._show('tailscaled.service').get('ActiveState') != 'inactive':
            raise RuntimeError('tailscaled did not stop; custody retained')
        run('systemctl', 'disable', '--now', custody.SERVICE)
        # Root-only explicit removal temporarily lifts manual-stop protection.
        write(socket_path, custody.SOCKET_TEXT.replace('RefuseManualStop=yes', 'RefuseManualStop=no'))
        run('systemctl', 'daemon-reload')
        run('systemctl', 'disable', '--now', custody.SOCKET)
        if custody._show(custody.SOCKET).get('ActiveState') != 'inactive':
            raise RuntimeError('socket removal did not acknowledge inactivity')
        socket_path.unlink()
        dropin.unlink(missing_ok=True)
        run('systemctl', 'daemon-reload')
        os.sync()  # persist unit retirement as well as the fsynced origin record
        print('Approval routes removed and acknowledged before socket release. Tailscaled remains stopped; administrator may restart it explicitly.')
        return
    if current:
        # Never upgrade an old routed bind into alleged continuous custody.
        # A previous genuine installation can be rerun only while custody holds.
        custody.require_custody()
    if not run('systemctl', 'show', 'tailscaled.service', '-p', 'LoadState', '--value').stdout.strip() == 'loaded':
        raise RuntimeError('system tailscaled service is required')
    write(socket_path, custody.SOCKET_TEXT)
    write(dropin, custody.TAILSCALE_TEXT)
    write(unitdir / custody.SERVICE, f'''[Unit]
Description=Wizard App Manager (protected approval activation)
After=docker.service network-online.target
Requires=docker.service
StartLimitIntervalSec=0

[Service]
User={account}
Group={user.pw_gid}
Environment=HOME={user.pw_dir}
Environment=WIZARD_OS_HOME={state}
Environment=RUNTIPI_ROOT={user.pw_dir}/runtipi
EnvironmentFile=-{state}/manager-service.env
WorkingDirectory={tree}
ExecStart=/usr/bin/python3 -m wam serve
Sockets={custody.SOCKET}
Restart=always
RestartSec=1s
UMask=0077

[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    # Occupied ports fail here, BEFORE any approval Serve mutation. The socket
    # stays independent of worker stop/restart/update; never PartOf the worker.
    run('systemctl', 'enable', '--now', custody.SOCKET)
    run('systemctl', 'enable', custody.SERVICE)
    # Include systemctl-created enablement symlinks and new parent directories,
    # before any later publication may rely on custody surviving a power loss.
    os.sync()
    custody.require_custody()
    print('Persistent approval socket custody ready; no approval origin published.')


if __name__ == '__main__':
    main()
