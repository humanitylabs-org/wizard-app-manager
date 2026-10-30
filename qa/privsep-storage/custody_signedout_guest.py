#!/usr/bin/python3
"""ONE synthetic signed-out prerequisite observation, not owner/tailnet acceptance.

Run only by custody_signedout_run.py inside its new KVM overlay. The complete
source tree and exact installer/worker are immutable git-archive bytes. No CLI
replacement, auth input, identity success stub, Serve mutation or storage entry.
"""
import hashlib
import http.client
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path('/root/custody-signedout')
TREE = Path('/opt/custody-signedout-source')
STATE = Path('/home/custodyqa/.wizard-os')
UNITS = ('tailscaled.service', 'wizard-owner-approval.socket', 'wizard-app-manager-api.service')
CHECKS = ('synthetic-fresh-prerequisites', 'official-package-real-daemon',
          'signedout-status-and-serve-inspectable', 'exact-installer-socket-dependency',
          'actual-worker-unauthenticated-owner-refusal', 'signedout-no-publication-readback')
report = {'schema': 'custody-signedout-guest-v1', 'passed': False,
          'full_acceptance': False, 'owner_identity_positive_proven': False,
          'tailnet_positive_proven': False, 'checks': [], 'commands': [],
          'first_error': None, 'cleanup_errors': []}


def save():
    p = ROOT/'result.json'
    p.write_text(json.dumps(report, sort_keys=True, indent=2)+'\n')
    p.chmod(0o600)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run(label, argv, timeout=45, required=True):
    # Never print untrusted CLI errors, auth URLs, identity JSON or log tails.
    # Only fixed operation names, exits, byte counts and raw digests leave VM.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
                            'LANG': 'C', 'DEBIAN_FRONTEND': 'noninteractive',
                            'PYTHONDONTWRITEBYTECODE': '1'}, umask=0o022,
                       start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=10)
        report['commands'].append({'operation': label, 'exit': 124,
                                   'interrupted_group_killed': True})
        save()
        raise
    p = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    report['commands'].append({'operation': label, 'exit': p.returncode,
        'stdout_bytes': len(p.stdout), 'stderr_bytes': len(p.stderr),
        'stdout_sha256': sha(p.stdout), 'stderr_sha256': sha(p.stderr)})
    save()
    if required:
        assert p.returncode == 0, label
    return p


def show(unit):
    p = run('unit-'+unit, ['/usr/bin/systemctl', '--system', 'show', unit], required=False)
    d = dict(line.split('=', 1) for line in p.stdout.decode().splitlines() if '=' in line)
    # No Environment, ExecStart, status message, argv or journal collection.
    keys = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'FragmentPath',
            'DropInPaths', 'Requires', 'After', 'Before', 'TriggeredBy', 'Sockets',
            'NeedDaemonReload', 'UnitFileState', 'RefuseManualStop', 'Accept',
            'Listen', 'StartLimitIntervalUSec', 'TriggerLimitIntervalUSec', 'User', 'Group')
    return {k: d.get(k) for k in keys}


def snapshot(label):
    status = run(label+'-status', ['/usr/bin/tailscale', 'status', '--json'], required=False)
    serve = run(label+'-serve', ['/usr/bin/tailscale', 'serve', 'status', '--json'], required=False)
    prefs = run(label+'-prefs', ['/usr/bin/tailscale', 'debug', 'prefs'], required=False)
    item = {'status_exit': status.returncode, 'serve_exit': serve.returncode,
            'prefs_exit': prefs.returncode, 'units': {u: show(u) for u in UNITS}}
    report[label] = item
    save()
    # Parse failures remain refusal, never synthesized empty objects.
    d, s, p = (json.loads(x.stdout) for x in (status, serve, prefs))
    assert isinstance(d, dict) and isinstance(s, dict) and isinstance(p, dict)
    item['status'] = {'BackendState': d.get('BackendState'), 'Version': d.get('Version'),
        'TUN': d.get('TUN'), 'AuthURL_present_redacted': bool(d.get('AuthURL')),
        'CurrentTailnet_present': bool(d.get('CurrentTailnet')),
        'User_present': bool(d.get('User')),
        'Self_user_present': bool((d.get('Self') or {}).get('UserID')),
        'Self_tailnet_ips_present': bool((d.get('Self') or {}).get('TailscaleIPs'))}
    item['prefs'] = {k: p.get(k) for k in ('WantRunning', 'LoggedOut', 'ShieldsUp')}
    item['prefs'].update(OperatorUser_present=bool(p.get('OperatorUser')),
        AdvertiseRoutes_present=bool(p.get('AdvertiseRoutes')),
        AdvertiseTags_present=bool(p.get('AdvertiseTags')),
        Persist_present_redacted=bool(p.get('Persist')))
    # Preserve genuine Serve JSON only when wholly authority-free. Any unknown
    # nonempty shape is diagnostic refusal; its raw digest is already recorded.
    item['serve_json'] = s if s == {} else None
    item['serve_nonempty_refused'] = s != {}
    save()
    assert status.returncode == serve.returncode == prefs.returncode == 0
    assert d.get('BackendState') == 'NeedsLogin'
    assert not d.get('CurrentTailnet') and not d.get('User')
    assert not (d.get('Self') or {}).get('UserID') and not (d.get('Self') or {}).get('TailscaleIPs')
    assert not p.get('OperatorUser') and not p.get('AdvertiseRoutes') and not p.get('AdvertiseTags')
    assert s == {}, 'unknown/nonempty genuine Serve config: stop before installer'
    assert item['units']['tailscaled.service']['LoadState'] == 'loaded'
    assert item['units']['tailscaled.service']['ActiveState'] == 'active'
    return item


