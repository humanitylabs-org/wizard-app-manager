"""Run wam with the docker group even when its systemd user manager started before the group was granted.

The installer adds the owner to `docker` after their user manager may already be running (linger enabled first, or
a prepared box whose user was created before the install). Every user unit (the wam API, nightly updates, app
updates, backups) then inherits the manager's old groups, and each docker call falls back to `sudo -n docker`, which
fails without passwordless sudo ("isolated networks could not be prepared ... sudo: a password is required").
Restarting the user manager would also stop the owner's own Hermes, so instead wam re-executes itself under
`sg docker` once, the same way get.sh's install runner already picks up the group.
"""
from __future__ import annotations

import grp
import os
import pwd
import shlex
import shutil
import sys

GUARD = "WIZARD_WAM_SG"


def needs_docker_group(groups=None, user=None, members=None, docker_gid=None) -> bool:
    """True when this account is listed in /etc/group's docker group but this process does not carry that gid."""
    try:
        if docker_gid is None or members is None:
            g = grp.getgrnam("docker")
            docker_gid, members = g.gr_gid, list(g.gr_mem)
        user = user or pwd.getpwuid(os.getuid()).pw_name
        groups = os.getgroups() if groups is None else groups
    except (KeyError, OSError):
        return False
    return user in members and docker_gid not in groups and os.getgid() != docker_gid


def reexec_with_docker_group(argv=None, env=None, execvp=os.execvp) -> bool:
    env = os.environ if env is None else env
    if env.get(GUARD) or os.getuid() == 0 or not needs_docker_group():
        return False
    sg = shutil.which("sg")
    if not sg:
        return False
    argv = sys.argv[1:] if argv is None else argv
    env[GUARD] = "1"
    command = shlex.join([sys.executable, "-m", "wam", *argv])
    execvp(sg, [sg, "docker", "-c", "exec " + command])
    return True
