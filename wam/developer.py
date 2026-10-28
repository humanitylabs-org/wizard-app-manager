"""Owner-only developer source and expiring host access (decisions 97/105).

No credential lives here: HTTP callers must pass owner.check; local writes require
an administrator to enter the protected manager CLI, never the agent transport.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import subprocess

HELPER = '/usr/local/libexec/wizard-os/server-access'
DEFAULT_SECONDS = 7 * 86400
RELEASE_REPO = 'https://github.com/humanitylabs-org/wizard-app-manager'


def read(home) -> dict:
    path = Path(home) / 'developer.json'
    return json.loads(path.read_text()) if path.exists() else {'enabled': False}


def save(home, doc):
    path = Path(home) / 'developer.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(doc) + '\n'); temp.chmod(0o600)
    if os.getuid() == 0:
        st = path.parent.stat(); os.chown(temp, st.st_uid, st.st_gid)
    temp.replace(path)


@contextmanager
def locked(home):
    home = Path(home)
    with (home / 'developer.lock').open('a') as lock:
        if os.getuid() == 0:
            st = home.stat(); os.fchown(lock.fileno(), st.st_uid, st.st_gid)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def support():
    path = Path('/etc/wizard-os/server-access-support.json')
    return json.loads(path.read_text()) if path.exists() else {'supported': True}


def helper(action, payload=None):
    if action not in ('grant', 'revoke', 'status'):
        raise ValueError('invalid server access action')
    supported = support()
    if not supported['supported']:
        if action == 'grant': raise ValueError(supported['reason'])
        return {**supported, 'active': False, 'available': True}
    p = subprocess.run(['sudo', '-n', HELPER, action],
                       input=json.dumps(payload or {}), capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise ValueError(p.stderr.strip() or 'server-access helper unavailable; administrator host preparation required')
    return json.loads(p.stdout)


def access_status():
    try:
        return helper('status')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'active': False, 'available': False, 'error': str(exc)}


def source(repo, ref):
    # HTTPS git only, no credentials, local paths, option injection or shell syntax.
    if not isinstance(repo, str) or not re.fullmatch(r'https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', repo):
        raise ValueError('source must be an HTTPS git repository without credentials')
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref) or '..' in ref or ref.endswith('/'):
        raise ValueError('invalid developer ref')
    return {'repo': repo, 'ref': ref}


def receipt_hash(home):
    path = Path(home) / 'release.json'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def status(home):
    from . import updates
    doc = read(home)
    selected = doc.get('source') if doc['enabled'] else {'repo': RELEASE_REPO, 'ref': 'releases/' + updates.channel(home)}
    checkout = Path(__file__).resolve().parents[1]
    def git(*args):
        p = subprocess.run(['git', '-C', str(checkout), *args], capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else None
    commit = git('rev-parse', 'HEAD')
    running_ref = git('symbolic-ref', '--short', 'HEAD') or 'detached'
    behind = git('rev-list', '--count', 'HEAD..refs/remotes/origin/main')
    banner = (f"Developer build: {running_ref} @ {commit or 'unknown'}, "
              f"{behind if behind is not None else 'unknown'} commits behind main. This box runs personal builds; automatic updates and stable guarantees stop.") if doc['enabled'] else None
    returning = bool(doc.get('return_pending')) and not (
        receipt_hash(home) != doc.get('release_receipt_before') and updates.installed(home))
    if returning:
        banner = 'Returning to release: a personal build may still be running until the release installer finishes. Check update status; retry wam developer off if it failed.'
    return {**doc, 'return_pending': returning, 'source': selected, 'banner': banner, 'commit': commit,
            'behind_main_reference': 'local origin/main (no network fetch)',
            'behind_main': int(behind) if behind is not None else None,
            'automatic_updates_paused': doc['enabled'], 'server_access': access_status()}


def configure(home, body):
    if set(body) - {'enabled', 'repo', 'ref'} or type(body.get('enabled')) is not bool:
        raise ValueError('send enabled and, when enabling, repo and ref')
    home = Path(home); home.mkdir(parents=True, exist_ok=True)
    with locked(home):
        if body['enabled']:
            doc = {'enabled': True, 'source': source(body.get('repo'), body.get('ref'))}
        else:
            # Fail closed: never report off while a root grant could remain.
            helper('revoke')
            doc = {'enabled': False, 'return_pending': True, 'release_receipt_before': receipt_hash(home)}
        save(home, doc)
    return status(home)


def access(home, body):
    if set(body) - {'action', 'seconds'} or body.get('action') not in ('grant', 'revoke'):
        raise ValueError('send grant or revoke and optional seconds')
    with locked(home):
        if body['action'] == 'grant':
            if not read(home)['enabled']:
                raise ValueError('Turn developer mode on before giving the agent full server access')
            seconds = body.get('seconds', DEFAULT_SECONDS)
            if type(seconds) is not int or not 1 <= seconds <= DEFAULT_SECONDS:
                raise ValueError('expiry must be 1..604800 seconds (at most seven days)')
            return helper('grant', {'seconds': seconds})
        return helper('revoke')


def migration_grant(home, agent_was_in_docker_group):
    """Slice 6 calls BEFORE removing docker membership; root enforces one use.

    This does not turn developer mode on. Extensions require it. Fresh container
    agents cannot use host sudo and are explicitly unsupported.
    """
    if not agent_was_in_docker_group:
        return {'active': False, 'skipped': 'agent was not in the docker group'}
    return helper('grant', {'seconds': DEFAULT_SECONDS, 'migration': True})


def health(home):
    doc = status(home); rows = []
    if doc['banner']:
        rows.append(('Developer build', False, doc['banner']))
    access = doc['server_access']
    if access.get('active'):
        rows.append(('Agent has full server access until ' + access['expires_at'], False,
                     'RED: root-equivalent access. Every sudo use is logged in /var/log/wizard-os-agent-sudo.log.'))
        if access.get('drift'):
            rows.append(('Server access security files changed', False, ', '.join(access['drift'])))
    elif access.get('error') and ((Path(home) / 'privsep.json').exists() or doc['enabled']):
        rows.append(('Server access status unavailable', False, access['error']))
    return rows


def apply_source(home, runner=subprocess.run):
    from . import updates
    doc = read(home)
    checkout = Path(__file__).resolve().parents[1]
    repo = doc['source']['repo'] if doc['enabled'] else RELEASE_REPO
    base_runner = runner
    state_owner = Path(home).stat().st_uid
    def manager_runner(argv, **kwargs):
        env = dict(kwargs.pop('env', os.environ))
        if os.getuid() == 0 and state_owner != 0:
            # Root authenticates the human, but never runs a personal checkout or
            # the installer at root. Execute as the protected manager identity.
            user = pwd.getpwuid(state_owner)
            argv = ['runuser', '-u', user.pw_name, '--', *argv]
            env.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name)
        env.update(WIZARD_OS_HOME=str(home), WAM_DIR=str(checkout))
        if (Path(home) / 'privsep.json').exists():
            env.update(WIZARD_SEPARATED='1', WIZARD_MODE='alongside', RUNTIPI_ROOT='/var/lib/wizard-os/runtipi')
        return base_runner(argv, env=env, **kwargs)
    runner = manager_runner
    # get.sh fetches origin on an existing checkout. Point it at the selected
    # fork (or back at the official source) before invoking that same installer.
    changed = runner(['git', '-C', str(checkout), 'remote', 'set-url', 'origin', repo],
                     capture_output=True, text=True, timeout=30)
    if changed.returncode:
        return {'started': False, 'error': (changed.stderr or changed.stdout)[-300:]}
    if not doc['enabled']:
        # Same installer and release selection as the ordinary Update button.
        return updates.apply(home, runner=runner, force=True, restore_release=True, installer=checkout / 'get.sh')
    env = {**os.environ, 'WAM_REPO': doc['source']['repo'], 'WAM_REF': doc['source']['ref'], 'WIZARD_STRICT': '0'}
    for key in ('WIZARD_RELEASE', 'WIZARD_RELEASE_PINS', 'WAM_IMAGE'):
        env.pop(key, None)
    p = runner(['bash', str(checkout / 'get.sh'), 'start'],
               env=env, capture_output=True, text=True, timeout=600)
    return {'started': p.returncode == 0, 'source': doc['source'],
            **({'error': (p.stderr or p.stdout)[-300:]} if p.returncode else {})}


def main(argv, home):
    p = argparse.ArgumentParser(prog='wam developer')
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('status'); sub.add_parser('apply')
    on = sub.add_parser('on'); on.add_argument('--repo', default=RELEASE_REPO); on.add_argument('--ref', required=True)
    off = sub.add_parser('off'); off.add_argument('--setting-only', action='store_true', help='restore release source without starting the installer')
    grant = sub.add_parser('grant'); grant.add_argument('--seconds', type=int, default=DEFAULT_SECONDS)
    sub.add_parser('revoke')
    args = p.parse_args(argv)
    try:
        if args.action == 'status':
            result = status(home)
        else:
            if os.getuid() != 0:
                raise ValueError('owner only: an administrator must run the box-local manager CLI as root')
            if args.action == 'on': result = configure(home, {'enabled': True, 'repo': args.repo, 'ref': args.ref})
            elif args.action == 'off':
                result = configure(home, {'enabled': False})
                if not args.setting_only: result['restore_release'] = apply_source(home)
            elif args.action == 'apply': result = apply_source(home)
            else: result = access(home, {'action': args.action, 'seconds': getattr(args, 'seconds', DEFAULT_SECONDS)})
        print(json.dumps(result, indent=2))
        return int(bool(result.get('error') or result.get('restore_release', {}).get('error')))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1