def source_closure():
    import stat
    import tarfile
    expected = set()
    with tarfile.open(ROOT/'source.tar') as archive:
        for m in archive:
            p = TREE/m.name
            s = p.lstat()
            assert s.st_uid == 0 and (stat.S_ISLNK(s.st_mode) or not s.st_mode & 0o022)
            expected.add(m.name.rstrip('/'))
            if m.isdir():
                assert stat.S_ISDIR(s.st_mode)
            elif m.issym():
                assert stat.S_ISLNK(s.st_mode) and os.readlink(p) == m.linkname
            else:
                assert m.isfile() and stat.S_ISREG(s.st_mode)
                stream = archive.extractfile(m)
                assert stream is not None
                assert sha(stream.read()) == sha(p.read_bytes())
                assert stat.S_IMODE(s.st_mode) == (m.mode & ~0o022)
    actual = {str(p.relative_to(TREE)) for p in TREE.rglob('*')}
    assert actual == expected
    report['exact_extracted_source_closure'] = True


def listeners(label):
    rows = []
    for f in ('/proc/net/tcp', '/proc/net/tcp6'):
        for row in Path(f).read_text().splitlines()[1:]:
            fields = row.split()
            if fields[3] == '0A':
                rows.append({'family': 'tcp6' if f.endswith('6') else 'tcp',
                    'address_hex': fields[1].split(':')[0],
                    'port': int(fields[1].split(':')[1], 16), 'uid': int(fields[7])})
    rows.sort(key=lambda r: (r['family'], r['port'], r['address_hex'], r['uid']))
    report[label] = rows
    save()
    return rows


def fresh():
    assert os.getuid() == 0 and Path('/run/wizard-storage-production-synthetic').is_file()
    source_closure()
    listeners('listeners_before')
    assert not Path('/var/lib/tailscale').exists()
    assert not Path('/usr/bin/tailscale').exists() and not Path('/usr/sbin/tailscaled').exists()
    facts = {u: show(u) for u in UNITS}
    report['units_before'] = facts
    assert all(d['LoadState'] == 'not-found' for d in facts.values())
    assert not Path('/etc/systemd/system/tailscaled.service.d').exists()
    for f in ('/proc/net/tcp', '/proc/net/tcp6'):
        assert not any(int(r.split()[1].split(':')[1], 16) in (7790, 7791, 7792)
                       and r.split()[3] == '0A' for r in Path(f).read_text().splitlines()[1:])
    assert not STATE.parent.exists()


