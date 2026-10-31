#!/usr/bin/python3
"""QA-only guest driver: ONE finite three-arm manager-start discriminator.

Only the admitted synthetic committed guest is supported (marker
/run/wizard-storage-production-synthetic). Never run on a host. No install.sh.

Arms run sequentially, each in its own Compose project with exact full-ID
tracking, a fresh synthetic DB bind directory and synthetic random credentials
generated here (never exported, never logged):
  A clean                 upstream v4.11.0 CLI template + CLI-shaped .env, image -> pinned
  B upstream-premigrated  as A, but the recorded upstream v4.11.0 manager first runs until
                          healthy (bounded), is stopped/removed by full ID, then pinned starts
  C product-wired         610 product wiring applied to the same CLI layout (see WIRING)
Every pinned start carries the read-only /diag preload (NODE_OPTIONS only; the
image, CMD and /app are unchanged). Container logs, env, healthcheck output and
.env stay in this VM; only the schema-validated export (enums, digests, counts)
leaves it.
"""
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

ROOT = Path('/root/storage-manager-start')
TREE = Path('/opt/storage-manager-start-source')
WORK = Path('/var/lib/msq-discriminator')        # 0755: the system user must traverse it (arm C)
SOCK = '/var/run/docker.sock'
PINNED = 'privsep-wam:595523fb'
SYSTEM_USER = 'wizard-system'                      # same name/uid allocation as prepare-alongside.py:191
GUEST_BUDGET_S = 2100                              # hard in-guest deadline for all three arms
POLL_S = 0.5
MIGRATION_EXISTS = "SELECT to_regclass('drizzle.__drizzle_migrations') IS NOT NULL"
MIGRATION_ROWS = "SELECT hash || ' ' || created_at FROM drizzle.__drizzle_migrations ORDER BY id LIMIT 257"
PSQL = 'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$0"'
CLEANUP_PROJECTS = ('msq-a', 'msq-b', 'runtipi')    # arm C uses install.sh:116's exact `-p runtipi`
ARM_PROJECT = {'A': 'msq-a', 'B': 'msq-b', 'C': 'runtipi'}
DIRS = ('apps', 'data', 'app-data', 'state', 'repos', 'media', 'traefik', 'user-config', 'logs', 'backups')  # CopySystemFiles
PERMS = (('777', ('state', 'data', 'apps', 'logs', 'traefik', 'repos', 'user-config')),         # EnsureFilePermissions
         ('666', ('state/settings.json',)), ('664', ('.env', 'docker-compose.yml', 'VERSION')),
         ('600', ('traefik/shared/acme.json', 'state/seed')))
CLI_VERSION = 'v4.11.0'

report = {'schema': 'privsep-manager-start-guest-v1', 'checks': [], 'first_error': None, 'commands': [],
          'cleanup_errors': [], 'phases': [], 'arm_cleanup': {}}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path); assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


_HERE = Path(__file__).resolve().parent
C = module('manager_start_collector', _HERE / 'collector.py' if (_HERE / 'collector.py').is_file()
           else _HERE / 'manager_start_collector.py')
HC = C.HC


def save():
    (ROOT / 'native.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    (ROOT / 'native.json').chmod(0o600)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run(label, args, timeout=60, required=True, cap=4194304, env=None, cwd=None):
    """Output can contain generated values or log text: hash it, never export it."""
    p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, env=env or {**os.environ, 'LANG': 'C'}, cwd=cwd)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate(timeout=15)
        report['commands'].append({'operation': label, 'exit': None, 'timeout': True}); save()
        if required:
            raise
        return None, b''
    report['commands'].append({'operation': label, 'exit': p.returncode, 'stdout_sha256': sha(out),
                               'stderr_sha256': sha(err), 'stdout_bytes': len(out), 'stderr_bytes': len(err)})
    save()
    if required:
        assert p.returncode == 0, 'fixed guest operation failed: ' + label
    return p.returncode, out[:cap]


def api(method, path, cap=4194304, timeout=30):
    """Docker Engine API over the unix socket; bounded raw body."""
    conn = http.client.HTTPConnection('localhost', timeout=timeout)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(timeout)
    try:
        sock.connect(SOCK); conn.sock = sock
        conn.request(method, path); r = conn.getresponse()
        raw = r.read(cap + 1)
        if len(raw) > cap:
            raise C.Violation('bounded daemon response exceeded')
        return r.status, raw
    finally:
        conn.close(); sock.close()


def api_json(method, path):
    status, raw = api(method, path)
    return status, (json.loads(raw) if raw else None)


def engine_id():
    status, info = api_json('GET', '/info'); assert status == 200 and isinstance(info.get('ID'), str) and info['ID']
    return info['ID']


