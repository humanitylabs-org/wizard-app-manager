"""Explicit owner updates of a source-installed host Hermes to the box's release pin.

Never follows upstream main, updates containers, or runs from apply_if_auto. Run
from a terminal outside the gateway's service cgroup: the command waits for the
restart and owns verification/rollback. Backups contain secrets and stay private.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import uuid

from . import alongside, updates, hermes_lock

TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
COMMIT = re.compile(r"[0-9a-f]{40}")
RESERVE_BYTES = 512 * 1024 * 1024


def _json(path: Path) -> dict:
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError(f"{path.name} must be an object")
    return doc


def _save(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(doc, out, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _run(*argv, cwd=None, check=True, timeout=600, env=None) -> str:
    r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode:
        # Do not persist stderr: installers can echo credential-bearing URLs/config.
        raise RuntimeError(f"{Path(str(argv[0])).name} failed (exit {r.returncode})")
    return r.stdout.strip() if r.returncode == 0 else ''


def _git(repo: Path, *args, **kw) -> str:
    return _run('git', '-C', repo, *args, **kw)


def target(home: Path, base: str | None = None) -> dict | None:
    release = updates.installed(home)
    if not release:
        return None
    base = (base or os.environ.get('WIZARD_RELEASE_BASE') or updates.RELEASE_BASE).rstrip('/')
    with urllib.request.urlopen(f'{base}/{release}.json', timeout=10) as response:
        doc = json.loads(response.read(1024 * 1024))
    pin = doc.get('hermes')
    if pin is None:
        return None
    if not isinstance(pin, dict) or not isinstance(pin.get('tag'), str) or not TAG.fullmatch(pin['tag']):
        raise ValueError('the release needs a final Hermes version tag')
    if not isinstance(pin.get('commit'), str) or not COMMIT.fullmatch(pin['commit']):
        raise ValueError('the release needs a full Hermes commit')
    hermes_lock.check_pin(pin)
    return pin


def _installation(home: Path) -> dict:
    # No fallback to this process's HERMES_HOME: only the explicitly linked host.
    path = alongside.state_path(home)
    if not path.exists():
        return {'install_type': 'container'}
    state = _json(path)
    if state.get('mode') != 'alongside':
        raise ValueError('cannot identify the linked host Hermes')
    agent = state.get('agent') or {}
    value = agent.get('hermes_home')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('alongside.json needs an absolute Hermes home')
    hh = Path(value)
    repo = hh / 'hermes-agent'
    origin = str(agent.get('origin') or '')
    # Only the port is configurable; never fetch health from a non-loopback host.
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', origin):
        raise ValueError('alongside.json needs a loopback Hermes API origin')
    git = ((repo / '.git').exists() and (repo / 'venv/bin/python').is_file()
           and _git(repo, 'rev-parse', '--show-toplevel', check=False) == str(repo.resolve()))
    return {'install_type': 'git' if git else 'manual', 'hermes_home': str(hh),
            'repo': str(repo), 'origin': origin, 'uid': agent.get('uid')}


def _health(origin: str) -> str | None:
    try:
        with urllib.request.urlopen(origin + '/health', timeout=3) as response:
            doc = json.loads(response.read(65536))
            if doc.get('status') not in (None, 'ok'):
                return None
            value = doc.get('version')
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, AttributeError):
        return None


def _version(value: str | None) -> tuple | None:
    if not value:
        return None
    match = re.fullmatch(r'v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:\+[^\s]+)?', value)
    return tuple(int(p) for p in match.groups()) if match else None


def receipt(home: Path) -> dict | None:
    st = updates._read_status(home / 'hermes-update.json')
    if st and st.get('state') == 'running':
        why = updates._runner_gone(st, updates.boot_id(), updates._booted_at())
        if why:
            healed = {**st, 'state': 'interrupted', 'reason': f'The Hermes update was interrupted ({why}); inspect its backup before retrying.'}
            try:
                with _lock(home):
                    cur = updates._read_status(home / 'hermes-update.json')
                    if cur == st:
                        _save(home / 'hermes-update.json', healed)
            except OSError:
                pass  # a live lock always wins; the projection is still useful on read-only homes
            return healed
    return st


def status(home: Path | None = None, base: str | None = None) -> dict:
    h = updates._home(home)
    out = {'state': 'no_pin', 'install_type': None, 'running_version': None, 'target_tag': None,
           'update_available': False, 'downgrade': False, 'dirty': False, 'receipt': receipt(h)}
    try:
        install = _installation(h)
        out.update(install)
        pin = target(h, base)
        if pin is None:
            return {**out, 'reason': 'This Wizard OS release has no Hermes pin; nothing to update.'}
        out.update(target_tag=pin['tag'], target_commit=pin['commit'])
        if install['install_type'] == 'container':
            return {**out, 'state': 'container', 'reason': "Hermes moves with the release's default app, not this command."}
        out['running_version'] = _health(install['origin'])
        if install['install_type'] == 'manual':
            return {**out, 'state': 'manual', 'reason': 'update this Hermes by hand (not a git checkout with a venv).'}
        repo = Path(install['repo'])
        out['commit'] = _git(repo, 'rev-parse', 'HEAD')
        out['checkout_tag'] = _git(repo, 'describe', '--tags', '--abbrev=0', check=False) or None
        out['running_version'] = out['running_version'] or out['checkout_tag']
        out['dirty'] = bool(_git(repo, 'status', '--porcelain', '--untracked-files=no'))
        have, want = _version(out['running_version']), _version(pin['tag'])
        assert want is not None  # target() accepts final numeric tags only
        out['downgrade'] = have is not None and want < have
        out['update_available'] = have is not None and want >= have and out['commit'] != pin['commit']
        if out['dirty']:
            return {**out, 'state': 'refused', 'reason': 'The Hermes checkout has uncommitted tracked changes; commit or stash them first.'}
        if out['downgrade']:
            return {**out, 'state': 'refused', 'reason': 'The release pin is older than running Hermes; downgrades are refused.'}
        if have is None:
            return {**out, 'state': 'refused', 'reason': 'Cannot determine the running Hermes version safely; update this Hermes by hand.'}
        # Health may still be old even if the checkout has already moved.
        out['update_available'] = out['update_available'] or want > have
        return {**out, 'state': 'update_available' if out['update_available'] else 'up_to_date',
                'reason': 'A pinned Hermes update is available.' if out['update_available'] else 'Hermes is at the release pin.'}
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as exc:
        return {**out, 'state': 'refused', 'reason': f'Cannot check Hermes: {exc}'}


def busy(home: Path, hh: Path) -> list[str]:
    reasons = []
    service = updates.service_origin(home)
    if service and updates.busy(service) is not False:
        reasons.append('The Agent Service is busy or unreadable.')
    gateway = hh / 'gateway_state.json'
    if gateway.exists():
        try:
            st = _json(gateway)
            if st.get('active_work'):
                reasons.append('Hermes has active gateway work.')
        except (OSError, ValueError):
            reasons.append('Hermes gateway state is unreadable.')
    probes = [(hh / 'state.db', 'session_turn_leases', 'expires_at > ?', (time.time(),)),
              (hh / 'state.db', 'async_delegations', "state IN ('pending','running')", ()),
              (hh / 'cron/executions.db', 'executions', "status='running' AND finished_at IS NULL", ())]
    for path, table, where, args in probes:
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                if not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
                    continue
                if db.execute(f'SELECT 1 FROM {table} WHERE {where} LIMIT 1', args).fetchone():
                    reasons.append(f'Hermes {table} has active work.')
        except (OSError, sqlite3.Error):
            reasons.append(f'Hermes {path.name} ({table}) is unreadable.')
    return reasons


def supervisor(hh: Path, repo: Path) -> dict | None:
    """Only select one active gateway whose effective service settings name this home/checkout."""
    found = []
    for scope in ('user', 'system'):
        prefix = ['systemctl'] + (['--user'] if scope == 'user' else [])
        try:
            units = _run(*prefix, 'list-units', '--all', '--plain', '--no-legend', 'hermes-gateway*.service', check=False)
            for row in units.splitlines():
                unit = row.split()[0]
                if not re.fullmatch(r'hermes-gateway[\w@.-]*\.service', unit):
                    continue
                text = _run(*prefix, 'show', unit, '--property=MainPID,ExecReload,ExecStart,Environment,WorkingDirectory,ActiveState,User')
                props = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
                if props.get('ActiveState') != 'active' or not props.get('MainPID', '').isdigit() or int(props['MainPID']) <= 0:
                    continue
                user = props.get('User')
                if user and user not in (str(os.getuid()), pwd.getpwuid(os.getuid()).pw_name):
                    continue
                env_home = re.search(r'(?:^|[\s\"])HERMES_HOME=([^\s\"]+)', props.get('Environment', ''))
                if env_home and Path(env_home[1]) != hh:
                    continue
                if str(repo) not in props.get('ExecStart', '') + props.get('WorkingDirectory', '') and not env_home:
                    continue
                found.append({'scope': scope, 'unit': unit, 'pid': int(props['MainPID']),
                              'usr1': bool(re.search(r'(?:SIG)?USR1\b', props.get('ExecReload', '')))})
        except (OSError, RuntimeError, subprocess.SubprocessError):
            continue
    if len(found) > 1:
        raise ValueError('Multiple Hermes gateways match this checkout; update this Hermes by hand.')
    return found[0] if found else None


def restart(route: dict) -> None:
    prefix = ['systemctl'] + (['--user'] if route['scope'] == 'user' else [])
    # The PID recorded before the quiet wait may already have exited. Never signal it blindly.
    pid = _run(*prefix, 'show', route['unit'], '--property=MainPID', '--value')
    if route['usr1'] and pid.isdigit() and int(pid) > 0:
        os.kill(int(pid), signal.SIGUSR1)
    else:
        _run(*prefix, 'restart', route['unit'])


def _wait_health(origin: str, version: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if _version(_health(origin)) == _version(version):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Hermes did not report {version} at /health within {timeout:g} seconds.')
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def _installer(repo: Path, *args) -> str:
    python = repo / 'venv/bin/python'
    uv = shutil.which('uv')
    if uv:
        return _run(uv, 'pip', *args, '--python', python, cwd=repo)
    return _run(repo / 'venv/bin/pip', *args, cwd=repo)


def _snapshot(h: Path, hh: Path, repo: Path, commit: str) -> Path:
    backup = h / 'backups' / ('hermes-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    source = hh / 'state.db'
    if source.exists():
        deadline = time.monotonic() + 120
        def progress(*_):
            if time.monotonic() > deadline:
                raise RuntimeError('Hermes database backup exceeded 120 seconds.')
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as src:
            src.execute('BEGIN')
            src.execute('SELECT count(*) FROM sqlite_master').fetchone()  # pin WAL read snapshot
            with closing(sqlite3.connect(backup / 'state.db')) as dst:
                src.backup(dst, pages=256, progress=progress)
        (backup / 'state.db').chmod(0o600)
        with closing(sqlite3.connect((backup / 'state.db').as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RuntimeError('Hermes database backup failed integrity_check.')
    present = []
    for src, name in ((hh / 'config.yaml', 'config.yaml'), (hh / '.env', '.env'),
                      (repo / 'install-stamp.json', 'install-stamp.json')):
        if src.exists():
            shutil.copy2(src, backup / name)
            (backup / name).chmod(0o600)
            present.append(name)
    (backup / 'freeze.txt').write_text(_installer(repo, 'freeze') + '\n')
    (backup / 'freeze.txt').chmod(0o600)
    _save(backup / 'snapshot.json', {'commit': commit, 'present': present})
    return backup


def _restore(backup: Path, hh: Path, repo: Path) -> None:
    present = _json(backup / 'snapshot.json')['present']
    for name, dst in (('config.yaml', hh / 'config.yaml'), ('.env', hh / '.env'),
                      ('install-stamp.json', repo / 'install-stamp.json')):
        if name in present:
            shutil.copy2(backup / name, dst)
        else:
            dst.unlink(missing_ok=True)
    # state.db is deliberately NOT restored: keep turns committed since the snapshot.


def _clear_drain(hh: Path, run_id: str) -> None:
    path = hh / '.drain_request.json'
    try:
        if _json(path).get('run_id') == run_id:
            path.unlink()
    except FileNotFoundError:
        pass


def _request_drain(hh: Path, out: dict) -> None:
    staged = hh / ('.wam-drain-' + out['run_id'])
    try:
        _save(staged, {'action': 'drain', 'run_id': out['run_id'], 'principal': 'wam-hermes-update',
                       'requested_at': out['updated_at'], 'suppress_notification': True})
        # Atomic publication without clobbering a concurrent maintenance owner's request.
        os.link(staged, hh / '.drain_request.json')
    finally:
        staged.unlink(missing_ok=True)


def _dependency_lines(lines: list[str], repo: Path | None = None) -> list[str]:
    # The editable Hermes line must not select a new checkout during rollback.
    return [line for line in lines if line and not line.startswith('#')
            and not (repo is not None and line == '-e ' + str(repo))
            and not re.search(r'(?:^|#egg=)hermes[-_]agent(?:\s|[=@<]|$)', line, re.I)]


def _distributions(lines: list[str]) -> set[str]:
    # freeze's canonical distribution identifiers, not URLs or editable paths.
    names = set()
    for line in _dependency_lines(lines):
        match = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==| @ )', line)
        if match:
            names.add(re.sub(r'[-_.]+', '-', match[1]).lower())
    return names


@contextmanager
def _lock(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / 'hermes-update.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)  # never unlink a flock inode: another process may already have opened it


def update(home: Path | None = None, base: str | None = None, *, dry_run: bool = False,
           wait_quiet: float = 1800, health_timeout: float = 600) -> dict:
    h = updates._home(home)
    if wait_quiet < 0 or health_timeout < 0:
        return {'state': 'refused', 'reason': 'Wait times must not be negative.'}
    if not dry_run:
        try:
            hermes_lock.authorize()
        except (ValueError, OSError) as exc:
            return {'state': 'refused', 'reason': str(exc)}
    try:
        with _lock(h):
            return _update(h, base, dry_run, wait_quiet, health_timeout)
    except BlockingIOError:
        return {'state': 'busy', 'reason': 'Another Hermes update holds the lock.'}
    except OSError as exc:
        return {'state': 'refused', 'reason': f'Cannot lock or record the Hermes update: {exc}'}


def _update(h: Path, base: str | None, dry_run: bool, wait_quiet: float, health_timeout: float) -> dict:
    st = status(h, base)
    previous = st.get('receipt') or {}
    if previous.get('state') in ('running', 'interrupted', 'rollback_failed'):
        return {**st, 'state': 'refused', 'reason': 'A previous Hermes update needs inspection; keep its receipt and backup before retrying.'}
    began = time.monotonic()
    out = {'state': 'running', 'from': st['running_version'], 'to': st['target_tag'], 'backup': None,
           'run_id': uuid.uuid4().hex,
           'pid': os.getpid(), 'boot_id': updates.boot_id(), 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'timings': {}, 'notes': []}
    def save(state: str, reason: str) -> dict:
        out.update(state=state, reason=reason, updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        out['timings']['elapsed_seconds'] = round(time.monotonic() - began, 3)
        if not dry_run:
            _save(h / 'hermes-update.json', out)
        return dict(out)
    if st['state'] in ('no_pin', 'manual', 'container', 'up_to_date'):
        return st
    if st['state'] == 'refused':
        return save('refused', st['reason'])
    repo, hh = Path(st['repo']), Path(st['hermes_home'])
    route = None
    old = st['commit']
    try:
        branch = _git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False) or 'main'
        if st.get('uid') != os.getuid() or hh.stat().st_uid != os.getuid():
            raise ValueError('Run wam hermes update as the linked Hermes owner, not a different user.')
        if dry_run:
            # Read-only: no fetch, backup, receipt, installer, signal or restart.
            resolved = _git(repo, 'rev-parse', '--verify', 'refs/tags/' + st['target_tag'] + '^{commit}', check=False)
        else:
            save('running', 'Checking the exact upstream release pin.')
            try:
                _git(repo, 'fetch', '--tags', 'origin')
            except RuntimeError:
                raise ValueError("release pin doesn't match upstream, or upstream could not be fetched") from None
            resolved = _git(repo, 'rev-parse', '--verify', 'refs/tags/' + st['target_tag'] + '^{commit}', check=False)
        if resolved != st['target_commit']:
            raise ValueError("release pin doesn't match upstream")
        size = sum(p.stat().st_size for p in (hh / 'state.db', hh / 'state.db-wal', hh / 'config.yaml', hh / '.env') if p.exists())
        if min(shutil.disk_usage(h).free, shutil.disk_usage(repo).free) < size * 2 + RESERVE_BYTES:
            raise ValueError('Not enough free disk space for a verified Hermes backup and dependency install.')
        route = supervisor(hh, repo)
        if not route:
            raise ValueError('No matching supervised Hermes gateway; update this Hermes by hand.')
        # Never perform self-restart from within the gateway's cgroup.
        cgroup = Path('/proc/self/cgroup')
        if cgroup.exists() and route['unit'] in cgroup.read_text():
            raise ValueError('Run this update outside the Hermes gateway service (for example in an owner terminal).')
        if dry_run:
            return save('dry_run', 'Preflight passed using local tags; no fetch, backup, install or restart was run.')
        out['backup'] = str(_snapshot(h, hh, repo, old))
        out['timings']['backup_seconds'] = round(time.monotonic() - began, 3)
        save('running', 'Backup verified; waiting for Hermes and the Agent Service to be quiet.')
        deadline = time.monotonic() + wait_quiet
        while True:
            reasons = busy(h, hh)
            if not reasons:
                break
            if time.monotonic() >= deadline:
                return save('busy', ' '.join(reasons) + ' Quiet-wait deadline reached; nothing was changed.')
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        # Revalidate after a potentially long wait: never overwrite new tracked edits or another checkout.
        fresh = status(h, base)
        if fresh['state'] == 'refused' or fresh.get('commit') != old or fresh.get('target_commit') != st['target_commit']:
            raise ValueError('Hermes or its release pin changed while waiting; nothing was changed.')
        backup = Path(out['backup'])
        for name, src in (('config.yaml', hh / 'config.yaml'), ('.env', hh / '.env'),
                          ('install-stamp.json', repo / 'install-stamp.json')):
            saved = backup / name
            if src.exists() != saved.exists() or (src.exists() and src.read_bytes() != saved.read_bytes()):
                raise ValueError('Hermes configuration changed while waiting; retry with a fresh backup.')
        if _installer(repo, 'freeze').strip() != (backup / 'freeze.txt').read_text().strip():
            raise ValueError('Hermes dependencies changed while waiting; retry with a fresh backup.')
        if busy(h, hh):
            return save('busy', 'Work arrived before the swap; nothing was changed.')
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        return save('refused', str(exc))
    try:
        if (hh / '.drain_request.json').exists():
            return save('busy', 'Another maintenance request is draining Hermes; nothing was changed.')
        _request_drain(hh, out)
        time.sleep(1)  # let the gateway observe admission closure, then census again before mutation
        if busy(h, hh):
            _clear_drain(hh, out['run_id'])
            return save('busy', 'Work arrived during admission closure; nothing was changed.')
    except (OSError, ValueError) as exc:
        _clear_drain(hh, out['run_id'])
        return save('refused', f'Cannot pause gateway admission: {exc}')
    try:
        save('running', 'Installing the pinned Hermes tag.')
        _git(repo, 'checkout', '-B', branch, 'refs/tags/' + st['target_tag'])
        _installer(repo, 'install', '-e', '.')
        if not (repo / 'hermes_cli/web_dist').is_dir():
            out['notes'].append('hermes_cli/web_dist is missing; no frontend build was run.')
        stamp = repo / 'install-stamp.json'
        if stamp.exists():
            doc = _json(stamp)
            doc['baseVersion'] = st['target_tag'][1:]
            _save(stamp, doc)
        _run(repo / 'venv/bin/python', '-c', 'import hermes_cli.main, gateway.run, run_agent, hermes_state',
             cwd=repo, env={**os.environ, 'HERMES_HOME': str(hh)})
        save('running', 'Restarting Hermes and checking the target version.')
        restart(route)
        _wait_health(st['origin'], st['target_tag'], health_timeout)
        if _git(repo, 'rev-parse', 'HEAD') != st['target_commit']:
            raise RuntimeError('Hermes checkout moved during the update.')
        _clear_drain(hh, out['run_id'])
        return save('updated', 'Hermes is running at the release pin; /health verified.')
    except Exception as exc:
        error = str(exc)
        try:
            save('running', 'The update failed; restoring the previous Hermes installation.')
            _git(repo, 'checkout', '-f', '-B', branch, old)
            backup = Path(out['backup'])
            _restore(backup, hh, repo)
            # Freeze may include an editable Hermes line. Restore dependency pins without
            # allowing that line to select another checkout, then re-register the old source.
            lines = (backup / 'freeze.txt').read_text().splitlines()
            deps = _dependency_lines(lines, repo)
            added = _distributions(_installer(repo, 'freeze').splitlines()) - _distributions(lines)
            if added:
                # Remove dependencies introduced by the failed update as well as restoring old pins.
                if shutil.which('uv'):
                    _installer(repo, 'uninstall', *sorted(added))
                else:
                    _installer(repo, 'uninstall', '-y', *sorted(added))
            requirements = backup / 'dependencies.txt'
            requirements.write_text('\n'.join(deps) + '\n'); requirements.chmod(0o600)
            _installer(repo, 'install', '--no-deps', '-r', requirements)
            _installer(repo, 'install', '--no-deps', '-e', '.')
            # Reuse the pinned service identity even if it is now inactive after a failed boot.
            restart(route)
            _wait_health(st['origin'], st['running_version'], health_timeout)
            _clear_drain(hh, out['run_id'])
            return save('rolled_back', f'The update failed ({error}); the old /health version is verified.')
        except Exception as rollback_error:
            return save('rollback_failed', f'The update failed ({error}); rollback failed ({rollback_error}). Keep the backup at {out["backup"]}.')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='wam hermes', description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('status').add_argument('--json', action='store_true')
    up = sub.add_parser('update')
    up.add_argument('--json', action='store_true')
    up.add_argument('--dry-run', action='store_true')
    up.add_argument('--wait-quiet', type=float, default=1800, metavar='SECONDS')
    args = ap.parse_args(argv)
    out = status() if args.command == 'status' else update(dry_run=args.dry_run, wait_quiet=args.wait_quiet)
    print(json.dumps(out, indent=2) if args.json else f'Hermes: {out["state"]} — {out["reason"]}')
    return 1 if args.command == 'update' and out['state'] in ('refused', 'busy', 'rolled_back', 'rollback_failed') else 0
