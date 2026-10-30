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
import runpy
import secrets
import shutil
import subprocess
import sys
import stat
from contextlib import contextmanager

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


IMAGE_KEYS = ('WAM_IMAGE', 'WIZARD_SHELL_IMAGE', 'WIZARD_UX_IMAGE', 'WIZARD_AGENT_SERVICE_IMAGE')


def image_overrides(environ):
    """Values, not executable environment: retain literal explicit image choices."""
    image = (r'(?:sha256:[0-9a-f]{64}|'
             r'(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/)*'
             r'[a-z0-9]+(?:[._-][a-z0-9]+)*'
             r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?'
             r'(?:@sha256:[0-9a-f]{64})?)')
    result = {}
    for key in IMAGE_KEYS:
        value = environ.get(key, '')
        if value:
            if len(value) > 512 or not re.fullmatch(image, value):
                raise SystemExit('invalid explicit image reference: ' + key)
            result[key] = value
    return result


def manager_environment(private_access, images):
    return {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': str(BASE), 'WIZARD_PRIVATE_ACCESS': private_access, **images}


@contextmanager
def protected_runtipi_setup(base, uid, gid):
    """Pin control directory inodes across the trusted upstream first setup.

    Refuse unsafe retained directories/files. Only a first upstream setup may
    normalize modes on the two already-pinned directory descriptors; never walk
    or chmod app data. No token is written until this context exits successfully.
    """
    opened = []
    def directory(parent, name):
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
            created = True
        except FileExistsError:
            created = False
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        opened.append(fd)
        if created:
            os.fchown(fd, uid, gid)
        st = os.fstat(fd)
        if st.st_uid not in (0, uid) or st.st_mode & 0o022:
            raise ValueError('unsafe retained host authority control directory')
        return fd, (st.st_dev, st.st_ino)
    def endpoints(fd):
        for name in ('wizard-host-approvals.json', '.wizard-host-authority.lock'):
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                    st.st_uid not in (0, uid) or st.st_mode & 0o022):
                raise ValueError('unsafe retained host authority control endpoint')
    try:
        basefd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.append(basefd)
        st = os.fstat(basefd)
        if st.st_uid not in (0, uid) or st.st_mode & 0o022:
            raise ValueError('unsafe host authority parent')
        rootfd, rootid = directory(basefd, 'runtipi')
        controlfd, controlid = directory(rootfd, 'user-config')
        endpoints(controlfd)
        try:
            os.stat('.env', dir_fd=rootfd, follow_symlinks=False)
            first_setup = False
        except FileNotFoundError:
            first_setup = True
        yield
        # A path replacement (even to another protected directory) is not ours.
        for parent, name, fd, identity in ((basefd, 'runtipi', rootfd, rootid),
                                           (rootfd, 'user-config', controlfd, controlid)):
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != identity:
                raise ValueError('host authority directory replaced during setup')
            current = os.fstat(fd)
            if current.st_uid not in (0, uid):
                raise ValueError('foreign host authority directory after setup')
            if first_setup:
                os.fchmod(fd, 0o755)
            elif current.st_mode & 0o022:
                raise ValueError('retained host authority directory became writable')
        endpoints(controlfd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def main():
    if os.getuid() != 0:
        raise SystemExit('An administrator must approve host preparation once.')
    private_access = os.environ.get('WIZARD_PRIVATE_ACCESS', 'tailscale')
    supplied_images = {k: os.environ.get(k, '') for k in IMAGE_KEYS}
    # Never resolve executables, Python/git hooks or shell startup in an agent's
    # inherited environment. get.sh invokes this with the isolated system Python.
    os.environ.clear()
    os.environ.update(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                      HOME='/root', WIZARD_PRIVATE_ACCESS=private_access)
    if private_access not in ('none', 'tailscale'):
        raise SystemExit('invalid private access mode')
    images = image_overrides(supplied_images)
    name, hh, ref = sys.argv[1:4]
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name) or not re.fullmatch(r'[A-Za-z0-9._/-]+', ref) or ref.startswith('-'):
        raise SystemExit('invalid account or source ref')
    migration = '--migration' in sys.argv[4:]
    if migration:
        journal = Path('/var/lib/wizard-os-migration/journal.json')
        j = json.loads(journal.read_text())
        if journal.stat().st_uid != 0 or journal.stat().st_mode & 0o077 or j.get('agent') != name or j.get('current') != 'prepare':
            raise SystemExit('Migration handoff requires the protected root journal')
    agent = pwd.getpwnam(name)
    if not agent.pw_uid or name == 'wizard-system':
        raise SystemExit('Separated alongside requires a non-root Hermes account.')
    # No root online copy, pairing rewrite, group probe, or Service change may
    # precede the separately authorized recovery-boot storage transaction.
    # get.sh fetches both reviewed files from the same trusted ref, under /run.
    storage_file = Path(__file__).with_name('protected-agent-storage.py')
    if not storage_file.is_file():
        raise SystemExit('maintenance-needed: protected storage preflight is missing')
    storage = runpy.run_path(str(storage_file))
    try: storage['require_mapping'](name, hh)
    except (ValueError, OSError, KeyError):
        raise SystemExit('maintenance-needed: selected Hermes storage must be relocated in root-only recovery first') from None
    # Do not silently change an existing account's privileges or layout (Slice 6).
    groups = {grp.getgrgid(g).gr_name for g in os.getgrouplist(name, agent.pw_gid)}
    if groups & {'sudo', 'wheel', 'admin'} or ('docker' in groups and not migration):
        raise SystemExit('Hermes already has administrator groups; migration requires a separate owner-approved step.')
    probe = subprocess.run(['runuser', '-u', name, '--', 'sudo', '-n', 'true'], capture_output=True)
    if probe.returncode == 0 and not migration:
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
    base_metadata = BASE.lstat()
    if (not stat.S_ISDIR(base_metadata.st_mode) or base_metadata.st_mode & 0o022 or
            base_metadata.st_uid not in (0, suid)):
        raise SystemExit('unsafe separated manager parent; refusing host preparation')
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
    env = manager_environment(private_access, images)
    with protected_runtipi_setup(BASE, suid, sgid):
        run(['bash', tree / 'scripts/prepare-named-user.sh', 'wizard-system',
             *(['--reassign-from', name] if migration else [])], env=env)
    # Fixed root helper is installed only in this owner-approved host phase.
    if not migration or not Path('/var/lib/wizard-os-server-access/migration-used').exists():
        run(['/usr/bin/python3', '-I', tree / 'scripts/install-server-access.py',
             tree / 'scripts/root-helpers/server-access', name, state], env=env)
    # The system account uses system units, not a lingering login manager.
    run(['loginctl', 'disable-linger', 'wizard-system'])
    try:
        files_group = grp.getgrnam('wizard-files')
    except KeyError:
        run(['groupadd', '--system', 'wizard-files']); files_group = grp.getgrnam('wizard-files')
    run(['usermod', '-aG', 'wizard-files', name])
    run(['usermod', '-aG', 'wizard-files', 'wizard-system'])
    files = BASE / 'runtipi/media'
    existed = files.exists(); files.mkdir(exist_ok=True)
    if not existed:
        os.chown(files, suid, files_group.gr_gid)
    os.chmod(files, 0o2770)  # preserve an already-running Files app's owner uid
    # Files initializes its root as gid 1000, so a named group ACL, inherited
    # by new folders, preserves the real owner's access across that startup.
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'install', '-y', '-qq', 'acl'])
    run(['setfacl', '-m', 'g:wizard-files:rwx,d:g:wizard-files:rwx,d:m:rwx', files])
    service_state = Path('/var/lib/wizard-agent') / str(agent.pw_uid)
    service_state.mkdir(parents=True, exist_ok=True)
    os.chown(service_state, agent.pw_uid, agent.pw_gid); os.chmod(service_state, 0o700)
    agents = state / 'agents'
    if agents.is_symlink(): raise SystemExit('Unsafe agent registry directory')
    agents.mkdir(mode=0o700, exist_ok=True)
    os.chown(agents, agent.pw_uid, agent.pw_gid)
    write(state / 'privsep.json', json.dumps({'schema': 1, 'agent_uid': agent.pw_uid, 'hermes_home': hh}), 0o600, suid, sgid)
    snapshot = {'mode': 'alongside', 'agent': {'external': True, 'id': 'host-hermes', 'name': 'host Hermes',
        'hermes_home': hh, 'origin': 'http://127.0.0.1:8642', 'uid': agent.pw_uid, 'gid': agent.pw_gid},
        'files_gid': files_group.gr_gid,
        'shell': {'port': 8788, 'share_history': False, 'share_sessions': False},
        'service': {'port': 8923, 'active': True}, 'images': {}}
    if migration:
        snapshot = json.loads((state / 'alongside.json').read_text())
        snapshot['files_gid'] = files_group.gr_gid
        snapshot['agent'].update(uid=agent.pw_uid, gid=agent.pw_gid, hermes_home=hh)
    if migration or not (state / 'alongside.json').exists():
        write(state / 'alongside.json', json.dumps(snapshot), 0o600, suid, sgid)
    if migration:
        # Already-installed apps do not pass install's override generator again.
        # Regenerate the protected service wiring rather than carrying the legacy
        # Hermes cache binds into the new policy context. Read only Compose
        # metadata for the saved loopback STT endpoint, never agent/app payloads.
        sys.path.insert(0, str(tree))
        from wam.network import load_yaml
        from wam.privsep import service_override
        override = BASE / 'runtipi/user-config/wizardapps/wizard-agent-service/docker-compose.yml'
        doc = load_yaml(override.read_text()) if override.is_file() else {}
        service_env = (((doc or {}).get('services') or {}).get('wizard-agent-service') or {}).get('environment') or {}
        write(override, service_override(snapshot, state, stt_origin=service_env.get('WIZARD_STT_ORIGIN')), 0o644, suid, sgid)
    for token in ('api-token', 'agent-token'):
        if not (state / token).exists():
            write(state / token, secrets.token_urlsafe(32) + '\n', 0o600, suid, sgid)
    sysenv = {**env, 'USER': 'wizard-system', 'LOGNAME': 'wizard-system', 'RUNTIPI_ROOT': str(BASE / 'runtipi'),
              'WIZARD_OS_HOME': str(state), 'WIZARD_MODE': 'alongside', 'WIZARD_SEPARATED': '1', 'WIZARD_TAILSCALE_WAIT': '1'}
    if migration:
        # Preserve the existing Serve graph; the ordinary installer must not
        # publish new links. Operator reassignment above is a separate root step.
        sysenv['WIZARD_PRIVATE_ACCESS'] = 'none'
    run(['runuser', '-u', 'wizard-system', '--', 'env', '-i', *[k + '=' + v for k, v in sysenv.items()], 'bash', tree / 'install.sh'], cwd=BASE)
    # Root installs the same persistent socket contract used by fresh boxes.
    # Worker restarts never release this address. No agent systemd authority.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='wizard-approval-', dir='/root') as staging:
        module = Path(staging) / 'approval_custody.py'
        shutil.copyfile(tree / 'wam/approval_custody.py', module)
        run(['/usr/bin/python3', '-I', tree / 'scripts/install-approval-custody.py',
             'install', 'wizard-system', state, tree, module], env=env)
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
    service_port = int((snapshot.get('service') or {}).get('port') or 8923)
    cfg_path = state / 'app-manager.json'
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    manager_port = int(cfg.get('port') or 7790)
    if not all(1 <= p <= 65535 for p in (service_port, manager_port)):
        raise SystemExit('Invalid saved App Manager or Agent Service port')
    handoff = {'url': f'http://127.0.0.1:{manager_port}', 'hermes_home': hh, 'service_port': service_port}
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
    run([*prefix, '/usr/local/bin/wizard', 'link', '--hermes-home', hh, '--url', f'http://127.0.0.1:{service_port}/wizard/mcp', '--restart'], cwd=agent.pw_dir)
    cmd = ['runuser', '-u', 'wizard-system', '--', 'env', '-i', *[k + '=' + v for k, v in sysenv.items()], '/usr/bin/python3', '-m', 'wam']
    for app in ('wizard-agent-service', 'wizard-ux'):
        run(cmd + ['install', app], cwd=tree)
    run(cmd + ['apps'], cwd=tree)
    print('System services prepared; Docker membership stays until migration doctor passes.' if migration else
          'Separated alongside installed. Hermes has only an agent-scoped wam token; no Docker/sudo/operator grant.')


if __name__ == '__main__':
    main()