def filt(**labels):
    return urllib.parse.quote(json.dumps({'label': [k + '=' + v for k, v in labels.items()]}, separators=(',', ':')))


def project_containers(project, service=None):
    lab = {'com.docker.compose.project': project}
    if service:
        lab['com.docker.compose.service'] = service
    status, rows = api_json('GET', '/containers/json?all=true&filters=' + filt(**lab))
    assert status == 200 and isinstance(rows, list)
    ids = [r['Id'] for r in rows]
    assert all(re.fullmatch('[0-9a-f]{64}', i) for i in ids) and len(set(ids)) == len(ids)
    return ids


def inspect(cid):
    status, doc = api_json('GET', '/containers/' + cid + '/json')
    assert status == 200 and doc['Id'] == cid, 'inspect_failed'
    return doc


# ----------------------------------------------------------------- images ---
def load_oci(name):
    """Re-verify the sealed archive chain (recorded index -> amd64 manifest -> blobs), load, bind ref->ID."""
    import tarfile
    doc = json.loads((ROOT / 'images.json').read_text())['images'][name]
    archive = ROOT / doc['archive']
    with tarfile.open(archive) as t:
        names = {m.name: m for m in t.getmembers()}
        def blob(d):
            return t.extractfile(names['blobs/sha256/' + d.split(':', 1)[1]]).read()
        idx_raw = blob(doc['recorded_index_digest']); assert 'sha256:' + sha(idx_raw) == doc['recorded_index_digest']
        idx = json.loads(idx_raw)
        amd = [m for m in idx['manifests'] if (m.get('platform') or {}).get('os') == 'linux'
               and m['platform'].get('architecture') == 'amd64' and not m['platform'].get('variant')]
        assert len(amd) == 1 and amd[0]['digest'] == doc['amd64_manifest_digest']
        man_raw = blob(doc['amd64_manifest_digest']); assert 'sha256:' + sha(man_raw) == doc['amd64_manifest_digest']
        man = json.loads(man_raw); assert man['config']['digest'] == doc['config_digest']
        assert [{'digest': l['digest'], 'size': l['size']} for l in man['layers']] == doc['layers']
        for d in [man['config']] + man['layers']:
            h = hashlib.sha256(); f = t.extractfile(names['blobs/sha256/' + d['digest'].split(':', 1)[1]])
            for chunk in iter(lambda: f.read(1048576), b''):
                h.update(chunk)
            assert 'sha256:' + h.hexdigest() == d['digest'], 'blob digest mismatch'
        li = json.loads(t.extractfile(names['index.json']).read())
        assert [m['digest'] for m in li['manifests']] == [doc['amd64_manifest_digest']]
    rc, out = run('load-oci-' + name, ['docker', 'load', '-i', archive], timeout=300)
    text = out.decode('utf-8', 'replace')
    ids = re.findall(r'^Loaded image ID: (sha256:[0-9a-f]{64})$', text, re.M)
    refs = re.findall(r'^Loaded image: (\S+)$', text, re.M)
    assert len(ids) + len(refs) == 1, 'exactly one loaded image'
    if ids:
        run('tag-oci-' + name, ['docker', 'tag', ids[0], doc['ref']], timeout=30)
    status, img = api_json('GET', '/images/' + urllib.parse.quote(doc['ref'], safe='') + '/json')
    assert status == 200
    kind = 'manifest' if img['Id'] == doc['amd64_manifest_digest'] else 'config' if img['Id'] == doc['config_digest'] else None
    assert kind, 'loaded image ID is not bound to the recorded digest chain'
    archive.unlink()
    report.setdefault('images', {})[name] = {'id': img['Id'], 'id_kind': kind, 'chain_verified': True}
    save()


