#!/usr/bin/python3
"""QA-only guest driver: ONE finite installer-health capture around the REAL root helper.

Changed reproduction ``current-upstream-sealed-at-run``. The selected root
helper runs unchanged with its exact argv; the existing profile observer only
watches call/return of its own ``run`` and never replaces a command, callable,
return value, source, unit or environment. On a healthy line-291 return the QA
invocation stops (HealthyStop) before any post-291 helper action.

Only the admitted synthetic committed guest is supported. Never run on a host.
Installer output, Docker State.Error, Health.Log.Output, environment, labels and
log text stay inside this VM; only the schema-validated export and fixed guest
report (enums, digests, counts) are downloaded.
"""
import fcntl
import hashlib
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import runpy
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

ROOT = Path('/root/storage-health-capture')
TREE = Path('/opt/storage-health-capture-source')
BASE = Path('/var/lib/wizard-os')
RUNTIPI = BASE / 'runtipi'
CHECKOUT = BASE / 'wizard-app-manager'
SOCK = '/var/run/docker.sock'
PRE291_SECONDS = 900
SETUP_PATH = re.compile(rb'/tmp/tmp\.[A-Za-z0-9]{6,16}/setup\.sh')
SYSENV_KEYS = frozenset({'PATH', 'HOME', 'WIZARD_PRIVATE_ACCESS', 'USER', 'LOGNAME', 'RUNTIPI_ROOT',
                         'WIZARD_OS_HOME', 'WIZARD_MODE', 'WIZARD_SEPARATED', 'WIZARD_TAILSCALE_WAIT',
                         'WAM_IMAGE', 'WIZARD_AGENT_SERVICE_IMAGE'})   # exact helper sysenv (610:280-281)
MIGRATION_EXISTS = "SELECT to_regclass('drizzle.__drizzle_migrations') IS NOT NULL"
MIGRATION_ROWS = "SELECT hash || ' ' || created_at FROM drizzle.__drizzle_migrations ORDER BY id LIMIT 257"
PSQL = 'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$0"'
PROBE = ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '5',
         'http://localhost:3000/api/health']

report = {'schema': 'privsep-health-capture-guest-v1', 'checks': [], 'first_error': None,
          'commands': [], 'cleanup_errors': [], 'phases': []}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path); assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


_HERE = Path(__file__).resolve().parent
C = module('health_capture_collector', _HERE / 'collector.py' if (_HERE / 'collector.py').is_file()
           else _HERE / 'health_capture_collector.py')


class HealthyStop(BaseException):
    """Line 291 returned healthy: stop before any post-291 helper action."""


class Refused(BaseException):
    """Scope/basis refusal BEFORE install.sh is started; nothing is guessed."""
    def __init__(self, reason):
        super().__init__(reason); self.reason = reason


def save():
    (ROOT / 'native.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    (ROOT / 'native.json').chmod(0o600)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path, cap=268435456):
    h = hashlib.sha256(); n = 0
    with open(path, 'rb') as f:
        while True:
            raw = f.read(1048576)
            if not raw:
                break
            n += len(raw); assert n <= cap, 'file hash bound'
            h.update(raw)
    return h.hexdigest()


def run(label, args, timeout=60, required=True, cap=4194304):
    # Output can contain owner URLs or generated values: hash, never export it.
    p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, env={**os.environ, 'LANG': 'C'})
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate(timeout=15); raise
    report['commands'].append({'operation': label, 'exit': p.returncode, 'stdout_sha256': sha(out),
                               'stderr_sha256': sha(err), 'stdout_bytes': len(out), 'stderr_bytes': len(err)})
    save()
    assert len(out) <= cap, 'command output bound'
    if required:
        assert p.returncode == 0, label
    return p.returncode, out