def package():
    # Official Debian instructions: signed apt repository, no shell installer,
    # no `up`, `login`, auth key, operator assignment, enrollment or Serve setup.
    run('official-key-download', ['curl', '--fail', '--silent', '--show-error',
        '--proto', '=https', '--tlsv1.2', '--max-time', '60',
        'https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg',
        '-o', '/usr/share/keyrings/custodyqa-tailscale.gpg'], 75)
    report['official_key_sha256'] = sha(Path('/usr/share/keyrings/custodyqa-tailscale.gpg').read_bytes())
    Path('/etc/apt/sources.list.d/custodyqa-tailscale.list').write_text(
        'deb [signed-by=/usr/share/keyrings/custodyqa-tailscale.gpg] https://pkgs.tailscale.com/stable/debian bookworm main\n')
    apt = ['apt-get', '-o', 'DPkg::Lock::Timeout=60',
           '-o', 'Dir::Etc::sourcelist=/etc/apt/sources.list.d/custodyqa-tailscale.list',
           '-o', 'Dir::Etc::sourceparts=-']
    run('official-repo-update', apt+['update', '-qq'], 180)
    run('official-package-install', apt+['install',
        '--no-install-recommends', '-y', '-qq', 'tailscale'], 240)
    verify = run('package-file-verification', ['dpkg', '--verify', 'tailscale'])
    assert not verify.stdout and not verify.stderr
    run('real-daemon-start', ['/usr/bin/systemctl', 'start', 'tailscaled.service'])
    version = run('installed-package-version', ['dpkg-query', '-W', '-f=${Version}', 'tailscale']).stdout.decode()
    assert re.fullmatch(r'[A-Za-z0-9.+:~_-]+', version)
    report['package_version'] = version
    report['binary_sha256'] = {p: sha(Path(p).read_bytes()) for p in ('/usr/bin/tailscale', '/usr/sbin/tailscaled')}
    unit = show('tailscaled.service')
    report['packaged_native_unit_sha256'] = sha(Path(unit['FragmentPath'] or '').read_bytes())
    pid = int(unit['MainPID'] or '0')
    assert pid > 1 and os.path.samefile('/proc/'+str(pid)+'/exe', '/usr/sbin/tailscaled')
    report['daemon_executable_verified'] = True


def custody():
    # New synthetic manager; no inherited/real account or app data.
    run('synthetic-manager-account', ['useradd', '--system', '--create-home',
        '--home-dir', str(STATE.parent), '--shell', '/usr/sbin/nologin', 'custodyqa'])
    STATE.mkdir(mode=0o700)
    user = pwd.getpwnam('custodyqa')
    os.chown(STATE, user.pw_uid, user.pw_gid)
    # Existing supported runtime switch; this fixture has NO app installs,
    # recommendations or network reconciliation work. No production edit.
    env = STATE/'manager-service.env'
    env.write_text('WAM_NETWORK_WATCH=0\nPYTHONDONTWRITEBYTECODE=1\n')
    env.chmod(0o600)
    os.chown(env, user.pw_uid, user.pw_gid)
    run('exact-root-custody-installer', ['/usr/bin/python3', '-I', '-B',
        str(TREE/'scripts/install-approval-custody.py'), 'install', 'custodyqa',
        str(STATE), str(TREE), str(TREE/'wam/approval_custody.py')], 150)
    # This is the actual shipped module, no patched _show/status/syscalls.
    sys.path.insert(0, str(TREE))
    from wam import approval_custody
    inode = approval_custody.require_custody()
    report['custody_inode'] = inode
    report['loaded_dependency_facts'] = {u: show(u) for u in UNITS}
    ts = report['loaded_dependency_facts']['tailscaled.service']
    assert approval_custody.SOCKET in ts['Requires'].split()
    assert approval_custody.SOCKET in ts['After'].split()
    for p in (Path('/etc/systemd/system')/approval_custody.SOCKET,
              Path('/etc/systemd/system/tailscaled.service.d/50-wizard-owner-approval.conf'),
              Path('/etc/systemd/system')/approval_custody.SERVICE):
        st = p.lstat()
        report.setdefault('protected_units', {})[str(p)] = {
            'uid': st.st_uid, 'gid': st.st_gid, 'mode': st.st_mode & 0o777,
            'sha256': sha(p.read_bytes())}
        assert st.st_uid == 0 and not st.st_mode & 0o022


def worker():
    from wam import approval_custody
    # Real socket triggers exact generated python -m wam serve worker. Requests
    # are wholly unauthenticated, not a positive identity-header simulation.
    rows = []
    for method, path, body in (
        ('GET', '/developer', None),
        ('POST', '/intent', '{"action":"grant","seconds":60}'),
    ):
        c = http.client.HTTPConnection('127.0.0.1', 7792, timeout=30)
        try:
            c.request(method, path, body=body,
                      headers={} if body is None else {'Content-Type': 'application/json'})
            response = c.getresponse()
            raw = response.read(8193)
            assert len(raw) <= 8192
            d = json.loads(raw)
            rows.append({'method': method, 'path': path, 'status': response.status,
                         'error_is_owner_only': d == {'error': 'owner only'},
                         'body_sha256': sha(raw)})
            report['unauthenticated_requests'] = rows
            save()
            assert response.status == 403 and d == {'error': 'owner only'}
        finally:
            c.close()
    unit = show('wizard-app-manager-api.service')
    assert unit['ActiveState'] == 'active' and int(unit['MainPID'] or '0') > 1
    assert unit['User'] == 'custodyqa'
    status = Path('/proc/'+str(unit['MainPID'])+'/status').read_text()
    assert next(r for r in status.splitlines() if r.startswith('PPid:')).split()[1] == '1'
    assert approval_custody.require_custody() == report['custody_inode']
    report['worker_after_requests'] = unit
    assert not (STATE/'owner-login').exists()
    assert not (STATE/'owner-intents').exists()
    assert not (STATE/'approval-origin.json').exists()
    assert not (STATE/'runtipi-operator.json').exists()
    assert not (STATE/'developer.json').exists()
    report['owner_actions_no_state_created'] = True