# ----------------------------------------------------------------- layout ---
def cli_layout(root, extra_env=None):
    """Faithful runtipi CLI v4.11.0 setup (CopySystemFiles, GenerateEnvFile, EnsureFilePermissions).

    Synthetic credentials come from secrets (the CLI derives them from a random seed);
    they are written only to the in-guest .env and never read back or exported.
    """
    template = (ROOT / 'comparator-cli-template.yml').read_bytes()
    root.mkdir(mode=0o755, parents=True)
    (root / 'docker-compose.yml').write_bytes(template)
    (root / 'VERSION').write_text(CLI_VERSION)
    for d in DIRS:
        (root / d).mkdir(mode=0o755, exist_ok=True)
    (root / 'traefik/shared').mkdir(mode=0o755)
    (root / 'cache').mkdir(mode=0o755)                    # template bind ./cache (Docker would create it as root)
    (root / 'state/settings.json').write_text('{}')
    (root / 'state/seed').write_text(secrets.token_hex(32))
    env = {'INTERNAL_IP': '127.0.0.1', 'ARCHITECTURE': 'amd64', 'TIPI_VERSION': CLI_VERSION,
           'ROOT_FOLDER_HOST': str(root), 'NGINX_PORT': '80', 'NGINX_PORT_SSL': '443',
           'RUNTIPI_APP_DATA_PATH': str(root), 'POSTGRES_HOST': 'runtipi-db', 'POSTGRES_PORT': '5432',
           'POSTGRES_DBNAME': 'tipi', 'POSTGRES_USERNAME': 'tipi', 'POSTGRES_PASSWORD': secrets.token_hex(24),
           'REDIS_HOST': 'runtipi-redis', 'REDIS_PASSWORD': secrets.token_hex(24), 'DOMAIN': 'example.com',
           'LOCAL_DOMAIN': 'tipi.local', 'RABBITMQ_HOST': 'runtipi-queue', 'RABBITMQ_USERNAME': 'tipi',
           'RABBITMQ_PASSWORD': secrets.token_hex(24),
           'RUNTIPI_FORWARD_AUTH_URL': 'http://runtipi:3000/api/auth/traefik'}
    env.update(extra_env or {})
    (root / '.env').write_text(''.join(k + '=' + v + '\n' for k, v in env.items()))
    for perms, paths in PERMS:
        for p in paths:
            if (root / p).exists():
                run('cli-chmod', ['chmod', '-Rf', perms, root / p], timeout=30)
    return sorted(env)


def diag_files(arm):
    d = WORK / ('diag-' + arm); out = WORK / ('diag-out-' + arm)
    d.mkdir(mode=0o755); out.mkdir(mode=0o755); out.chmod(0o777)   # writable for whatever uid the image runs as
    for n in ('hook.mjs', 'loader.mjs'):
        shutil.copyfile(ROOT / n, d / n); (d / n).chmod(0o444)
    d.chmod(0o555)
    override = WORK / ('diag-' + arm + '.yml')
    override.write_text(json.dumps({'services': {'runtipi': {
        'environment': {'NODE_OPTIONS': '--import=/diag/hook.mjs'},
        'volumes': [str(d) + ':/diag:ro', str(out) + ':/diag-out']}}}))
    return override, out


def pinned_override(arm):
    p = WORK / ('pinned-' + arm + '.yml')
    p.write_text(json.dumps({'services': {'runtipi': {'image': PINNED}}}))
    return p


def compose(project, root, files):
    argv = ['docker', 'compose', '-p', project, '--project-directory', root]
    for f in files:
        argv += ['-f', f]
    return argv


# --------------------------------------------------------------- C wiring ---
def product_wiring(base, root):
    """610 product wiring BEFORE base start, as library/verbatim calls on synthetic paths.

    Field map (manager_start.md): prepare-alongside.py:191 system user; :218 protected_runtipi_setup;
    prepare-named-user.sh:25-29 box-only override, :34-35 ownership hand-off, :41 docker group;
    install.sh:77 app-data dir; install.sh:84 -> wam.release_pins.configure_manager (image,
    pull_policy; environment popped because WIZARD_RELEASE_PINS is unset exactly as in the capture).
    Approval custody (install-approval-custody.py) is Tailscale/systemd-socket only and changes no
    runtipi service field: excluded (No Tailscale rule).
    """
    pa = module('msq_prepare_alongside', TREE / 'scripts/prepare-alongside.py')
    try:
        pw = pwd.getpwnam(SYSTEM_USER)
    except KeyError:
        run('useradd-system', ['useradd', '--system', '--home-dir', base, '--shell', '/usr/sbin/nologin', SYSTEM_USER])
        pw = pwd.getpwnam(SYSTEM_USER)
    uid, gid = pw.pw_uid, pw.pw_gid
    base.mkdir(mode=0o755)
    with pa.protected_runtipi_setup(base, uid, gid):                                   # prepare-alongside.py:218
        if not (root / 'user-config/tipi-compose.yml').exists():                       # prepare-named-user.sh:26-29
            (root / 'user-config/tipi-compose.yml').write_text(named_user_box_only())
        keys = cli_layout_into(root, {'NGINX_PORT': '8480', 'NGINX_PORT_SSL': '8443', 'INTERNAL_IP': '127.0.0.1'})
        run('named-user-chown-root', ['chown', '%d:%d' % (uid, gid), root])            # prepare-named-user.sh:34
        run('named-user-chown-tree', ['find', root, '-mindepth', '1', '-maxdepth', '1', '!', '-name', 'data', '-exec',
                                      'chown', '-R', '%d:%d' % (uid, gid), '{}', '+'])   # prepare-named-user.sh:35
    run('named-user-docker-group', ['usermod', '-aG', 'docker', SYSTEM_USER])         # prepare-named-user.sh:41
    run('install-sh-77-app-data', as_system(root, ['mkdir', '-p', root / 'app-data/wizard-os']))
    ovr = root / 'user-config/tipi-compose.yml'
    rc, _ = run('install-sh-84-release-pins', as_system(root, ['/usr/bin/python3', '-m', 'wam.release_pins', ovr, PINNED]),
                cwd=TREE, required=False)
    assert rc == 0, 'wiring_invalid'
    modes = {n: oct((root / n).stat().st_mode & 0o7777) for n in ('.', '.env', 'user-config', 'state', 'data')}
    return {'override_sha256': sha(ovr.read_bytes()), 'image_matches_engine_id': False, 'base_env_keys': keys,
            'dir_modes_applied': modes, 'host_approvals_sync_rc': None}


