#!/usr/bin/env python3
"""Owner-approved root step for NEW named-user alongside installs only.

Fetch this from the same trusted ref as get.sh; never run an agent-owned copy as
root. Existing layouts are not migrated. No agent sudoers/groups are created.
"""
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import sys

BASE = Path('/var/lib/wizard-os')
REPO = 'https://github.com/humanitylabs-org/wizard-app-manager.git'


def run(argv, **kwargs):
    return subprocess.run([str(v) for v in argv], check=True, **kwargs)


def write(path, text, mode=0o644, uid=0, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f'refusing symlink {path}')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(path, mode)
    os.chown(path, uid, gid)


def main():
    if os.getuid() != 0:
        raise SystemExit('An administrator must approve host preparation once.')
    name, hh, ref = sys.argv[1:4]
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name) or not re.fullmatch(r'[A-Za-z0-9._/-]+', ref) or ref.startswith('-'):
        raise SystemExit('invalid account or source ref')
    agent = pwd.getpwnam(name)
    if not agent.pw_uid or name == 'wizard-system':
        raise SystemExit('Separated alongside requires a non-root Hermes account.')
    # Do not silently change an existing account's privileges or layout (Slice 6).
    groups = {grp.getgrgid(g).gr_name for g in os.getgrouplist(name, agent.pw_gid)}
    if groups & {'docker', 'sudo', 'wheel', 'admin'}:
        raise SystemExit('Hermes already has administrator groups; migration requires a separate owner-approved step.')
    probe = subprocess.run(['runuser', '-u', name, '--', 'sudo', '-n', 'true'], capture_output=True)
    if probe.returncode == 0:
        raise SystemExit('Hermes already has passwordless sudo; refusing to pretend this is isolated.')
    # String-only path validation; never follow/read the Hermes home as root.
    if not re.fullmatch(r'/[A-Za-z0-9._/-]+', hh) or '..' in Path(hh).parts:
        raise SystemExit('unsupported Hermes home path')
    # Probe existing layout at the account uid, not as root across home symlinks.
    old = subprocess.run(['runuser', '-u', name, '--', 'test', '-e', agent.pw_dir + '/.wizard-os/alongside.json'])
    if old.returncode == 0 and not (BASE / '.wizard-privsep').is_file():
        raise SystemExit('Existing alongside installs retain their layout; use the migration slice.')
    first = not BASE.exists()
    if not first and not (BASE / '.wizard-privsep').is_file():
        raise SystemExit('Existing /var/lib/wizard-os is not a separated layout; refusing adoption.')
    if BASE.is_symlink():
        raise SystemExit('refusing symlink system home')
    BASE.mkdir(mode=0o755, exist_ok=True)
    try:
        system = pwd.getpwnam('wizard-system')
        if system.pw_dir != str(BASE) or system.pw_uid == agent.pw_uid:
            raise SystemExit('wizard-system account does not match this layout')
    except KeyError:
        run(['useradd', '--system', '--home-dir', BASE, '--shell', '/usr/sbin/nologin', 'wizard-system'])
        system = pwd.getpwnam('wizard-system')
    suid, sgid = system.pw_uid, system.pw_gid
    os.chown(BASE, suid, sgid)
    write(BASE / '.wizard-privsep', name + '\n', uid=suid, gid=sgid)
    state = BASE / '.wizard-os'
    state.mkdir(mode=0o700, exist_ok=True); os.chown(state, suid, sgid)
    if (state / 'privsep.json').is_file():
        previous = json.loads((state / 'privsep.json').read_text())
        if previous['agent_uid'] != agent.pw_uid or previous['hermes_home'] != hh:
            raise SystemExit('This layout is already paired to another Hermes; refusing replacement.')
    tree = BASE / 'wizard-app-manager'
    if first or not tree.exists():
        run(['git', 'clone', '--branch', ref, REPO, tree]) if not re.fullmatch('[0-9a-f]{40}', ref) else (
            run(['git', 'clone', '--no-checkout', REPO, tree]), run(['git', '-C', tree, 'checkout', '--detach', ref]))
        run(['chown', '-R', f'{suid}:{sgid}', tree])
    else:
        # Reruns never reset/update code or read agent-provided repository paths.
        got = subprocess.check_output(['runuser', '-u', 'wizard-system', '--', 'git', '-C', tree, 'rev-parse', 'HEAD'], text=True).strip()
        print('Reusing protected App Manager checkout ' + got)
    # Reuse upstream root setup, but grant only the system identity its Docker and
    # Tailscale operator. No invocation of the agent's shell or login scripts.
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': str(BASE),
           'WIZARD_PRIVATE_ACCESS': os.environ.get('WIZARD_PRIVATE_ACCESS', 'tailscale')}
    run(['bash', tree / 'scripts/prepare-named-user.sh', 'wizard-system'], env=env)
    # The system account uses system units, not a lingering login manager.
    run(['loginctl', 'disable-linger', 'wizard-system'])
    try:
        files_group = grp.getgrnam('wizard-files')
    except KeyError:
        run(['groupadd', '--system', 'wizard-files']); files_group = grp.getgrnam('wizard-files')
    run(['usermod', '-aG', 'wizard-files', name])
    run(['usermod', '-aG', 'wizard-files', 'wizard-system'])
    files = BASE / 'runtipi/media'; files.mkdir(exist_ok=True)
    os.chown(files, suid, files_group.gr_gid); os.chmod(files, 0o2770)
    # Files initializes its root as gid 1000, so a named group ACL, inherited
    # by new folders, preserves the real owner's access across that startup.
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'install', '-y', '-qq', 'acl'])
    run(['setfacl', '-m', 'g:wizard-files:rwx,d:g:wizard-files:rwx,d:m:rwx', files])
    service_state = Path('/var/lib/wizard-agent') / str(agent.pw_uid)
    service_state.mkdir(parents=True, exist_ok=True)
    os.chown(service_state, agent.pw_uid, agent.pw_gid); os.chmod(service_state, 0o700)
    write(state / 'privsep.json', json.dumps({'schema': 1, 'agent_uid': agent.pw_uid, 'hermes_home': hh}), 0o600, suid, sgid)
    snapshot = {'mode': 'alongside', 'agent': {'external': True, 'id': 'host-hermes', 'name': 'host Hermes',
        'hermes_home': hh, 'origin': 'http://127.0.0.1:8642', 'uid': agent.pw_uid, 'gid': agent.pw_gid},
        'files_gid': files_group.gr_gid,
        'shell': {'port': 8788, 'share_history': False, 'share_sessions': False},
        'service': {'port': 8923, 'active': True}, 'images': {}}
    if not (state / 'alongside.json').exists():
        write(state / 'alongside.json', json.dumps(snapshot), 0o600, suid, sgid)
    for token in ('api-token', 'agent-token'):
        if not (state / token).exists():
            write(state / token, secrets.token_urlsafe(32) + '\n', 0o600, suid, sgid)
    sysenv = {**env, 'USER': 'wizard-system', 'LOGNAME': 'wizard-system', 'RUNTIPI_ROOT': str(BASE / 'runtipi'),
              'WIZARD_OS_HOME': str(state), 'WIZARD_MODE': 'alongside', 'WIZARD_SEPARATED': '1', 'WIZARD_TAILSCALE_WAIT': '1'}
    run(['runuser', '-u', 'wizard-system', '--', 'env', '-i', *[k + '=' + v for k, v in sysenv.items()], 'bash', tree / 'install.sh'], cwd=BASE)
    unit_env = f'Environment=HOME={BASE}\nEnvironment=WIZARD_OS_HOME={state}\nEnvironment=RUNTIPI_ROOT={BASE}/runtipi\n'
    write(Path('/etc/systemd/system/wizard-app-manager-api.service'), f'''[Unit]
Description=Wizard App Manager (privilege-separated)
After=docker.service network-online.target
Requires=docker.service
[Service]
User=wizard-system
Group=wizard-system
{unit_env}WorkingDirectory={tree}
ExecStart=/usr/bin/python3 -m wam serve
Restart=on-failure
UMask=0077
[Install]
WantedBy=multi-user.target
''')
    write(Path('/etc/systemd/system/wizard-runtipi.service'), f'''[Unit]
Description=Wizard Runtipi base
Requires=docker.service
After=docker.service network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
User=wizard-system
Group=wizard-system
WorkingDirectory={BASE}/runtipi
ExecStart=/usr/bin/docker compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d
ExecStop=/usr/bin/docker compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml stop
[Install]
WantedBy=multi-user.target
''')
    # Public executables are root-owned, never an agent-writable checkout.
    write(Path('/usr/local/bin/wam'), '#!/usr/bin/python3\n' + (tree / 'wam/client.py').read_text(), 0o755)
    wizard = Path('/usr/local/bin/wizard')
    if wizard.exists() or wizard.is_symlink(): wizard.unlink()
    wizard.symlink_to(tree / 'wizard-cli/wizard')
    run(['chmod', '+x', tree / 'wizard-cli/wizard'])
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'wizard-runtipi.service', 'wizard-app-manager-api.service'])
    # Hand off only the agent token; root never opens a file in the agent home.
    handoff = {'url': 'http://127.0.0.1:7790', 'hermes_home': hh, 'service_port': 8923}
    script = '''import json,os,pathlib,sys
h=pathlib.Path.home()/'.wizard-os'; h.mkdir(mode=0o700,exist_ok=True)
d=json.load(sys.stdin)
for name,value in [('client.json',json.dumps(d['client'])),('agent-token',d['token']+'\\n'),('alongside.json',json.dumps(d['alongside']))]:
 p=h/name; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w') as f: f.write(value)
 os.chmod(p,0o600)
'''
    agentenv = {'HOME': agent.pw_dir, 'USER': name, 'PATH': agent.pw_dir + '/.local/bin:' + env['PATH'], 'HERMES_HOME': hh}
    prefix = ['runuser', '-u', name, '--', 'env', '-i', *[k + '=' + v for k, v in agentenv.items()]]
    run([*prefix, '/usr/bin/python3', '-c', script], input=json.dumps({'client': handoff,
        'token': (state / 'agent-token').read_text().strip(), 'alongside': snapshot}).encode(), cwd=agent.pw_dir)
    # wizard link installs the stock plugin and API config as the Hermes uid only.
    run([*prefix, '/usr/local/bin/wizard', 'link', '--hermes-home', hh, '--url', 'http://127.0.0.1:8923/wizard/mcp', '--restart'], cwd=agent.pw_dir)
    cmd = ['runuser', '-u', 'wizard-system', '--', 'env', '-i', *[k + '=' + v for k, v in sysenv.items()], '/usr/bin/python3', '-m', 'wam']
    for app in ('wizard-agent-service', 'wizard-ux'):
        run(cmd + ['install', app], cwd=tree)
    run(cmd + ['apps'], cwd=tree)
    print('Separated alongside installed. Hermes has only an agent-scoped wam token; no Docker/sudo/operator grant.')


if __name__ == '__main__':
    main()