# ------------------------------------------------------------ Docker API ---
class Api:
    """Guest-only unix-socket Docker API; bounded bytes; shared absolute deadline."""

    def __init__(self):
        self.deadline = None

    def timeout(self):
        if self.deadline is None:
            return 10.0
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise C.Violation('identity_api_unavailable')
        return max(0.1, min(5.0, left))

    def raw(self, path, cap, method='GET'):
        t = self.timeout()
        conn = http.client.HTTPConnection('localhost', timeout=t)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(t)
        try:
            sock.connect(SOCK); conn.sock = sock
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read(cap + 1)
            return resp.status, body[:cap], len(body) > cap
        except (OSError, http.client.HTTPException):
            raise C.Violation('identity_api_unavailable') from None
        finally:
            conn.close(); sock.close()

    def __call__(self, path, cap):
        status, body, over = self.raw(path, cap)
        if over:
            raise C.Violation('identity_response_bound')
        try:
            return status, (json.loads(body) if body else None)
        except ValueError:
            raise C.Violation('identity_response_bound') from None


API = Api()


def identity():
    status, info = API('/info', 262144)
    if status != 200 or not isinstance(info, dict):
        raise C.Violation('identity_api_unavailable')
    s = os.stat(SOCK)
    if not stat.S_ISSOCK(s.st_mode):
        raise C.Violation('identity_socket_changed')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return {'engine': info.get('ID'), 'boot': boot, 'socket': [s.st_dev, s.st_ino]}