def as_system(root, argv):
    """Exact helper sysenv shape (prepare-alongside.py:276-281, capture SYSENV_KEYS) minus tokens/custody."""
    sysenv = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': str(root.parent),
              'USER': SYSTEM_USER, 'LOGNAME': SYSTEM_USER, 'RUNTIPI_ROOT': str(root), 'WIZARD_MODE': 'alongside',
              'WIZARD_SEPARATED': '1', 'WIZARD_PRIVATE_ACCESS': 'none', 'WAM_IMAGE': PINNED,
              'WIZARD_OS_HOME': str(root.parent / 'state'), 'LANG': 'C'}
    return ['runuser', '-u', SYSTEM_USER, '--', 'env', '-i'] + [k + '=' + v for k, v in sysenv.items()] + list(argv)


def named_user_box_only():
    """Exact bytes prepare-named-user.sh:27-28 writes (parsed from the sealed 610-identical script)."""
    import shlex
    text = (TREE / 'scripts/prepare-named-user.sh').read_text()
    m = re.search(r"printf '%s\\n' (.*?) > \"\$base/user-config/tipi-compose\.yml\"", text, re.S)
    args = shlex.split(m.group(1).replace('\\\n', ' '))
    assert args[0] == 'services:' and len(args) == 5
    return ''.join(a + '\n' for a in args)


def cli_layout_into(root, extra):
    """cli_layout for an already-created (protected) root."""
    tmp = root.with_name(root.name + '.cli'); keys = cli_layout(tmp, extra)
    for p in tmp.iterdir():
        dst = root / p.name
        if p.name == 'user-config':
            for q in p.iterdir():
                shutil.move(str(q), dst / q.name)
            continue
        if p.name == '.env':
            text = p.read_text().replace(str(tmp), str(root)); dst.write_text(text); dst.chmod(0o664); continue
        shutil.move(str(p), dst)
    shutil.rmtree(tmp)
    run('cli-chmod-user-config', ['chmod', '-Rf', '777', root / 'user-config'], timeout=30)  # EnsureFilePermissions
    return keys


# ------------------------------------------------------------ observation ---
def wait_base(argv, budget):
    # install.sh:88 shape; the derived budget is enforced by the bounded subprocess timeout.
    rc, _ = run('base-up', argv + ['up', '-d', '--no-recreate', '--wait', 'runtipi-db', 'runtipi-queue'],
                timeout=budget, required=False)
    return rc == 0


def migrations(project, allow):
    ids = project_containers(project, 'runtipi-db')
    if len(ids) != 1:
        return C.migration_row(HC.migration_membership('unreadable', None, None, allow), allow)
    cid = ids[0]
    rc, out = run('migration-table-exists', ['docker', 'exec', cid, 'sh', '-c', PSQL, MIGRATION_EXISTS],
                  timeout=15, required=False, cap=64)
    if rc != 0 or out.strip() not in (b't', b'f'):
        m = HC.migration_membership('unreadable', None, None, allow)
    elif out.strip() == b'f':
        m = HC.migration_membership('table_absent', 0, None, allow)
    else:
        rc, out = run('migration-rows', ['docker', 'exec', cid, 'sh', '-c', PSQL, MIGRATION_ROWS],
                      timeout=15, required=False, cap=65536)
        m = HC.migration_membership('read' if rc == 0 else 'unreadable', rc if rc is not None and 0 <= rc <= 255 else -1,
                                    out, allow)
    return C.migration_row(m, allow)


