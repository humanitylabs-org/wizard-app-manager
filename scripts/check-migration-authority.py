#!/usr/bin/python3 -I
"""Root-only, read-only legacy authority check from the protected source tree.

Ask the policy engine as root; never try to elevate the agent or execute a
command as it. Unknown policy errors fail closed before any runtime is stopped.
"""
import grp
import os
import pwd
import subprocess
import sys


def check(agent):
    if os.getuid() != 0:
        raise ValueError('An owner must run the migration authority check as root.')
    user = pwd.getpwnam(agent)
    if not user.pw_uid:
        raise ValueError('Cannot migrate a root agent.')
    groups = {grp.getgrgid(g).gr_name for g in os.getgrouplist(agent, user.pw_gid)}
    if groups & {'sudo', 'wheel', 'admin'}:
        raise ValueError('Remove administrator groups explicitly before migration; never silently revoke owner access.')
    # Listing queries policy only; it does not run true or use an agent ticket.
    # Reject even password-protected permission to run true as root. This is more
    # conservative than probing one cached/passwordless agent elevation.
    p = subprocess.run(['/usr/bin/sudo', '-n', '-l', '-U', agent, '--', '/usr/bin/true'],
                       capture_output=True, text=True, timeout=30,
                       env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if p.returncode == 0:
        raise ValueError('Existing sudo policy needs an owner review before migration.')
    # A command-specific listing denies with status 1 and no diagnostic (some
    # releases instead print an explicit policy denial). Never swallow errors.
    if p.returncode != 1 or (p.stderr.strip() and 'not allowed to run' not in p.stderr):
        raise ValueError('Cannot establish the existing sudo policy; owner review is required.')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('usage: check-migration-authority.py AGENT')
        check(sys.argv[1])
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
