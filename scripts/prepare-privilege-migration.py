#!/usr/bin/python3 -I
"""Owner-approved host step. Fetch a trusted ref; NEVER run an agent checkout.

Usage: sudo /usr/bin/python3 -I prepare-privilege-migration.py AGENT HERMES_HOME REF [--authorize-invalidation]
Rollback is forbidden once the credential password-change intent starts.
Keep a separate root/admin login open: the selected account's sessions restart.
"""
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


def main():
    if os.getuid() != 0: raise SystemExit('An administrator must run this explicit host step.')
    os.environ.clear()
    os.environ.update(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', HOME='/root')
    authorize = sys.argv[4:] == ['--authorize-invalidation']
    if len(sys.argv) != 4 and not authorize: raise SystemExit(__doc__)
    agent, hh, ref = sys.argv[1:4]
    if (not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', agent) or
            not re.fullmatch(r'/[A-Za-z0-9._/-]+', hh) or '..' in Path(hh).parts or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref) or '..' in ref):
        raise SystemExit('Invalid account, Hermes home or source ref')
    if not pwd.getpwnam(agent).pw_uid or agent == 'wizard-system': raise SystemExit('A non-root host Hermes is required')
    control = Path('/var/lib/wizard-os-migration')
    control.mkdir(mode=0o700, exist_ok=True)
    if control.is_symlink() or control.stat().st_uid != 0 or control.stat().st_mode & 0o077:
        raise SystemExit('Unsafe migration control directory')
    tree = control/'source'
    if tree.is_symlink(): raise SystemExit('Unsafe protected source symlink')
    if not tree.exists():
        subprocess.run(['git','clone','--no-checkout','https://github.com/humanitylabs-org/wizard-app-manager.git',str(tree)],check=True)
    if not (control/'journal.json').exists():
        # Clone/checkout can be interrupted before the state machine's journal.
        # No runtime has moved yet, so completing this initial checkout is safe.
        subprocess.run(['git','-C',str(tree),'checkout','--detach',ref],check=True)
    # An interrupted run must retain the original source, not silently upgrade it.
    actual = subprocess.check_output(['git','-C',str(tree),'rev-parse',ref],text=True).strip()
    head = subprocess.check_output(['git','-C',str(tree),'rev-parse','HEAD'],text=True).strip()
    if head != actual: raise SystemExit('Resume using the original migration source ref')
    # The source directory stays root-owned. Service-account lifecycle calls use
    # the independent protected copy at /var/lib/wizard-os/wizard-app-manager.
    subprocess.run(['/usr/bin/python3','-I',str(tree/'scripts/wam-privilege'),'migrate',
                    '--agent',agent,'--hermes-home',hh,'--ref',ref,
                    *(['--authorize-invalidation'] if authorize else [])],check=True)


if __name__ == '__main__': main()