def observe(project, window_s, since):
    """One bounded start window for the runtipi service already `up`-ed. Stops it by full ID afterwards."""
    t0 = time.monotonic()
    m = C.empty_manager(window_s)
    ids = project_containers(project, 'runtipi')
    if len(ids) != 1:
        raise C.Violation('manager_not_unique')
    cid = ids[0]; healthy_at = None; doc = inspect(cid)
    m['image_ref_is_pinned'] = doc['Config']['Image'] == PINNED
    while True:
        doc = inspect(cid); st = doc['State']
        if (st.get('Health') or {}).get('Status') == 'healthy':
            healthy_at = time.monotonic(); m['stop_reason'] = 'healthy'; break
        if doc.get('RestartCount', 0) >= C.EXIT_EVENTS_STOP - 1 and st['Status'] in ('exited', 'restarting', 'dead'):
            m['stop_reason'] = 'exits_observed'; break
        if st['Status'] in ('exited', 'dead') and (doc['HostConfig'].get('RestartPolicy') or {}).get('Name') in ('', 'no'):
            m['stop_reason'] = 'exits_observed'; break
        if time.monotonic() - t0 >= window_s:
            m['stop_reason'] = 'deadline'; break
        time.sleep(POLL_S)
    stop_ns = time.time_ns()
    m['elapsed_ms'] = min(int((time.monotonic() - t0) * 1000), 3600000)
    m['health_reached'] = healthy_at is not None
    m['health_ms'] = None if healthy_at is None else min(int((healthy_at - t0) * 1000), 3600000)
    status, _ = api('POST', '/containers/' + cid + '/stop?t=10', timeout=40)
    m['stopped_by_qa'] = status in (204, 304)
    doc = inspect(cid); st = doc['State']
    m['restart_count'] = min(int(doc.get('RestartCount', 0)), 9999)
    m['final_status'] = st['Status'] if st['Status'] in C.STATUSES else 'absent'
    m['final_exit_code'] = st['ExitCode'] if isinstance(st.get('ExitCode'), int) and -255 <= st['ExitCode'] <= 255 else None
    m['oom_killed'] = bool(st.get('OOMKilled'))
    q = 'since=%d&until=%d&filters=%s' % (int(since), int(time.time()) + 1, urllib.parse.quote(json.dumps(
        {'type': ['container'], 'container': [cid]}, separators=(',', ':'))))
    status, raw = api('GET', '/events?' + q, cap=262144, timeout=30)
    assert status == 200
    m['events'] = C.parse_events(raw, stop_ns)
    status, logs = api('GET', '/containers/' + cid + '/logs?stdout=true&stderr=true&tail=' + str(HC.LOG_TAIL),
                       cap=HC.LOG_BYTES * 4)
    assert status == 200
    m['log'] = HC.parse_log(logs, truncated=len(logs) > HC.LOG_BYTES)
    return cid, m


def read_diag(out_dir):
    rows = []; bad = 0
    for k in range(C.MAX_DIAG):
        p = out_dir / ('diag-%d.json' % k)
        if not p.exists():
            continue
        try:
            st = p.lstat()
            assert st.st_size <= C.DIAG_BYTES and p.is_file() and not p.is_symlink()
            rows.append(C.validate_diag(p.read_bytes()))
        except (C.Violation, AssertionError, OSError):
            bad += 1
    extra = [p for p in out_dir.iterdir() if not re.fullmatch(r'diag-[0-7]\.json', p.name)]
    return rows, bad + len(extra)


def remove_project(project):
    """Exact full-ID removal of this arm's containers + networks, with absence readback."""
    rep = {'containers': [], 'networks': [], 'absent': False}
    for cid in project_containers(project):
        status, _ = api('DELETE', '/containers/' + cid + '?force=true&v=true')
        assert status in (204, 404); rep['containers'].append(cid)
        status, _ = api('GET', '/containers/' + cid + '/json'); assert status == 404
    status, nets = api_json('GET', '/networks?filters=' + filt(**{'com.docker.compose.project': project}))
    assert status == 200
    for n in nets:
        assert re.fullmatch('[0-9a-f]{64}', n['Id'])
        status, _ = api('DELETE', '/networks/' + n['Id']); assert status in (204, 404); rep['networks'].append(n['Id'])
        status, _ = api('GET', '/networks/' + n['Id']); assert status == 404
    rep['absent'] = not project_containers(project)
    report['arm_cleanup'][project] = rep; save()
    return rep['absent']