# ---------------------------------------------------------------- processes ---
def proc_table():
    rows = {}
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal():
            continue
        try:
            raw = (p / 'stat').read_bytes(); args = (p / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        fields = raw[raw.rindex(b')') + 2:].split()
        rows[int(p.name)] = (int(fields[1]), [a for a in args if a])
    return rows


def descendants(root, table=None):
    table = table or proc_table(); out = []; frontier = [root]
    while frontier:
        cur = frontier.pop()
        for pid, (ppid, _) in table.items():
            if ppid == cur and pid not in out:
                out.append(pid); frontier.append(pid)
    return out


def quiesce(root_pids):
    """Stop exact descendants of our own installer subprocess tree; verify gone."""
    table = proc_table(); pids = []
    for r in root_pids:
        if r in table:
            pids += [r] + [d for d in descendants(r, table) if d not in pids]
    if not pids:
        return False                      # never claim a vacuous quiesce
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        end = time.monotonic() + 5
        while time.monotonic() < end and any(Path('/proc', str(p)).exists() and
                                             not _zombie(p) for p in pids):
            time.sleep(0.1)
    return not any(Path('/proc', str(p)).exists() and not _zombie(p) for p in pids)


def _zombie(pid):
    try:
        raw = Path('/proc', str(pid), 'stat').read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return raw[raw.rindex(b')') + 2:raw.rindex(b')') + 3] == b'Z'


def gates(root):
    """Finite active-gate enum from our installer's own Compose descendants (in guest only)."""
    table = proc_table(); out = []
    for pid in descendants(root, table):
        args = table[pid][1]
        if b'compose' not in args or b'up' not in args:
            continue
        if b'--wait' in args and b'runtipi-db' in args and b'runtipi-queue' in args:
            out.append('GATE88')
        elif b'--wait' in args and b'--no-deps' in args and args[-1] == b'runtipi':
            out.append('GATE116')
        elif args[-1] == b'runtipi-reverse-proxy' and b'--no-recreate' in args:
            out.append('LINE118')
    return sorted(set(out), key=C.GATES.index)


def no_manager_writer():
    for pid, (_, args) in proc_table().items():
        if pid == os.getpid():
            continue
        assert not (b'-m' in args and b'wam' in args), 'concurrent manager writer before installer'


# --------------------------------------------------------------- fresh mode ---
def fresh(revision):
    assert re.fullmatch('[0-9a-f]{40}', revision)
    assert os.geteuid() == 0 and Path('/run/wizard-storage-production-synthetic').is_file()
    assert not BASE.exists() and not Path('/usr/bin/tailscale').exists()
    assert not Path('/var/lib/tailscale').exists()
    no_manager_writer()
    s = module('health_source_inspector', TREE / 'qa/privsep-storage/custody_signedout_guest.py')
    s.ROOT = ROOT; s.TREE = TREE; s.report = report
    s.source_closure()
    report['exact_root_helper_argv'] = ['prepare-alongside.py', 'hermes', '/home/hermes/.hermes', revision]
    report['checks'].append({'name': 'fresh-synthetic-inputs', 'passed': True}); save()


def fetched_source_closure(revision):
    out = subprocess.check_output(['runuser', '-u', 'wizard-system', '--', 'git', '-C', str(CHECKOUT),
                                   'rev-parse', 'HEAD'], timeout=30).decode().strip()
    assert out == revision, 'root helper fetched a different revision'
    for row in json.loads((ROOT / 'source-members.json').read_text()):
        p = CHECKOUT / row['path']
        raw = os.fsencode(os.readlink(p)) if row['kind'] == 'symlink' else p.read_bytes()
        assert sha(raw) == row['sha256'], 'fetched source closure differs'
    report['root_fetched_complete_source_closure'] = True; save()


# ---------------------------------------------------------- capture state ---
class Capture:
    def __init__(self, comparator, backend):
        self.comp = comparator; self.backend = backend
        self.obs = C.Observer(API, identity, backend)
        self.st = {'install': {'return': 'not_reached', 'exception_type': None, 'returncode': None,
                               'line291_reached': False, 'pre291_command': None, 'quiesced': False},
                   'upstream': C.empty_upstream(), 'collector_errors': [], 'expected_backend': backend,
                   'allowlist_sha256': comparator['migration_allowlist_sha256']}
        self.setup_seen = threading.Event(); self.setup_stop = threading.Event(); self.setup_thread = None
        self.done = threading.Event(); self.sampler = None
        self.entry = None; self.sample_deadline = None; self.deadline = None
        self.watchdog = None; self.pre291_fired = False

    def err(self, code):
        assert code in C.COLLECTOR_ERRORS
        self.st['collector_errors'].append(code)

    # upstream setup observation (metadata only) ---------------------------
    def watch_setup(self):
        try:
            while not self.setup_stop.is_set():
                for pid, (ppid, args) in proc_table().items():
                    if len(args) >= 2 and args[0].endswith(b'bash') and SETUP_PATH.fullmatch(args[1]) \
                            and not self.setup_seen.is_set():
                        try:
                            raw = Path(os.fsdecode(args[1])).read_bytes()[:1048577]
                        except OSError:
                            continue
                        if len(raw) <= 1048576:
                            up = self.st['upstream']
                            up['setup_sha256'] = sha(raw); up['setup_bytes'] = len(raw)
                            up['setup_matches_staging_snapshot'] = sha(raw) == self.comp['setup_sha256']
                            self.setup_seen.set()
                self.setup_stop.wait(0.02)
        except BaseException:
            self.err('setup_script_unobserved')

    def after_named_user(self):
        up = self.st['upstream']
        cli = RUNTIPI / 'runtipi-cli'
        try:
            assert cli.is_file() and not cli.is_symlink()
            up['cli_sha256'] = file_sha(cli)
            up['cli_matches_v4_11_0'] = up['cli_sha256'] == self.comp['cli_v4_11_0_binary_sha256']
            rc, out = run('cli-version', ['runuser', '-u', 'wizard-system', '--', str(cli), '--version'],
                          timeout=15, required=False, cap=4096)
            m = C.VERSION.search(out.decode('utf-8', 'replace')) if rc == 0 else None
            if m and len(m.group(1)) <= 48:
                up['cli_version'] = m.group(1)
            else:
                self.err('cli_version_unparsed')
        except (AssertionError, OSError, subprocess.SubprocessError):
            self.err('cli_absent')
        try:
            up['compose_sha256'] = file_sha(RUNTIPI / 'docker-compose.yml', 4194304)
            up['compose_matches_cli_template'] = up['compose_sha256'] == self.comp['cli_template_sha256']
        except OSError:
            self.err('compose_absent')

    # line 291 entry --------------------------------------------------------
    def render_basis(self, env_keys):
        rc, out = run('render-merged-config', ['docker', 'compose', '-p', 'runtipi', '--project-directory',
                      str(RUNTIPI), '-f', str(RUNTIPI / 'docker-compose.yml'), '-f',
                      str(RUNTIPI / 'user-config/tipi-compose.yml'), 'config', '--format', 'json',
                      '--no-interpolate'], timeout=30, required=False)
        if rc != 0:
            return None, 'basis_render_failed'
        try:
            doc = json.loads(out)
        except ValueError:
            return None, 'basis_render_failed'
        return C.deadline_basis(doc, env_keys)

    def enter(self, argv):
        """Exact checked line-291 call. Everything here happens BEFORE install.sh starts."""
        t0 = time.monotonic()
        if self.watchdog:
            self.watchdog.cancel()
        if self.pre291_fired:
            raise Refused('install_line291_not_reached')
        self.st['install']['line291_reached'] = True; self.st['install']['return'] = 'refused'
        env = entry_env(argv, self.backend)
        if env is None:
            raise Refused('entry_identity_invalid')
        try:
            self.st['upstream']['override_entry_sha256'] = file_sha(RUNTIPI / 'user-config/tipi-compose.yml', 1048576)
        except OSError:
            pass
        try:
            self.obs.entry(0)
        except C.Violation as v:
            raise Refused(v.reason) from None
        self.env_keys = set(env)
        basis, refusal = self.render_basis(self.env_keys)
        self.obs.basis = basis
        if refusal:
            raise Refused(refusal)
        sampling = basis['gate88_s'] + basis['gate116_s'] + basis['allowance_sampling_s']
        self.entry = t0; self.sample_deadline = t0 + sampling
        self.deadline = t0 + basis['total_s']; API.deadline = self.deadline
        self.obs.max_ticks = int(math.ceil(sampling)) + 2
        self.st['install']['return'] = 'exception'
        self.sampler = threading.Thread(target=self.sample, daemon=True); self.sampler.start()

    def ms(self):
        return int((time.monotonic() - self.entry) * 1000)

    def own_children(self):
        """Direct children of THIS guest process: only the helper's own subprocess (line 291)."""
        me = os.getpid()
        return [pid for pid, (ppid, _) in proc_table().items() if ppid == me]

    def sample(self):
        try:
            while True:
                now = time.monotonic()
                if self.done.is_set():
                    if not (self.obs.incident or self.obs.violation):
                        self.obs.tick(self.ms(), [], exited=True)   # failure-bound state at real exit
                    break
                if now >= self.sample_deadline:
                    self.st['deadline_hit'] = True
                    if self.obs.gate_ms['GATE88'] is None:
                        self.st['pre291_budget_exhausted'] = True
                    break
                if self.obs.tick(self.ms(), gates(os.getpid())) == 'stop':
                    break
                self.done.wait(max(0.0, 1.0 - (time.monotonic() - now)))
            if not (self.obs.incident or self.obs.violation or self.done.is_set()):
                self.st['deadline_hit'] = True       # tick ceiling is part of the same deadline
            inc = self.obs.incident
            if inc and C.needs_probe(inc):
                self.st['probe'] = self.probe(inc['snapshot']['id'])
            if not self.done.is_set():
                if inc and not self.obs.violation:
                    # Let the real command fail on its own; never wait for green.
                    self.done.wait(max(0.0, min(self.sample_deadline, time.monotonic() + C.INCIDENT_GRACE) - time.monotonic()))
                if not self.done.is_set():
                    self.st['install']['quiesced'] = quiesce(self.own_children())
                    if not self.st['install']['quiesced'] and not self.done.is_set():
                        self.err('quiesce_incomplete')
        except BaseException:
            self.err('sampler_error')
            if self.obs.violation is None:
                self.obs.violation = 'collector_internal_error'

    def probe(self, cid):
        try:
            self.obs.check_identity()
            rc, out = run('fixed-health-probe', ['docker', 'exec', cid, *PROBE], timeout=10, required=False, cap=16)
            self.obs.check_identity()
            return C.probe_result(rc if rc is not None else -1, out)
        except BaseException:
            self.err('probe_failed'); return None

    # after the real command ----------------------------------------------
    def returned(self, ok):
        self.done.set()
        if self.sampler:
            self.sampler.join(timeout=max(1.0, (self.deadline or time.monotonic()) - time.monotonic() + 15))
            if self.sampler.is_alive():
                self.err('sampler_join_timeout')
        if ok:
            self.st['install']['return'] = 'healthy'

    def post(self):
        """Final failure-bound snapshot and bounded metadata BEFORE any cleanup."""
        obs = self.obs
        if not self.st['install']['line291_reached'] or self.entry is None:
            return
        self.st['elapsed_ms'] = self.ms()
        if time.monotonic() >= self.deadline:
            self.err('post_collection_deadline')
        try:
            final, changed = obs.final(self.ms())
            self.st['final'] = final; self.st['final_identity_changed'] = changed
            if changed:
                self.err('final_identity_changed')
        except BaseException:
            self.err('final_snapshot_failed')
        try:
            self.st['upstream']['override_return_sha256'] = file_sha(RUNTIPI / 'user-config/tipi-compose.yml', 1048576)
        except OSError:
            pass
        up = self.st['upstream']
        if up['override_return_sha256'] != up['override_entry_sha256']:
            # install.sh:84 rewrote the override after entry: the deadline basis must
            # still be the actual one (contract: release_pins keeps health definitions).
            again, refusal = self.render_basis(self.env_keys)
            same = refusal is None and again is not None and again['services'] == obs.basis['services']
            self.st['rerender_matches_entry'] = same
            if not same:
                for s in C.SERVICES:
                    if again is None or again['services'].get(s) != obs.basis['services'].get(s):
                        obs.basis_mismatch[s] = True
        inc = obs.incident
        cause = C.cause_class(inc, self.st.get('probe'))
        if inc and C.needs_tail(cause):
            self.st['log_tail'] = self.tail(inc['service'], inc['snapshot'])
        if obs.bound['runtipi-db'] is not None and (inc or self.st['install']['return'] == 'healthy'):
            self.st['migrations'] = self.migrations(obs.bound['runtipi-db']['id'])
        images = {k: None for k in C.SERVICES + ('upstream-manager',)}
        for s in C.SERVICES:
            if obs.bound[s] is not None:
                images[s] = self.image(obs.bound[s]['image_id'])
        if obs.pre_manager_id is not None:
            images['upstream-manager'] = self.image(obs.pre['runtipi']['image_id'])
        self.st['images'] = images
        try:
            mismatch = {s: v for s, v in obs.basis_mismatch.items() if v}
            self.st['crosscheck'] = obs.compare_healthchecks()
            for s in mismatch:
                obs.basis_mismatch[s] = True        # a re-render mismatch is never erased
        except BaseException:
            self.err('healthcheck_compare_failed')

    def image(self, image_id):
        try:
            status, doc = API('/images/' + image_id + '/json', C.MAX_IMAGE_BYTES)
            assert status == 200
            return C.image_row(doc, image_id)
        except BaseException:
            self.err('image_inspect_failed'); return None

    def tail(self, service, snap):
        """Exactly ONE bounded logs read of the identified owned failing container."""
        cid = snap['id']
        def same():
            status, doc = API('/containers/' + cid + '/json', C.MAX_INSPECT_BYTES)
            return status == 200 and doc.get('Id') == cid and doc.get('Image') == snap['image_id'] \
                and doc.get('Created') == snap['created']
        try:
            self.obs.check_identity()
            if not same():
                self.err('log_tail_identity_changed'); return None
            since = C.ts_ns(snap['created']) // 1000000000
            status, raw, over = API.raw('/containers/' + cid + '/logs?stdout=1&stderr=1&follow=0&tail=' +
                                        str(C.LOG_TAIL) + '&since=' + str(since), C.LOG_BYTES)
            ok = same(); self.obs.check_identity()
            if status != 200 or not ok:
                self.err('log_tail_identity_changed' if not ok else 'log_tail_failed'); return None
            out = C.parse_log(raw, truncated=over)
            del raw
            out.update(service=service, identity_rechecked=True)
            return out
        except BaseException:
            self.err('log_tail_failed'); return None

    def migrations(self, cid):
        allow = self.comp['migration_allowlist']
        base = C.migration_membership('not_attempted', None, None, allow)
        try:
            self.obs.check_identity()
            rc, out = run('migration-table-exists', ['docker', 'exec', cid, 'sh', '-c', PSQL, MIGRATION_EXISTS],
                          timeout=15, required=False, cap=64)
            if rc != 0 or out.strip() not in (b't', b'f'):
                base = C.migration_membership('unreadable', rc if rc is not None and -1 <= rc <= 255 else -1, None, allow)
            elif out.strip() == b'f':
                base = C.migration_membership('table_absent', 0, None, allow)
            else:
                rc, out = run('migration-rows', ['docker', 'exec', cid, 'sh', '-c', PSQL, MIGRATION_ROWS],
                              timeout=15, required=False, cap=65536)
                base = C.migration_membership('read' if rc == 0 else 'unreadable',
                                              rc if rc is not None and -1 <= rc <= 255 else -1, out, allow)
            self.obs.check_identity()
        except BaseException:
            self.err('migration_read_failed')
            base = C.migration_membership('unreadable', None, None, allow)
        base['allowlist_sha256'] = self.comp['migration_allowlist_sha256']
        return base

    def export(self):
        doc = C.assemble(self.obs if self.st['install']['line291_reached'] else None, self.st)
        raw = json.dumps(doc, sort_keys=True, indent=2) + '\n'
        (ROOT / 'health-export.json').write_text(raw); (ROOT / 'health-export.json').chmod(0o600)
        report['health_export_sha256'] = sha(raw.encode()); report['result'] = doc['result']; save()
        return doc


def entry_env(argv, backend):
    """Exact line-291 argv: known runuser identity, clean env, protected install.sh."""
    if argv[:6] != ['runuser', '-u', 'wizard-system', '--', 'env', '-i'] or argv[-2:] != ['bash', str(CHECKOUT / 'install.sh')]:
        return None
    pairs = argv[6:-2]
    if not all('=' in v for v in pairs):
        return None
    env = dict(v.split('=', 1) for v in pairs)
    if len(env) != len(pairs) or set(env) != SYSENV_KEYS:
        return None
    if env.get('WAM_IMAGE') != backend or env.get('WIZARD_PRIVATE_ACCESS') != 'none' or \
            env.get('WIZARD_SEPARATED') != '1' or env.get('RUNTIPI_ROOT') != str(RUNTIPI) or 'WIZARD_RELEASE_PINS' in env:
        return None
    return env


def exc_type(e):
    name = type(e).__name__
    return name if name in C.EXC_TYPES else 'Other'


def pre291_name(argv):
    for a in argv:
        for name in C.PRE291_COMMANDS[:-1]:
            if a == name or a.endswith('/' + name):
                return name
    return 'other'


# ------------------------------------------------------------ install mode ---
def install(revision, backend, service):
    assert re.fullmatch('[0-9a-f]{40}', revision)
    assert all(re.fullmatch('sha256:[0-9a-f]{64}', v) for v in (backend, service))
    comparator = json.loads((ROOT / 'comparator.json').read_text())
    assert C.sha(json.dumps(comparator['migration_allowlist'], sort_keys=True, separators=(',', ':')).encode()) \
        == comparator['migration_allowlist_sha256']
    helper = TREE / 'scripts/prepare-alongside.py'
    cap = Capture(comparator, backend)

    def fire_watchdog():
        cap.pre291_fired = True
        cap.st['pre291_budget_exhausted'] = True
        quiesce(cap.own_children())
    cap.watchdog = threading.Timer(PRE291_SECONDS, fire_watchdog); cap.watchdog.daemon = True

    def profile(frame, event, arg):
        if event not in ('call', 'return') or frame.f_code.co_filename != str(helper) or frame.f_code.co_name != 'run':
            return
        argv = [str(a) for a in frame.f_locals.get('argv', [])]
        is_install = argv[-1:] == [str(CHECKOUT / 'install.sh')]
        if event == 'call' and not is_install:
            cap.st['install']['pre291_command'] = pre291_name(argv)
        if any(a.endswith('/scripts/prepare-named-user.sh') for a in argv):
            if event == 'call':
                no_manager_writer(); fetched_source_closure(revision)
                cap.setup_thread = threading.Thread(target=cap.watch_setup, daemon=True); cap.setup_thread.start()
            else:
                cap.setup_stop.set(); cap.setup_thread.join(timeout=10)
                if not cap.setup_seen.is_set():
                    cap.err('setup_script_unobserved')
                if arg is not None:
                    cap.after_named_user()
        if is_install:
            if event == 'call':
                cap.enter(argv)
            else:
                cap.returned(arg is not None)
                if arg is not None:
                    raise HealthyStop()

    os.environ.update(WIZARD_PRIVATE_ACCESS='none', WAM_IMAGE=backend, WIZARD_AGENT_SERVICE_IMAGE=service,
                      WIZARD_RELEASE='none', WIZARD_GITHUB='0', WIZARD_AGENT_SAFE='0', PYTHONDONTWRITEBYTECODE='1')
    os.environ.pop('WIZARD_RELEASE_PINS', None)
    old = sys.argv[:]; sys.argv = [str(helper), 'hermes', '/home/hermes/.hermes', revision]
    original = None
    cap.watchdog.start()
    sys.setprofile(profile)
    try:
        runpy.run_path(str(helper), run_name='__main__')
        original = None
    except HealthyStop:
        cap.st['install']['exception_type'] = 'HealthyStop'
    except Refused as e:
        cap.st['refusal'] = e.reason if e.reason in C.REASONS else 'collector_internal_error'
        cap.st['install']['exception_type'] = 'Refused'
    except BaseException as e:
        original = e
        cap.st['install']['exception_type'] = exc_type(e)
        rc = getattr(e, 'returncode', None)
        cap.st['install']['returncode'] = rc if type(rc) is int and -64 <= rc <= 255 else None
        if cap.st['install']['line291_reached'] and not cap.done.is_set():
            cap.returned(False)
    finally:
        sys.setprofile(None); sys.argv = old; cap.watchdog.cancel(); cap.setup_stop.set()
    if original is None and cap.st['install']['exception_type'] is None:
        # The helper returned without reaching line 291's return path: not admissible.
        cap.st['install']['exception_type'] = 'Other'
    report['original_install'] = dict(cap.st['install']); save()
    # Collector errors are appended separately; they never replace the original.
    try:
        cap.post()
    except BaseException:
        cap.err('collector_internal_error')
    doc = cap.export()
    report['checks'].append({'name': 'health-capture-exported', 'passed': True}); save()
    if original is not None:
        report['first_error'] = {'type': exc_type(original), 'phase': 'install'}; save()
    return doc


# ------------------------------------------------------------ cleanup mode ---
def orphan_installers():
    hits = []
    for pid, (_, args) in proc_table().items():
        if pid == os.getpid():
            continue
        names = [os.fsdecode(a) for a in args]
        if any(a.endswith(('/prepare-alongside.py', '/prepare-named-user.sh', '/install.sh',
                           '/install-approval-custody.py')) for a in names):
            hits.append(pid)
    return hits


def cleanup():
    """Exact-ID cleanup with an explicitly created unowned synthetic negative control."""
    left = orphan_installers()
    if left:
        report['orphans_quiesced'] = quiesce(left)
        assert report['orphans_quiesced'] and not orphan_installers(), 'installer still active'
    for u in ('wizard-app-manager-api.service', 'wizard-runtipi.service'):
        rc, out = run('cleanup-show-' + u, ['systemctl', 'show', u, '-p', 'LoadState', '--value'], required=False)
        if out.strip() == b'loaded':
            run('cleanup-stop-' + u, ['systemctl', 'stop', u])
    m = module('health_exact_cleanup', TREE / 'qa/privsep-storage/production_guest.py'); m.ROOT = ROOT
    service = json.loads((ROOT / 'service-attestation-before.json').read_text())['engine_image_id']
    status, info = m.daemon_request('GET', '/info'); assert status == 200
    engine = info['ID']
    rc, out = run('create-unowned-control', ['docker', 'create', '--label', 'com.docker.compose.project=notes_wizardapps',
                  '--entrypoint=/bin/sh', service, '-c', 'exit 0'])
    raw = out.decode().strip(); assert re.fullmatch('[0-9a-f]{64}', raw)
    (ROOT / 'negative-control-id').write_text(raw + '\n')
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
    assert first['first_error_type'] == 'AssertionError' and first['absent_exact_ids'] == first['attempted_exact_ids']
    status, after = m.daemon_request('GET', '/containers/' + raw + '/json'); assert status == 200 and after == before
    status, info = m.daemon_request('GET', '/info'); assert status == 200 and info['ID'] == engine
    # Exact QA-owned negative only, by full ID, then structural absence readback.
    status, _ = m.daemon_request('DELETE', '/containers/' + raw + '?force=true'); assert status == 204
    status, gone = m.daemon_request('GET', '/containers/' + raw + '/json')
    assert status == 404 and isinstance(gone, dict) and isinstance(gone.get('message'), str)
    m.cleanup_containers()
    final = json.loads((ROOT / 'container-cleanup.json').read_text())
    status, info = m.daemon_request('GET', '/info'); assert status == 200 and info['ID'] == engine
    assert final['passed'] and final['readback_absent']
    report['exact_cleanup'] = {'negative_id': raw, 'negative_survived_owned_cleanup': True,
                               'negative_removed_by_full_id_absent': True, 'engine_id_stable': True,
                               'owned_attempted': len(first['attempted_exact_ids']),
                               'first_report_sha256': sha((ROOT / 'cleanup-negative.json').read_bytes()),
                               'final_report_sha256': sha((ROOT / 'container-cleanup.json').read_bytes())}
    save()


def main():
    global report
    mode = sys.argv[1]
    assert mode in ('fresh', 'install', 'cleanup')
    if (ROOT / 'native.json').exists():
        report = json.loads((ROOT / 'native.json').read_text())
    lock = os.open(ROOT / 'native.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    report['phases'].append(mode)
    try:
        if mode == 'fresh':
            assert not report['checks']
            fresh(sys.argv[2])
        elif mode == 'install':
            # Exactly once; never a second attempt in the same guest.
            assert report['checks'] == [{'name': 'fresh-synthetic-inputs', 'passed': True}]
            assert 'install' not in report['phases'][:-1], 'second installer attempt refused'
            assert report['exact_root_helper_argv'][-1] == sys.argv[2]
            with (ROOT / 'installer-private.log').open('wb') as out:
                oldout = os.dup(1); olderr = os.dup(2); os.dup2(out.fileno(), 1); os.dup2(out.fileno(), 2)
                try:
                    install(*sys.argv[2:5])
                finally:
                    os.dup2(oldout, 1); os.dup2(olderr, 2); os.close(oldout); os.close(olderr)
        else:
            cleanup()
    except BaseException as e:
        # Cleanup errors are always appended separately; they never displace or
        # become the original install outcome.
        if mode == 'cleanup':
            report['cleanup_errors'].append(type(e).__name__)
        elif report['first_error'] is None:
            report['first_error'] = {'type': type(e).__name__, 'phase': mode}
        raise
    finally:
        save(); os.close(lock)


if __name__ == '__main__':
    main()