def final_snapshot():
    snapshot('after_requests')
    current = listeners('listeners_after')
    for row in current:
        assert row in report['listeners_before'] or (
            row['family'] == 'tcp' and row['address_hex'] == '0100007F'
            and row['port'] in (7790, 7791, 7792))
    report['no_new_nonloopback_tcp_listener'] = True
    source_closure()


def cleanup():
    # Native daemon stop/readback BEFORE VM poweroff. Never call source removal
    # (retirement is outside this proof) or weaken socket manual-stop protection.
    run('cleanup-stop-daemon', ['/usr/bin/systemctl', 'stop', 'tailscaled.service'], 60)
    d = show('tailscaled.service')
    assert d['ActiveState'] == 'inactive' and d['MainPID'] == '0'
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal():
            continue
        try:
            assert not os.path.samefile(p/'exe', '/usr/sbin/tailscaled')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    report['native_daemon_stopped_before_vm_cleanup'] = True
    # Stop only the actual synthetic worker; socket remains with PID 1 until
    # poweroff. An absent/uninstalled worker is not a cleanup success claim.
    u = show('wizard-app-manager-api.service')
    if u['LoadState'] == 'loaded':
        run('cleanup-stop-worker', ['/usr/bin/systemctl', 'stop', 'wizard-app-manager-api.service'])
    save()


def main():
    assert sys.argv[1:] in (['probe'], ['cleanup'])
    if sys.argv[1] == 'cleanup':
        global report
        report = json.loads((ROOT/'result.json').read_text())
        # Stop this exact synthetic probe before native daemon cleanup, so an
        # outer SSH/deadline interruption cannot race a late package/start step.
        lock = os.open(ROOT/'probe.lock', os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pid = report['probe_pid']
                proc = Path('/proc', str(pid))
                assert (proc/'stat').read_text().split()[21] == report['probe_start_time']
                assert (proc/'cmdline').read_bytes().split(b'\0')[:-1] == [
                    b'/usr/bin/python3', b'-I', b'-B', os.fsencode(ROOT/'guest.py'), b'probe']
                assert proc.stat().st_uid == 0
                os.kill(pid, signal.SIGTERM)
                end = time.monotonic()+30
                while time.monotonic() < end:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.2)
                else:
                    raise TimeoutError('synthetic probe not quiescent')
        finally:
            os.close(lock)
        report = json.loads((ROOT/'result.json').read_text())
        if Path('/usr/sbin/tailscaled').exists():
            cleanup()
        else:
            report['daemon_never_installed'] = True
            save()
        return 0
    lock = os.open(ROOT/'probe.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    report['probe_pid'] = os.getpid()
    report['probe_start_time'] = Path('/proc/self/stat').read_text().split()[21]
    def interrupt(sig, frame):
        raise TimeoutError('bounded synthetic prerequisite interrupted')
    signal.signal(signal.SIGALRM, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    signal.alarm(900)
    phases = (fresh, package, lambda: snapshot('before_custody'), custody,
              worker, final_snapshot)
    try:
        for name, fn in zip(CHECKS, phases):
            report['phase'] = name
            save()
            fn()
            report['checks'].append({'name': name, 'passed': True})
            save()
        report['passed'] = True
    except BaseException as exc:
        report['checks'].append({'name': report['phase'], 'passed': False})
        report['first_error'] = {'type': type(exc).__name__, 'phase': report['phase'],
            'frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                       for f in traceback.extract_tb(exc.__traceback__)]}
        report['classification'] = 'diagnostic-prerequisite-refusal-not-full-acceptance'
    finally:
        signal.alarm(0)
        save()
        os.close(lock)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