# ------------------------------------------------------------- engine main ---
def engine_main():
    """sha256 + size of /app/main.mjs as the engine materialises the pinned image (no node, no network).

    One exact-ID container: created, run attached, removed by full ID with 404 readback. Only the
    64-hex digest and the decimal size are parsed; nothing else is exported.
    """
    res = {'state': 'not_attempted', 'sha256': None, 'size': None, 'matches_bundle': False}
    rc, out = run('engine-main-create', ['docker', 'create', '--network', 'none', '--entrypoint', '/bin/sh', PINNED,
                  '-c', 'sha256sum /app/main.mjs && stat -c %s /app/main.mjs'], timeout=60, required=False)
    raw = (out or b'').decode('ascii', 'replace').strip()
    if rc != 0 or not re.fullmatch('[0-9a-f]{64}', raw):
        res['state'] = 'failed'; return res
    try:
        rc, out = run('engine-main-start', ['docker', 'start', '-a', raw], timeout=120, required=False, cap=4096)
        m = re.fullmatch(r'([0-9a-f]{64})  /app/main\.mjs\n([0-9]{1,10})\n', (out or b'').decode('ascii', 'replace'))
        if rc == 0 and m:
            res.update(state='read', sha256=m.group(1), size=int(m.group(2)))
            res['matches_bundle'] = res['sha256'] == C.BUNDLE_SHA256 and res['size'] == C.BUNDLE_SIZE
        else:
            res['state'] = 'failed'
    finally:
        status, _ = api('DELETE', '/containers/' + raw + '?force=true'); assert status in (204, 404)
        status, _ = api('GET', '/containers/' + raw + '/json'); assert status == 404
    return res


def storage_driver():
    status, info = api_json('GET', '/info')
    d = info.get('Driver') if status == 200 and isinstance(info, dict) else None
    return d if isinstance(d, str) and re.fullmatch('[a-z0-9][a-z0-9_.-]{0,31}', d) else None


# --------------------------------------------------------------------- arms ---
def run_arm(arm, comp, allow, engine_backend, deadline):
    a = C.empty_arm(arm); a['insufficient_reason'] = 'none'
    project = ARM_PROJECT[arm]
    if arm == 'C':
        base = WORK / 'c-base'; root = base / 'runtipi'
    else:
        root = WORK / ('arm-' + arm.lower())
    try:
        if arm == 'C':
            try:
                a['wiring'] = product_wiring(base, root)
            except BaseException:
                a['insufficient_reason'] = 'wiring_invalid'; return a
            files = [root / 'docker-compose.yml', root / 'user-config/tipi-compose.yml']
        else:
            cli_layout(root)
            files = [root / 'docker-compose.yml']
        rc, out = run('render-' + arm, compose(project, root, files) + ['config', '--format', 'json', '--no-interpolate'],
                      timeout=30, required=False)
        a['basis'], refused = C.basis(json.loads(out)) if rc == 0 else (None, 'basis_refused')
        if refused:
            a['insufficient_reason'] = 'basis_refused'; return a
        b = a['basis']
        need = b['base_wait_s'] + b['manager_window_s'] + (b['upstream_window_s'] if arm == 'B' else 0) + 120
        if time.monotonic() + need > deadline:
            a['insufficient_reason'] = 'arm_not_run'; return a
        if not wait_base(compose(project, root, files), b['base_wait_s']):
            a['insufficient_reason'] = 'base_up_failed'
        for svc in ('runtipi-db', 'runtipi-queue'):
            ids = project_containers(project, svc)
            if len(ids) == 1:
                a['container_ids'][svc] = ids[0]
                st = inspect(ids[0])['State']
                if svc == 'runtipi-db':
                    a['db_healthy'] = (st.get('Health') or {}).get('Status') == 'healthy'
                else:
                    a['queue_running'] = st['Status'] == 'running'
        if a['insufficient_reason'] != 'none':
            return a
        if arm == 'C':                                                             # install.sh:114 (tolerated)
            rc, _ = run('install-sh-114-host-approvals', as_system(root, ['/usr/bin/python3', '-m', 'wam',
                        'host-approvals', 'sync']), cwd=TREE, required=False, timeout=120)
            a['wiring']['host_approvals_sync_rc'] = rc if rc is not None and 0 <= rc <= 255 else -1
        if not a['db_healthy']:
            a['insufficient_reason'] = 'db_not_healthy'; return a
        if not a['queue_running']:
            a['insufficient_reason'] = 'queue_not_running'; return a
        if arm == 'B':
            up = {'image_is_recorded_upstream': False, 'healthy': False, 'elapsed_ms': 0, 'stopped_by_id': False,
                  'removed_by_id': False, 'exit_code': None}
            a['upstream'] = up
            since = time.time() - 1
            rc, _ = run('upstream-up', compose(project, root, files) + ['up', '-d', '--no-deps', 'runtipi'], timeout=120, required=False)
            if rc != 0:
                a['insufficient_reason'] = 'upstream_up_failed'; return a
            ucid, um = observe(project, b['upstream_window_s'], since)
            a['container_ids']['upstream'] = ucid
            udoc = inspect(ucid)
            up['image_is_recorded_upstream'] = udoc['Image'] == report['images']['upstream']['id']
            up['healthy'] = um['health_reached']; up['elapsed_ms'] = um['elapsed_ms']
            up['stopped_by_id'] = um['stopped_by_qa']; up['exit_code'] = um['final_exit_code']
            status, _ = api('DELETE', '/containers/' + ucid + '?force=true')
            st2, _ = api('GET', '/containers/' + ucid + '/json')
            up['removed_by_id'] = status == 204 and st2 == 404
            if not up['image_is_recorded_upstream']:
                a['insufficient_reason'] = 'image_identity_mismatch'; return a
            if not up['healthy']:
                a['insufficient_reason'] = 'upstream_not_healthy'; return a
            if not (up['stopped_by_id'] and up['removed_by_id']):
                a['insufficient_reason'] = 'upstream_stop_failed'; return a
        a['migrations_before_manager'] = migrations(project, allow)
        if arm == 'B' and a['migrations_before_manager']['class'] != 'includes_upstream_only':
            a['insufficient_reason'] = 'upstream_not_migrated'; return a
        diag_yml, diag_out = diag_files(arm)
        mfiles = files + ([] if arm == 'C' else [pinned_override(arm)]) + [diag_yml]
        rc, out = run('manager-config-' + arm, compose(project, root, mfiles) + ['config', '--format', 'json'],
                      timeout=30, required=False, cap=1048576)
        try:
            svc = json.loads(out)['services']['runtipi'] if rc == 0 else {}
        except ValueError:
            svc = {}
        if svc.get('image') != PINNED:
            a['insufficient_reason'] = 'image_identity_mismatch' if rc == 0 else 'manager_up_failed'; return a
        since = time.time() - 1
        # install.sh:116 shape (`up -d --no-deps runtipi`) minus --wait: the bounded observer replaces it.
        rc, _ = run('manager-up-' + arm, compose(project, root, mfiles) + ['up', '-d', '--no-deps', 'runtipi'],
                    timeout=120, required=False)
        if rc != 0:
            a['insufficient_reason'] = 'manager_up_failed'; return a
        try:
            mcid, a['manager'] = observe(project, b['manager_window_s'], since)
        except C.Violation as e:
            a['insufficient_reason'] = 'manager_not_unique' if 'unique' in str(e) else 'events_unreadable'; return a
        a['container_ids']['runtipi'] = mcid
        if inspect(mcid)['Image'] != engine_backend or not a['manager']['image_ref_is_pinned']:
            a['insufficient_reason'] = 'image_identity_mismatch'; return a
        if a['wiring'] is not None:
            a['wiring']['image_matches_engine_id'] = True
        a['diag'], a['diag_invalid'] = read_diag(diag_out)
        a['diag_mechanism'] = C.diag_mechanism(a['diag'])
        a['migrations_after'] = migrations(project, allow)
        if a['diag_invalid']:
            a['insufficient_reason'] = 'diag_invalid'
        return a
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        report.setdefault('arm_errors', {})[arm] = type(e).__name__; save()
        a['insufficient_reason'] = 'arm_exception'; return a


