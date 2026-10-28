#!/usr/bin/python3 -I
"""Owner-approved host preparation only; never called by install.sh/updaters.

Usage: install-server-access.py HELPER_FILE AGENT|--fresh MANAGER_STATE
The invoking root bootstrap fetches HELPER_FILE from its selected trusted ref.
"""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile


def install(helper, agent, manager_home):
    if os.getuid() != 0: raise ValueError('administrator host preparation required')
    if agent != '--fresh':
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', agent) or agent == 'wizard-system' or not pwd.getpwnam(agent).pw_uid:
            raise ValueError('invalid non-root host agent')
    if not Path(manager_home).is_absolute() or '..' in Path(manager_home).parts:
        raise ValueError('absolute manager state required')
    def write(path, text, mode=0o644):
        path = Path(path)
        for p in reversed((path.parent, *path.parent.parents)):
            if p.is_symlink(): raise ValueError('unsafe privileged ancestor: ' + str(p))
            p.mkdir(exist_ok=True)
            if p.stat().st_uid != 0 or p.stat().st_mode & 0o022:
                raise ValueError('unsafe privileged ancestor: ' + str(p))
        s = path.parent.stat()
        if path.parent.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
            raise ValueError('unsafe privileged directory')
        fd, name = tempfile.mkstemp(prefix='.wizard-access-', dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, 'w') as f: f.write(text)
            os.replace(name, path)
        finally: Path(name).unlink(missing_ok=True)
    write('/usr/local/libexec/wizard-os/server-access', Path(helper).read_text(), 0o755)
    write('/etc/wizard-os/server-access.json', json.dumps({'agent': None if agent == '--fresh' else agent,
          'manager_home': str(manager_home)}), 0o600)
    write('/etc/wizard-os/server-access-support.json', json.dumps({
        'supported': agent != '--fresh',
        'reason': 'Full server access is unsupported for fresh hermes-agent containers: host sudo is not available. Use your owner SSH login.' if agent == '--fresh' else None}))
    write('/var/lib/wizard-os-server-access/installed', 'host-prepared\n', 0o600)
    line = 'wizard-system ALL=(root) NOPASSWD: /usr/local/libexec/wizard-os/server-access grant, /usr/local/libexec/wizard-os/server-access revoke, /usr/local/libexec/wizard-os/server-access status\n'
    # Validate before making a sudoers include effective.
    fd, candidate = tempfile.mkstemp(prefix='sudoers-', dir='/var/lib/wizard-os-server-access')
    try:
        with os.fdopen(fd, 'w') as f: f.write(line)
        subprocess.run(['/usr/sbin/visudo', '-cf', candidate], check=True)
        write('/etc/sudoers.d/90-wizard-server-access-helper', line, 0o440)
        subprocess.run(['/usr/sbin/visudo', '-c'], check=True)
    finally: Path(candidate).unlink(missing_ok=True)
    write('/etc/systemd/system/wizard-agent-access-expiry.service', '''[Unit]
Description=Expire Wizard agent full server access (root)
[Service]
Type=oneshot
User=root
ExecStart=/usr/local/libexec/wizard-os/server-access status
''')
    write('/etc/systemd/system/wizard-agent-access-expiry.timer', '''[Unit]
Description=Remove expired Wizard agent sudo grants
[Timer]
OnBootSec=1s
OnCalendar=*-*-* *:*:00,15,30,45
AccuracySec=1s
Persistent=true
[Install]
WantedBy=timers.target
''')
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True)
    subprocess.run(['/usr/bin/systemctl', 'enable', '--now', 'wizard-agent-access-expiry.timer'], check=True)
    # Host re-preparation never preserves a grant across an identity/layout change.
    subprocess.run(['/usr/local/libexec/wizard-os/server-access', 'revoke'], check=True)


if __name__ == '__main__':
    install(*sys.argv[1:])