def arms(backend_engine_id):
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', backend_engine_id)
    deadline = time.monotonic() + GUEST_BUDGET_S
    comp = json.loads((ROOT / 'comparator.json').read_text())
    allow = comp['migration_allowlist']
    assert C.canonical_sha(allow) == comp['migration_allowlist_sha256']
    template_sha = sha((ROOT / 'comparator-cli-template.yml').read_bytes())
    WORK.mkdir(mode=0o755); WORK.chmod(0o755)
    engine = engine_id(); errors = []
    report['phases'].append('engine-main'); save()
    main_identity = engine_main(); driver = storage_driver()
    out = {k: C.empty_arm(k) for k in C.ARMS}
    clean = True
    for arm in C.ARMS:
        if not clean:
            out[arm]['insufficient_reason'] = 'previous_arm_cleanup_failed'; continue
        report['phases'].append('arm-' + arm); save()
        out[arm] = run_arm(arm, comp, allow, backend_engine_id, deadline)
        try:
            clean = remove_project(ARM_PROJECT[arm])
        except BaseException:
            clean = False
        out[arm]['cleanup_absent'] = clean
        if not clean:
            errors.append('arm_cleanup_failed')
    stable = engine_id() == engine
    if not stable:
        errors.append('identity_changed')
    imgs = {n: report['images'][n] for n in ('postgres', 'rabbitmq', 'upstream')}
    status, img = api_json('GET', '/images/' + urllib.parse.quote(PINNED, safe='') + '/json'); assert status == 200
    env_names = [e.split('=', 1)[0] for e in (img.get('Config') or {}).get('Env') or []]
    backend = {'engine_image_id': img['Id'], 'matches_capture': img['Id'] == backend_engine_id,
               'image_env_node_options_present': 'NODE_OPTIONS' in env_names,
               'engine_main': main_identity, 'storage_driver': driver}
    doc = C.finish(out, imgs, backend, errors,
                   comp['migration_allowlist_sha256'], template_sha, stable)
    (ROOT / 'manager-start-export.json').write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n')
    (ROOT / 'manager-start-export.json').chmod(0o600)
    report['verdict'] = doc['verdict']; save()


def fresh(revision):
    assert re.fullmatch('[0-9a-f]{40}', revision)
    assert os.geteuid() == 0 and Path('/run/wizard-storage-production-synthetic').is_file()
    assert not Path('/var/lib/wizard-os').exists() and not WORK.exists()
    assert not Path('/usr/bin/tailscale').exists() and not Path('/var/lib/tailscale').exists()
    status, rows = api_json('GET', '/containers/json?all=true'); assert status == 200 and rows == []
    report['checks'].append({'name': 'fresh-synthetic-inputs', 'passed': True}); save()


def cleanup():
    """Exact-ID cleanup with an explicitly created unowned synthetic negative control (reused harness)."""
    m = module('msq_exact_cleanup', TREE / 'qa/privsep-storage/production_guest.py'); m.ROOT = ROOT
    m.CLEANUP_PROJECTS = CLEANUP_PROJECTS
    for p in CLEANUP_PROJECTS:
        try:
            remove_project(p)
        except BaseException as e:
            report['cleanup_errors'].append('project-' + type(e).__name__)
    engine = engine_id()
    present = [ref for ref in (PINNED, 'docker.io/library/postgres:14')
               if api('GET', '/images/' + urllib.parse.quote(ref, safe='') + '/json')[0] == 200]
    assert present, 'no locally loaded image for the unowned negative control'
    rc, out = run('create-unowned-control', ['docker', 'create', '--label', 'com.docker.compose.project=notes_wizardapps',
                  '--entrypoint=/bin/sh', present[0], '-c', 'exit 0'])
    raw = out.decode().strip(); assert re.fullmatch('[0-9a-f]{64}', raw)
    status, before = m.daemon_request('GET', '/containers/' + raw + '/json'); assert status == 200 and before['Id'] == raw
    try:
        m.cleanup_containers()
    except AssertionError:
        pass
    else:
        raise AssertionError('unowned negative was silently removed or ignored')
    first = json.loads((ROOT / 'container-cleanup.json').read_text())
    (ROOT / 'cleanup-negative.json').write_text(json.dumps(first, sort_keys=True, indent=2) + '\n')
    assert first['unowned_exact_ids'] == [raw] and not first['passed']
    status, after = m.daemon_request('GET', '/containers/' + raw + '/json'); assert status == 200 and after == before
    assert engine_id() == engine
    status, _ = m.daemon_request('DELETE', '/containers/' + raw + '?force=true'); assert status == 204
    status, gone = m.daemon_request('GET', '/containers/' + raw + '/json'); assert status == 404
    m.cleanup_containers()
    final = json.loads((ROOT / 'container-cleanup.json').read_text())
    assert final['passed'] and final['readback_absent'] and engine_id() == engine
    report['exact_cleanup'] = {'negative_id': raw, 'negative_survived_owned_cleanup': True,
                               'negative_removed_by_full_id_absent': True, 'engine_id_stable': True,
                               'owned_attempted': len(first['attempted_exact_ids'])}
    save()


def main():
    global report
    mode = sys.argv[1]
    assert mode in ('fresh', 'load', 'arms', 'cleanup')
    assert os.geteuid() == 0 and Path('/run/wizard-storage-production-synthetic').is_file()
    if (ROOT / 'native.json').exists():
        report = json.loads((ROOT / 'native.json').read_text())
    lock = os.open(ROOT / 'native.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    report['phases'].append(mode)
    try:
        if mode == 'fresh':
            assert not report['checks']; fresh(sys.argv[2])
        elif mode == 'load':
            for n in ('postgres', 'rabbitmq', 'upstream'):
                load_oci(n)
        elif mode == 'arms':
            assert 'arms' not in report['phases'][:-1], 'second discriminator attempt refused'
            with (ROOT / 'arms-private.log').open('wb') as out:
                oldout = os.dup(1); olderr = os.dup(2); os.dup2(out.fileno(), 1); os.dup2(out.fileno(), 2)
                try:
                    arms(sys.argv[2])
                finally:
                    os.dup2(oldout, 1); os.dup2(olderr, 2); os.close(oldout); os.close(olderr)
        else:
            cleanup()
    except BaseException as e:
        if mode == 'cleanup':
            report['cleanup_errors'].append(type(e).__name__)
        elif report['first_error'] is None:
            report['first_error'] = {'type': type(e).__name__, 'phase': mode}
        raise
    finally:
        save(); os.close(lock)


if __name__ == '__main__':
    main()
