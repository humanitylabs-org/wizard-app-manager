#!/usr/bin/python3
"""QA-only finite installer-health collector (pure logic, no I/O of its own).

Changed reproduction labelled ``current-upstream-sealed-at-run``: the guest runs
the unchanged product path; this module only projects Docker API metadata into a
closed schema. Docker State.Error, Health.Log.Output, environment, labels other
than the two Compose identity labels, argv and log text never enter an export.
Every value leaving the guest is validated by ``validate_export``.

Python 3.11 compatible (Debian 12 guest) and imported with -I -B only.
"""
import datetime
import hashlib
import json
import re

SCHEMA = 'privsep-health-capture-v1'
LABEL = 'current-upstream-sealed-at-run'
PROJECT = 'runtipi'
SERVICES = ('runtipi-db', 'runtipi-queue', 'runtipi', 'runtipi-reverse-proxy')
GATED = ('runtipi-db', 'runtipi')          # healthcheck timings are mandatory
GATE_OF = {'runtipi-db': 'GATE88', 'runtipi-queue': 'GATE88',
           'runtipi': 'GATE116', 'runtipi-reverse-proxy': 'LINE118'}
GATES = ('GATE88', 'GATE116', 'LINE118')
RESULTS = tuple('REPRODUCED_' + GATE_OF[s] + '_' + s.upper().replace('-', '_') for s in SERVICES) + (
    'NOT_REPRODUCED', 'INSUFFICIENT', 'DEADLINE')
REASONS = ('first_incident_frozen', 'line291_returned_healthy', 'deadline_reached',
           'install_failed_without_health_incident', 'install_line291_not_reached',
           'budget_exhausted_before_gate88', 'basis_render_failed', 'basis_unparseable',
           'basis_over_cap', 'scope_release_index_present', 'scope_services_missing',
           'entry_identity_invalid', 'deadline_basis_mismatch',
           'identity_engine_changed', 'identity_boot_changed', 'identity_socket_changed',
           'identity_duplicate_container', 'identity_foreign_label', 'identity_invalid_id',
           'identity_container_swap', 'identity_image_swap', 'identity_container_vanished',
           'identity_lifecycle_regression', 'identity_manager_image_not_pinned',
           'identity_api_unavailable', 'identity_response_bound', 'collector_internal_error')
CAUSES = ('H1_DB_HEALTH_FAILING', 'H1_DB_EXITED', 'H1_DB_OOM',
          'H2_MANAGER_EXITED', 'H2_MANAGER_CONNECT_FAILED', 'H2_MANAGER_CHECK_TIMEOUT',
          'H3_MANAGER_HTTP_503', 'H3_MANAGER_HTTP_ERROR',
          'QUEUE_EXITED', 'QUEUE_HEALTH_FAILING', 'PROXY_EXITED', 'PROXY_HEALTH_FAILING',
          'OOM_KILLED', 'UNCLASSIFIED')
STATUSES = ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead')
HEALTHS = ('none', 'starting', 'healthy', 'unhealthy')
INCIDENT_KINDS = ('unhealthy', 'exited', 'dead', 'oom_killed')
SIGNALS = {1: 'SIGHUP', 2: 'SIGINT', 3: 'SIGQUIT', 6: 'SIGABRT', 9: 'SIGKILL',
           11: 'SIGSEGV', 13: 'SIGPIPE', 15: 'SIGTERM'}
CURL_CLASSES = {7: 'connect', 28: 'timeout', 22: 'http_error', 52: 'empty_reply',
                56: 'recv_failure', -1: 'docker_check_timeout'}
COMPARATOR_TESTS = {  # public CLI template f7da61f9..., sealed at staging
    'runtipi-db': ['CMD-SHELL', 'pg_isready -d tipi -U tipi'],
    'runtipi': ['CMD', 'curl', '-f', 'http://localhost:3000/api/health']}
INSTALL_RETURNS = ('healthy', 'exception', 'refused', 'not_reached')
EXC_TYPES = ('CalledProcessError', 'HealthyStop', 'Refused', 'TimeoutError',
             'AssertionError', 'SystemExit', 'OSError', 'KeyError', 'ValueError',
             'RuntimeError', 'KeyboardInterrupt', 'Other')
PRE291_COMMANDS = ('prepare-named-user.sh', 'install-server-access.py',
                   'install-approval-custody.py', 'git', 'chown', 'runuser', 'other')
COLLECTOR_ERRORS = ('setup_script_unobserved', 'cli_absent', 'cli_version_unparsed',
                    'compose_absent', 'final_identity_changed', 'final_snapshot_failed',
                    'post_collection_deadline', 'migration_read_failed', 'probe_failed',
                    'log_tail_failed', 'log_tail_identity_changed', 'image_inspect_failed',
                    'sampler_join_timeout', 'quiesce_incomplete', 'sampler_error', 'collector_internal_error',
                    'pre_install_snapshot_failed', 'healthcheck_compare_failed')
MIGRATION_STATES = ('read', 'table_absent', 'unreadable', 'not_attempted')
H_VALUES = {
    'H1': ('supported', 'not_supported', 'undetermined'),
    'H2': ('supported', 'not_supported', 'undetermined'),
    'H3': ('supported', 'not_supported', 'undetermined'),
    'H4': ('precondition_absent', 'precondition_present',
           'precondition_present_with_db_error_codes', 'undetermined'),
    'H5': ('consistent_with_transient', 'persistent_within_window', 'undetermined')}

# Finite bounds.
MAX_LIST_BYTES = 65536
MAX_LIST_ROWS = 4
MAX_INSPECT_BYTES = 1048576
MAX_IMAGE_BYTES = 262144
MAX_HEALTH_RECORDS = 5
MAX_TIMELINE = 32
MAX_REPO_REFS = 8
CAP_SECONDS = 900
ALLOWANCE_SAMPLING = 30
ALLOWANCE_POST = 30
INCIDENT_GRACE = 30
LOG_TAIL = 64
LOG_BYTES = 65536
LOG_LINE = 8192
MAX_MIGRATION_ROWS = 256

HEX64 = re.compile(r'[0-9a-f]{64}')
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
TS = re.compile(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z')
ZERO_TS = '0001-01-01T00:00:00Z'
REPO_DIGEST = re.compile(r'[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}')
REPO_TAG = re.compile(r'[a-z0-9][a-z0-9._/-]{0,200}:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')
ENGINE_ID = re.compile(r'[0-9A-Za-z:-]{8,128}')
BOOT_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
VERSION = re.compile(r'\bv?(\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[0-9A-Za-z.]{1,32})?)\b')
DURATION = re.compile(r'(?:(\d{1,4})h)?(?:(\d{1,4})m(?!s))?(?:(\d{1,6}(?:\.\d{1,6})?)s)?(?:(\d{1,7})ms)?')


class Violation(Exception):
    """Identity/bounds refusal. ``reason`` is always a member of REASONS."""
    def __init__(self, reason):
        assert reason in REASONS
        super().__init__(reason)
        self.reason = reason


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_sha(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def ts_ns(value):
    """RFC3339Nano (UTC, Docker form) -> integer ns since epoch; strict."""
    m = TS.fullmatch(value) if isinstance(value, str) and len(value) <= 40 else None
    if not m:
        raise Violation('identity_lifecycle_regression')
    if value == ZERO_TS:
        return 0
    y, mo, d, h, mi, s = (int(v) for v in m.groups()[:6])
    frac = (m.group(7) or '').ljust(9, '0')
    epoch = datetime.datetime(y, mo, d, h, mi, s, tzinfo=datetime.timezone.utc).timestamp()
    return int(epoch) * 1000000000 + int(frac or 0)


def parse_duration(value):
    """Go/Compose duration -> seconds (float) or None. No env references."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value / 1e9 if value >= 0 else None      # inspect form: ns
    if not isinstance(value, str) or not value or len(value) > 24:
        return None
    m = DURATION.fullmatch(value)
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0) + int(ms or 0) / 1000


def schedule(hc):
    """Return a finite timing schedule, None for no healthcheck, or 'bad'."""
    if hc is None:
        return None
    if not isinstance(hc, dict):
        return 'bad'
    test = hc.get('test')
    if hc.get('disable') is True or test in (['NONE'], 'NONE'):
        return None
    if not isinstance(test, list) or not test or not all(isinstance(t, str) for t in test):
        return 'bad'
    vals = {}
    for key in ('interval', 'timeout', 'start_period'):
        v = parse_duration(hc.get(key))
        if v is None or v > 3600:
            return 'bad'
        vals[key] = v
    retries = hc.get('retries')
    if type(retries) is not int or not 0 < retries <= 1000:
        return 'bad'
    budget = vals['start_period'] + retries * (vals['interval'] + vals['timeout'])
    return {'interval_s': vals['interval'], 'timeout_s': vals['timeout'],
            'start_period_s': vals['start_period'], 'retries': retries,
            'budget_s': budget, 'test_sha256': canonical_sha(test)}


def deadline_basis(config, argv_env_keys):
    """Recompute the absolute budget from the ACTUAL rendered merged config.

    Returns (basis, refusal_reason). Healthcheck contents stay in the guest:
    only timings, budgets and the canonical test hash are kept.
    """
    if not isinstance(config, dict) or not isinstance(config.get('services'), dict):
        return None, 'basis_render_failed'
    services = config['services']
    if 'wizard-release-index' in services or 'WIZARD_RELEASE_PINS' in argv_env_keys:
        return None, 'scope_release_index_present'
    if not all(s in services and isinstance(services[s], dict) for s in SERVICES):
        return None, 'scope_services_missing'
    per = {}
    for s in SERVICES:
        sch = schedule(services[s].get('healthcheck'))
        if sch == 'bad' or (s in GATED and sch is None):
            return None, 'basis_unparseable'
        per[s] = sch
    gate88 = max(per['runtipi-db']['budget_s'], (per['runtipi-queue'] or {}).get('budget_s', 0))
    gate116 = per['runtipi']['budget_s']
    total = gate88 + gate116 + ALLOWANCE_SAMPLING + ALLOWANCE_POST
    basis = {'source': 'rendered-compose-config-no-interpolate', 'services': per,
             'gate88_s': gate88, 'gate116_s': gate116, 'allowance_sampling_s': ALLOWANCE_SAMPLING,
             'allowance_post_s': ALLOWANCE_POST, 'total_s': total, 'cap_s': CAP_SECONDS}
    if total > CAP_SECONDS:
        return basis, 'basis_over_cap'
    return basis, None


def inspect_schedule(hc):
    """Container Config.Healthcheck (ns integers) -> comparable schedule."""
    if hc is None:
        return None
    if not isinstance(hc, dict) or not isinstance(hc.get('Test'), list):
        return 'bad'
    if hc['Test'] in (['NONE'],):
        return None
    out = {'test': hc['Test']}
    # Docker engine semantics: 0/absent Interval/Timeout = 30s, StartPeriod = 0, Retries = 3.
    for k, v, default in (('interval', 'Interval', 30000), ('timeout', 'Timeout', 30000), ('start_period', 'StartPeriod', 0)):
        raw = hc.get(v, 0)
        if type(raw) is not int or raw < 0 or raw % 1000000:
            return 'bad'
        out[k] = str(raw // 1000000 or default) + 'ms'
    retries = hc.get('Retries', 0)
    out['retries'] = 3 if retries == 0 and type(retries) is int else retries
    return schedule(out)


def label_filter(service):
    return json.dumps({'label': ['com.docker.compose.project=' + PROJECT,
                                 'com.docker.compose.service=' + service]}, separators=(',', ':'))


def list_rows(rows, service):
    """Validate ONE project+service filtered list. Never trust the filter."""
    if not isinstance(rows, list):
        raise Violation('identity_api_unavailable')
    if len(rows) > MAX_LIST_ROWS:
        raise Violation('identity_duplicate_container')
    out = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get('Id'), str) or not HEX64.fullmatch(r['Id']):
            raise Violation('identity_invalid_id')
        if not isinstance(r.get('ImageID'), str) or not IMAGE_ID.fullmatch(r['ImageID']):
            raise Violation('identity_invalid_id')
        labels = r.get('Labels')
        if not isinstance(labels, dict) or labels.get('com.docker.compose.project') != PROJECT or \
                labels.get('com.docker.compose.service') != service:
            raise Violation('identity_foreign_label')
        if labels.get('com.docker.compose.oneoff', 'False') != 'False' or \
                labels.get('com.docker.compose.container-number', '1') != '1':
            raise Violation('identity_duplicate_container')
        out.append({'id': r['Id'], 'image_id': r['ImageID']})
    if len({r['id'] for r in out}) != len(out):
        raise Violation('identity_duplicate_container')
    return out


def _int(v, lo, hi):
    if type(v) is not int or not lo <= v <= hi:
        raise Violation('identity_response_bound')
    return v


def _bool(v):
    if type(v) is not bool:
        raise Violation('identity_response_bound')
    return v


def project_inspect(doc, expected_id, expected_image, t_ms):
    """Closed projection of a container inspect document."""
    if not isinstance(doc, dict) or doc.get('Id') != expected_id:
        raise Violation('identity_container_swap')
    if doc.get('Image') != expected_image:
        raise Violation('identity_image_swap')
    state = doc.get('State')
    if not isinstance(state, dict) or state.get('Status') not in STATUSES:
        raise Violation('identity_response_bound')
    created, started, finished = doc.get('Created'), state.get('StartedAt'), state.get('FinishedAt')
    for v in (created, started, finished):
        ts_ns(v)
    health = state.get('Health')
    if health is None:
        hstatus, streak, records = 'none', 0, []
    else:
        if not isinstance(health, dict) or health.get('Status') not in HEALTHS[1:]:
            raise Violation('identity_response_bound')
        hstatus = health['Status']
        streak = _int(health.get('FailingStreak', 0), 0, 1000000)
        log = health.get('Log') or []
        if not isinstance(log, list) or len(log) > 64:
            raise Violation('identity_response_bound')
        records = []
        for row in log[-MAX_HEALTH_RECORDS:]:
            if not isinstance(row, dict):
                raise Violation('identity_response_bound')
            ts_ns(row.get('Start')); ts_ns(row.get('End'))
            records.append({'start': row['Start'], 'end': row['End'],
                            'exit_code': _int(row.get('ExitCode'), -1, 255)})
    exit_code = _int(state.get('ExitCode', 0), -1, 255)
    sig = SIGNALS.get(exit_code - 128) if exit_code > 128 else None
    return {'id': expected_id, 'image_id': expected_image, 'created': created,
            'started_at': started, 'finished_at': finished,
            'restart_count': _int(doc.get('RestartCount', 0), 0, 1000000),
            'status': state['Status'], 'running': _bool(state.get('Running')),
            'dead': _bool(state.get('Dead')), 'paused': _bool(state.get('Paused')),
            'restarting': _bool(state.get('Restarting')), 'oom_killed': _bool(state.get('OOMKilled')),
            'exit_code': exit_code, 'signal_inferred': sig, 'health': hstatus,
            'failing_streak': streak, 'health_log': records, 't_ms': t_ms}


def incident_kind(snap):
    if snap['oom_killed']:
        return 'oom_killed'
    if snap['status'] == 'dead' or snap['dead']:
        return 'dead'
    if snap['status'] == 'exited':
        return 'exited'
    if snap['health'] == 'unhealthy':
        return 'unhealthy'
    return None


def regression(prev, cur):
    if prev is None:
        return False
    return (cur['created'] != prev['created'] or ts_ns(cur['started_at']) < ts_ns(prev['started_at'])
            or cur['restart_count'] < prev['restart_count'] or cur['t_ms'] < prev['t_ms'])


class Observer:
    """Bounded, identity-fenced sampler state. ``api(path, cap)`` -> (status, obj)."""

    def __init__(self, api, identity, expected_manager_image):
        self.api = api; self.identity_fn = identity
        self.expected_manager_image = expected_manager_image
        self.baseline = None; self.pre = {s: None for s in SERVICES}; self.pre_manager_id = None
        self.bound = {s: None for s in SERVICES}; self.last = {s: None for s in SERVICES}
        self.timeline = {s: [] for s in SERVICES}; self.timeline_dropped = {s: 0 for s in SERVICES}
        self.incident = None; self.violation = None; self.ticks = 0; self.samples = 0
        self.gate_ms = {g: None for g in GATES}; self.max_ticks = None
        self.basis_mismatch = {s: None for s in SERVICES}; self.hc = {s: None for s in SERVICES}
        self.basis = None; self.unarmed_states = {s: 0 for s in SERVICES}

    # identity -------------------------------------------------------------
    def check_identity(self):
        cur = self.identity_fn()
        if not (isinstance(cur, dict) and isinstance(cur.get('engine'), str) and ENGINE_ID.fullmatch(cur['engine'])
                and isinstance(cur.get('boot'), str) and BOOT_ID.fullmatch(cur['boot'])
                and isinstance(cur.get('socket'), list) and len(cur['socket']) == 2
                and all(type(v) is int and v >= 0 for v in cur['socket'])):
            raise Violation('entry_identity_invalid' if self.baseline is None else 'identity_api_unavailable')
        if self.baseline is None:
            self.baseline = cur; return cur
        for key, reason in (('engine', 'identity_engine_changed'), ('boot', 'identity_boot_changed'),
                            ('socket', 'identity_socket_changed')):
            if cur[key] != self.baseline[key]:
                raise Violation(reason)
        return cur

    def list(self, service):
        status, rows = self.api('/containers/json?all=1&filters=' + _quote(label_filter(service)), MAX_LIST_BYTES)
        if status != 200:
            raise Violation('identity_api_unavailable')
        return list_rows(rows, service)

    def inspect(self, cid, image, t_ms):
        status, doc = self.api('/containers/' + cid + '/json', MAX_INSPECT_BYTES)
        if status == 404:
            raise Violation('identity_container_vanished')
        if status != 200:
            raise Violation('identity_api_unavailable')
        snap = project_inspect(doc, cid, image, t_ms)
        return snap, (doc.get('Config') or {}).get('Healthcheck') if isinstance(doc.get('Config'), dict) else None

    # entry ----------------------------------------------------------------
    def entry(self, t_ms):
        """Pre-install snapshot at the exact line-291 call; binds base containers."""
        self.check_identity()
        for s in SERVICES:
            rows = self.list(s)
            if len(rows) > 1:
                raise Violation('identity_duplicate_container')
            if rows:
                snap, hc = self.inspect(rows[0]['id'], rows[0]['image_id'], t_ms)
                self.pre[s] = snap
                if s == 'runtipi' and rows[0]['image_id'] != self.expected_manager_image:
                    self.pre_manager_id = rows[0]['id']     # upstream manager, never bound
                else:
                    self.bound[s] = rows[0]; self.last[s] = snap; self._timeline(s, snap)
                    self.hc[s] = hc
        self.check_identity()

    # sampling -------------------------------------------------------------
    def _timeline(self, s, snap):
        key = (snap['status'], snap['health'], snap['restart_count'], snap['started_at'], snap['failing_streak'])
        rows = self.timeline[s]
        if rows and (rows[-1]['status'], rows[-1]['health'], rows[-1]['restart_count'],
                     rows[-1]['started_at'], rows[-1]['failing_streak']) == key:
            return
        if len(rows) >= MAX_TIMELINE:
            self.timeline_dropped[s] += 1; return
        rows.append({'t_ms': snap['t_ms'], 'status': snap['status'], 'health': snap['health'],
                     'restart_count': snap['restart_count'], 'started_at': snap['started_at'],
                     'failing_streak': snap['failing_streak']})

    def bind_rows(self, s, rows):
        if s == 'runtipi' and self.pre_manager_id is not None:
            rows = [r for r in rows if r['id'] != self.pre_manager_id]
        if len(rows) > 1:
            raise Violation('identity_duplicate_container')
        bound = self.bound[s]
        if bound is None:
            if not rows:
                return None
            if s == 'runtipi' and rows[0]['image_id'] != self.expected_manager_image:
                raise Violation('identity_manager_image_not_pinned')
            self.bound[s] = rows[0]
            return rows[0]
        if not rows:
            raise Violation('identity_container_vanished')
        if rows[0]['id'] != bound['id']:
            raise Violation('identity_container_swap')
        if rows[0]['image_id'] != bound['image_id']:
            raise Violation('identity_image_swap')
        return bound

    def armed(self, service, exited):
        """A pre-existing upstream container counts only once its gate (or a later one) ran."""
        later = GATES[GATES.index(GATE_OF[service]):]
        return exited or any(self.gate_ms[g] is not None for g in later)

    def tick(self, t_ms, active_gates, exited=False):
        """One bounded sample. Returns 'continue' or 'stop' (incident/violation)."""
        if self.incident or self.violation:
            return 'stop'
        if self.max_ticks is not None and self.ticks >= self.max_ticks:
            return 'stop'
        self.ticks += 1
        for g in active_gates:
            if g in self.gate_ms and self.gate_ms[g] is None:
                self.gate_ms[g] = t_ms
        try:
            self.check_identity()
            for s in SERVICES:
                row = self.bind_rows(s, self.list(s))
                if row is None:
                    continue
                snap, hc = self.inspect(row['id'], row['image_id'], t_ms)
                if regression(self.last[s], snap):
                    raise Violation('identity_lifecycle_regression')
                if self.hc[s] is None:
                    self.hc[s] = hc
                self.last[s] = snap; self.samples += 1; self._timeline(s, snap)
                kind = incident_kind(snap)
                if kind and not self.armed(s, exited):
                    self.unarmed_states[s] += 1
                elif kind and self.incident is None:
                    active = [g for g in GATES if g in active_gates]
                    self.incident = {'service': s, 'gate': GATE_OF[s], 'kind': kind,
                                     'active_gate_observed': active[-1] if active else None,
                                     'after_command_exit': bool(exited), 't_ms': t_ms, 'snapshot': snap}
                    return 'stop'
            self.check_identity()
        except Violation as v:
            self.violation = v.reason
            return 'stop'
        return 'continue'

    def final(self, t_ms):
        """Failure-bound snapshot BEFORE cleanup; never changes a frozen incident."""
        out = {s: None for s in SERVICES}; changed = False
        self.check_identity()
        for s in SERVICES:
            rows = self.list(s)
            if s == 'runtipi' and self.pre_manager_id is not None:
                rows = [r for r in rows if r['id'] != self.pre_manager_id]
            bound = self.bound[s]
            if bound is None:
                changed = changed or bool(rows); continue
            if len(rows) != 1 or rows[0]['id'] != bound['id'] or rows[0]['image_id'] != bound['image_id']:
                changed = True; continue
            out[s], _ = self.inspect(bound['id'], bound['image_id'], t_ms)
        self.check_identity()
        return out, changed

    def compare_healthchecks(self):
        """In-guest comparison; only hashes/booleans are exported."""
        rows = {}
        for s in SERVICES:
            hc = self.hc[s]; rendered = (self.basis or {}).get('services', {}).get(s)
            if hc is None and self.bound[s] is None:
                rows[s] = None; continue
            sch = inspect_schedule(hc)
            if sch == 'bad':
                rows[s] = {'healthcheck_sha256': canonical_sha(hc), 'timing_matches_rendered': False,
                           'test_matches_rendered': False, 'test_matches_comparator': False}
                self.basis_mismatch[s] = True; continue
            same_timing = (sch is None and rendered is None) or (
                sch is not None and rendered is not None and all(
                    abs(sch[k] - rendered[k]) < 1e-6 for k in ('interval_s', 'timeout_s', 'start_period_s'))
                and sch['retries'] == rendered['retries'])
            # An image-inherited healthcheck beyond the rendered budget is a mismatch.
            if sch is not None and rendered is None:
                same_timing = False
            comp = COMPARATOR_TESTS.get(s)
            rows[s] = {'healthcheck_sha256': canonical_sha(hc) if hc is not None else None,
                       'timing_matches_rendered': bool(same_timing),
                       'test_matches_rendered': (sch is None and rendered is None) or (
                           sch is not None and rendered is not None and sch['test_sha256'] == rendered['test_sha256']),
                       'test_matches_comparator': None if comp is None else (
                           sch is not None and sch['test_sha256'] == canonical_sha(comp))}
            self.basis_mismatch[s] = not same_timing
        return rows


def _quote(text):
    from urllib.parse import quote
    return quote(text, safe='')


# ----------------------------------------------------------------- causes ---
def last_health_exit(snap):
    return snap['health_log'][-1]['exit_code'] if snap and snap['health_log'] else None


def needs_probe(incident):
    return bool(incident and incident['service'] == 'runtipi' and incident['kind'] == 'unhealthy'
                and last_health_exit(incident['snapshot']) == 22)


def cause_class(incident, probe):
    if not incident:
        return None
    s, kind, snap = incident['service'], incident['kind'], incident['snapshot']
    if kind == 'oom_killed':
        return 'H1_DB_OOM' if s == 'runtipi-db' else 'OOM_KILLED'
    code = last_health_exit(snap)
    if s == 'runtipi-db':
        return 'H1_DB_HEALTH_FAILING' if kind == 'unhealthy' else 'H1_DB_EXITED'
    if s == 'runtipi':
        if kind in ('exited', 'dead'):
            return 'H2_MANAGER_EXITED'
        cls = CURL_CLASSES.get(code)
        if cls in ('connect', 'empty_reply', 'recv_failure'):
            return 'H2_MANAGER_CONNECT_FAILED'
        if cls in ('timeout', 'docker_check_timeout'):
            return 'H2_MANAGER_CHECK_TIMEOUT'
        if cls == 'http_error':
            return 'H3_MANAGER_HTTP_503' if probe and probe.get('http_status') == 503 else 'H3_MANAGER_HTTP_ERROR'
        return 'UNCLASSIFIED'
    if s == 'runtipi-queue':
        return 'QUEUE_HEALTH_FAILING' if kind == 'unhealthy' else 'QUEUE_EXITED'
    return 'PROXY_HEALTH_FAILING' if kind == 'unhealthy' else 'PROXY_EXITED'


SUFFICIENT_CAUSES = ('H1_DB_OOM', 'OOM_KILLED', 'H3_MANAGER_HTTP_503')


def needs_tail(cause):
    return cause is not None and cause not in SUFFICIENT_CAUSES


def probe_result(exit_code, stdout):
    """Fixed predeclared localhost probe: curl exit + HTTP status only."""
    if type(exit_code) is not int or not -1 <= exit_code <= 255:
        raise Violation('identity_response_bound')
    status = None
    if isinstance(stdout, bytes) and re.fullmatch(rb'[1-5][0-9]{2}', stdout):
        status = int(stdout)
    return {'curl_exit': exit_code, 'http_status': status}


# -------------------------------------------------------------- log parser ---
ERRNO = ('EACCES', 'EPERM', 'ENOENT', 'ECONNREFUSED', 'ETIMEDOUT', 'ECONNRESET', 'EADDRINUSE',
         'EAI_AGAIN', 'ENOTFOUND', 'EHOSTUNREACH', 'ENETUNREACH', 'ENOSPC', 'EROFS', 'EEXIST',
         'ENOTDIR', 'EISDIR', 'EMFILE', 'EPIPE')
SQLSTATE = {'08000', '08001', '08003', '08004', '08006', '0A000', '22P02', '23502', '23503', '23505',
            '25P02', '28000', '28P01', '3D000', '3F000', '40001', '40P01', '42501', '42601', '42701',
            '42703', '42704', '42710', '42P01', '42P06', '42P07', '53100', '53200', '53300',
            '55P03', '57014', '57P01', '57P03', '58P01', 'XX000'}
MARKERS = (  # source-backed fixed phrases (backend 595523fb, postgres, rabbitmq)
    ('.env file not found', 'BACKEND_ENV_FILE_MISSING'),
    ('Invalid environment variables', 'BACKEND_ENV_INVALID'),
    ('Error migrating database:', 'BACKEND_MIGRATION_ERROR'),
    ('Database migration complete.', 'BACKEND_MIGRATION_COMPLETE'),
    ('Failed to connect to RabbitMQ within timeout period', 'BACKEND_QUEUE_CONNECT_TIMEOUT'),
    ('Queue connection lost, exceeded maximum reconnection attempts', 'BACKEND_QUEUE_RECONNECT_EXHAUSTED'),
    ('Queue connection lost, attempting to reconnect', 'BACKEND_QUEUE_RECONNECTING'),
    ('Queue connection not ready', 'BACKEND_QUEUE_NOT_READY'),
    ('Queue connection error', 'BACKEND_QUEUE_CONNECTION_ERROR'),
    ('Connected to the queue', 'BACKEND_QUEUE_CONNECTED'),
    ('Failed to initialize EventPublisher channel', 'BACKEND_QUEUE_PUBLISHER_INIT_FAILED'),
    ('Could not repair app environments', 'BACKEND_APP_ENV_REPAIR_FAILED'),
    ('Nest application successfully started', 'BACKEND_STARTED'),
    ('database system is ready to accept connections', 'PG_READY'),
    ('password authentication failed', 'PG_AUTH_FAILED'),
    ('database files are incompatible with server', 'PG_DATA_INCOMPATIBLE'),
    ('could not create shared memory segment', 'PG_SHMEM_FAILED'),
    ('No space left on device', 'NO_SPACE'),
    ('Permission denied', 'PERMISSION_DENIED'),
    ('Server startup complete', 'RMQ_STARTED'),
)
SEVERITY = (('PANIC:', 'PG_PANIC'), ('FATAL:', 'PG_FATAL'), ('ERROR:', 'PG_ERROR'))
CODE_FIELD = re.compile(r"""(?:\bcode: '|"code":\s?")([0-9A-Z_]{2,16})['"]""")
ERRNO_INLINE = re.compile(r'\b(?:Error: |connect |getaddrinfo |read |write |open )(E[A-Z_]{2,15})\b')
SECRET = re.compile(r'(?i)(?:passw(?:or)?d\s*[=:]|secret\s*[=:]|token\s*[=:]|bearer\s|authorization:'
                    r'|[a-z]+://[^\s/:@]+:[^\s/@]+@|PGPASSWORD|JWT_SECRET|RABBITMQ_PASSWORD'
                    r'|\b[A-Za-z0-9+/_-]{40,}={0,2})')
SGR = re.compile(r'\x1b\[[0-9;]{0,32}m')
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
FORBIDDEN_KEYS = frozenset({'schema', 'label', 'result', 'result_reason', 'cause_class', 'incident',
                            'codes', 'membership', 'hypotheses'})
LOG_CODES = tuple(sorted({'ERRNO_' + e for e in ERRNO} | {'SQLSTATE_' + c for c in SQLSTATE} |
                         {m[1] for m in MARKERS} | {m[1] for m in SEVERITY}))


def demux(raw):
    """Docker non-TTY multiplexed log stream -> list of text lines (in memory)."""
    out = []; pos = 0; text = b''
    while pos < len(raw):
        if len(raw) - pos < 8:
            return None, True
        kind, size = raw[pos], int.from_bytes(raw[pos + 4:pos + 8], 'big')
        if kind not in (1, 2) or raw[pos + 1:pos + 4] != b'\0\0\0' or size > LOG_BYTES:
            return None, False
        chunk = raw[pos + 8:pos + 8 + size]
        if len(chunk) < size:
            text += chunk; return text, True
        text += chunk; pos += 8 + size
    return text, False


def parse_log(raw, *, truncated):
    """Strict finite projection: code enums and counts only."""
    rejected = {'control': 0, 'secret': 0, 'forged': 0, 'overlong': 0, 'undecodable': 0}
    codes = {}; unknown = 0; unmatched = 0
    text, partial = demux(raw[:LOG_BYTES])
    stream_valid = text is not None
    lines = [] if text is None else text.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    if partial and lines:
        lines.pop(); truncated = True     # never parse a cut line
    lines = lines[-LOG_TAIL:]
    def hit(code):
        codes[code] = codes.get(code, 0) + 1
    for raw_line in lines:
        if len(raw_line) > LOG_LINE:
            rejected['overlong'] += 1; continue
        try:
            line = raw_line.decode('utf-8')
        except UnicodeDecodeError:
            rejected['undecodable'] += 1; continue
        line = SGR.sub('', line.rstrip('\r'))
        if CONTROL.search(line) or '\x1b' in line:
            rejected['control'] += 1; continue
        if SECRET.search(line):
            rejected['secret'] += 1; continue
        stripped = line.strip()
        found = []
        if stripped.startswith('{') or stripped.startswith('['):
            try:
                obj = json.loads(stripped)
            except ValueError:
                obj = None
            if obj is not None:
                if not isinstance(obj, dict) or _depth(obj) > 2 or len(obj) > 32 or FORBIDDEN_KEYS & set(obj):
                    rejected['forged'] += 1; continue
                for key in ('code', 'err', 'error'):
                    v = obj.get(key)
                    if isinstance(v, dict):
                        v = v.get('code')
                    if isinstance(v, str):
                        found.append(v)
                stripped = ' '.join(v for v in obj.values() if isinstance(v, str))
        found += CODE_FIELD.findall(stripped) + ERRNO_INLINE.findall(stripped)
        matched = False
        for code in found:
            if code.startswith('E') and code in ERRNO:
                hit('ERRNO_' + code); matched = True
            elif code in SQLSTATE:
                hit('SQLSTATE_' + code); matched = True
            else:
                unknown += 1; matched = True
        for phrase, code in MARKERS:
            if phrase in stripped:
                hit(code); matched = True
        for phrase, code in SEVERITY:
            if phrase in stripped:
                hit(code); matched = True; break
        if not matched:
            unmatched += 1
    return {'bytes': min(len(raw), LOG_BYTES), 'lines': len(lines), 'truncated': bool(truncated),
            'stream_valid': stream_valid, 'codes': dict(sorted(codes.items())),
            'unknown_code_count': unknown, 'rejected': rejected, 'unmatched_lines': unmatched,
            'insufficient': (unknown > 0 or not stream_valid)}


def _depth(v):
    """Nesting depth: scalar 0, flat object/array 1."""
    if isinstance(v, dict):
        return 1 + max([_depth(x) for x in v.values()] or [0])
    if isinstance(v, list):
        return 1 + max([_depth(x) for x in v] or [0])
    return 0


# --------------------------------------------------------------- migrations ---
def migration_allowlist(pinned, upstream):
    """pinned/upstream: [{'tag','sha256','when'}] from sealed source journals."""
    names = sorted({r['tag'] for r in pinned} | {r['tag'] for r in upstream})
    return {'names': names, 'pinned': {r['tag']: (r['sha256'], r['when']) for r in pinned},
            'upstream_v4_11_0': {r['tag']: (r['sha256'], r['when']) for r in upstream}}


def migration_membership(state, exit_code, stdout, allow):
    """Fixed-query rows ``<hash> <created_at>`` -> membership + unknown count."""
    assert state in MIGRATION_STATES
    out = {'state': state, 'exit_code': exit_code, 'rows': 0, 'unknown_count': 0,
           'membership': {}, 'order_consistent': None}
    if state != 'read':
        return out
    if not isinstance(stdout, bytes) or len(stdout) > 65536:
        out['state'] = 'unreadable'; return out
    lines = [v for v in stdout.split(b'\n') if v]
    if len(lines) > MAX_MIGRATION_ROWS:
        out['state'] = 'unreadable'; return out
    rows = []
    for v in lines:
        m = re.fullmatch(rb'([0-9a-f]{64}) ([0-9]{1,16})', v)
        if not m:
            out['state'] = 'unreadable'; out['rows'] = 0; return out
        rows.append((m.group(1).decode(), int(m.group(2))))
    out['rows'] = len(rows)
    applied = set(rows)
    pinned = {k: (v[0], int(v[1])) for k, v in allow['pinned'].items()}
    upstream = {k: (v[0], int(v[1])) for k, v in allow['upstream_v4_11_0'].items()}
    for name in allow['names']:
        out['membership'][name] = {
            'pinned': pinned[name] in applied if name in pinned else None,
            'upstream_v4_11_0': upstream[name] in applied if name in upstream else None}
    known = set(pinned.values()) | set(upstream.values())
    out['unknown_count'] = sum(1 for r in rows if r not in known)
    out['order_consistent'] = [r[1] for r in rows] == sorted(r[1] for r in rows)
    return out


# --------------------------------------------------------- classification ---
def classify(state):
    """Finite result. NOT_REPRODUCED never means fixed."""
    inc = state.get('incident'); outcome = state['install']['return']
    if state.get('refusal'):
        return 'INSUFFICIENT', state['refusal']
    if inc:
        name = 'REPRODUCED_' + inc['gate'] + '_' + inc['service'].upper().replace('-', '_')
        return name, 'first_incident_frozen'
    if state.get('violation'):
        return 'INSUFFICIENT', state['violation']
    if state.get('pre291_budget_exhausted'):
        return 'INSUFFICIENT', 'budget_exhausted_before_gate88'
    if state.get('deadline_hit'):
        if any(state.get('basis_mismatch') or ()):
            return 'INSUFFICIENT', 'deadline_basis_mismatch'
        return 'DEADLINE', 'deadline_reached'
    if outcome == 'healthy':
        if any(state.get('basis_mismatch') or ()):
            return 'INSUFFICIENT', 'deadline_basis_mismatch'
        return 'NOT_REPRODUCED', 'line291_returned_healthy'
    if outcome == 'exception' and state.get('line291_reached'):
        return 'INSUFFICIENT', 'install_failed_without_health_incident'
    return 'INSUFFICIENT', 'install_line291_not_reached'


def hypotheses(result, incident, cause, final, migrations, tail, basis):
    db_final = (final or {}).get('runtipi-db')
    h = {k: 'undetermined' for k in H_VALUES}
    if incident:
        s = incident['service']
        h['H1'] = 'supported' if s == 'runtipi-db' else (
            'not_supported' if db_final and db_final['health'] == 'healthy' else 'undetermined')
        if s == 'runtipi':
            h['H2'] = 'supported' if cause and cause.startswith('H2_') else 'not_supported'
            h['H3'] = 'supported' if cause and cause.startswith('H3_') else (
                'not_supported' if cause == 'H2_MANAGER_EXITED' else 'undetermined')
        snap = incident['snapshot']; retries = None
        if basis and basis.get('services', {}).get(s):
            retries = basis['services'][s]['retries']
        if incident['kind'] == 'unhealthy' and retries and snap['failing_streak'] >= retries:
            h['H5'] = 'persistent_within_window'
    elif result == 'NOT_REPRODUCED':
        h['H5'] = 'consistent_with_transient'
    if migrations and migrations['state'] == 'read':
        ahead = migrations['unknown_count'] > 0 or any(
            v['upstream_v4_11_0'] and v['pinned'] is None for v in migrations['membership'].values())
        db_codes = bool(tail and any(c.startswith('SQLSTATE_') or c == 'BACKEND_MIGRATION_ERROR'
                                     for c in tail['codes']))
        h['H4'] = ('precondition_present_with_db_error_codes' if ahead and db_codes else
                   'precondition_present' if ahead else 'precondition_absent')
    return h


# ------------------------------------------------------------ export schema ---
class SchemaError(AssertionError):
    pass


def _req(cond, where):
    if not cond:
        raise SchemaError(where)


def _keys(d, keys, where):
    _req(isinstance(d, dict) and set(d) == set(keys), where + ':keys')


def _opt(v, check, where):
    if v is not None:
        check(v, where)


def _hex(v, where):
    _req(isinstance(v, str) and HEX64.fullmatch(v), where)


def _image(v, where):
    _req(isinstance(v, str) and IMAGE_ID.fullmatch(v), where)


def _ts(v, where):
    _req(isinstance(v, str) and TS.fullmatch(v) and len(v) <= 40, where)


def _n(v, lo, hi, where, real=False):
    ok = (type(v) is int) or (real and type(v) is float)
    _req(ok and lo <= v <= hi, where)


def _b(v, where):
    _req(type(v) is bool, where)


def _enum(v, values, where):
    _req(isinstance(v, str) and v in values, where)


SNAP_KEYS = ('id', 'image_id', 'created', 'started_at', 'finished_at', 'restart_count', 'status',
             'running', 'dead', 'paused', 'restarting', 'oom_killed', 'exit_code', 'signal_inferred',
             'health', 'failing_streak', 'health_log', 't_ms')


def _snap(v, where):
    _keys(v, SNAP_KEYS, where)
    _hex(v['id'], where + '.id'); _image(v['image_id'], where + '.image_id')
    for k in ('created', 'started_at', 'finished_at'):
        _ts(v[k], where + '.' + k)
    _n(v['restart_count'], 0, 1000000, where); _enum(v['status'], STATUSES, where)
    for k in ('running', 'dead', 'paused', 'restarting', 'oom_killed'):
        _b(v[k], where + '.' + k)
    _n(v['exit_code'], -1, 255, where); _opt(v['signal_inferred'], lambda x, w: _enum(x, tuple(SIGNALS.values()), w), where)
    _enum(v['health'], HEALTHS, where); _n(v['failing_streak'], 0, 1000000, where)
    _req(isinstance(v['health_log'], list) and len(v['health_log']) <= MAX_HEALTH_RECORDS, where + '.health_log')
    for r in v['health_log']:
        _keys(r, ('start', 'end', 'exit_code'), where + '.hl'); _ts(r['start'], where); _ts(r['end'], where)
        _n(r['exit_code'], -1, 255, where)
    _n(v['t_ms'], 0, 10 ** 8, where)


def _services_map(v, check, where, allow_none=True):
    _keys(v, SERVICES, where)
    for s in SERVICES:
        if v[s] is None:
            _req(allow_none, where + '.' + s)
        else:
            check(v[s], where + '.' + s)


def _sched(v, where):
    _keys(v, ('interval_s', 'timeout_s', 'start_period_s', 'retries', 'budget_s', 'test_sha256'), where)
    for k in ('interval_s', 'timeout_s', 'start_period_s', 'budget_s'):
        _n(v[k], 0, 10 ** 7, where + '.' + k, real=True)
    _n(v['retries'], 1, 1000, where); _hex(v['test_sha256'], where)


def _basis(v, where):
    _keys(v, ('source', 'services', 'gate88_s', 'gate116_s', 'allowance_sampling_s', 'allowance_post_s',
              'total_s', 'cap_s'), where)
    _enum(v['source'], ('rendered-compose-config-no-interpolate',), where)
    _services_map(v['services'], _sched, where + '.services')
    for k in ('gate88_s', 'gate116_s', 'total_s'):
        _n(v[k], 0, 10 ** 7, where + '.' + k, real=True)
    _req(v['allowance_sampling_s'] == ALLOWANCE_SAMPLING and v['allowance_post_s'] == ALLOWANCE_POST
         and v['cap_s'] == CAP_SECONDS, where)


def _timeline(v, where):
    _req(isinstance(v, list) and len(v) <= MAX_TIMELINE, where)
    for r in v:
        _keys(r, ('t_ms', 'status', 'health', 'restart_count', 'started_at', 'failing_streak'), where)
        _n(r['t_ms'], 0, 10 ** 8, where); _enum(r['status'], STATUSES, where); _enum(r['health'], HEALTHS, where)
        _n(r['restart_count'], 0, 1000000, where); _ts(r['started_at'], where); _n(r['failing_streak'], 0, 1000000, where)


def _hc_row(v, where):
    _keys(v, ('healthcheck_sha256', 'timing_matches_rendered', 'test_matches_rendered', 'test_matches_comparator'), where)
    _opt(v['healthcheck_sha256'], _hex, where); _b(v['timing_matches_rendered'], where); _b(v['test_matches_rendered'], where)
    _opt(v['test_matches_comparator'], _b, where)


def _image_row(v, where):
    _keys(v, ('image_id', 'repo_digests', 'repo_tags'), where); _image(v['image_id'], where)
    for k, rx in (('repo_digests', REPO_DIGEST), ('repo_tags', REPO_TAG)):
        _req(isinstance(v[k], list) and len(v[k]) <= MAX_REPO_REFS and
             all(isinstance(x, str) and rx.fullmatch(x) for x in v[k]), where + '.' + k)


def _tail(v, where):
    _keys(v, ('service', 'bytes', 'lines', 'truncated', 'stream_valid', 'codes', 'unknown_code_count',
              'rejected', 'unmatched_lines', 'insufficient', 'identity_rechecked'), where)
    _enum(v['service'], SERVICES, where); _n(v['bytes'], 0, LOG_BYTES, where); _n(v['lines'], 0, LOG_TAIL, where)
    for k in ('truncated', 'stream_valid', 'insufficient', 'identity_rechecked'):
        _b(v[k], where + '.' + k)
    _req(isinstance(v['codes'], dict) and len(v['codes']) <= len(LOG_CODES), where)
    for k, c in v['codes'].items():
        _enum(k, LOG_CODES, where); _n(c, 1, LOG_TAIL * 8, where)
    _n(v['unknown_code_count'], 0, LOG_TAIL * 8, where); _n(v['unmatched_lines'], 0, LOG_TAIL, where)
    _keys(v['rejected'], ('control', 'secret', 'forged', 'overlong', 'undecodable'), where)
    for c in v['rejected'].values():
        _n(c, 0, LOG_TAIL, where)


def _migrations(v, where):
    _keys(v, ('state', 'exit_code', 'rows', 'unknown_count', 'membership', 'order_consistent', 'allowlist_sha256'), where)
    _enum(v['state'], MIGRATION_STATES, where); _opt(v['exit_code'], lambda x, w: _n(x, -1, 255, w), where)
    _n(v['rows'], 0, MAX_MIGRATION_ROWS, where); _n(v['unknown_count'], 0, MAX_MIGRATION_ROWS, where)
    _opt(v['order_consistent'], _b, where); _hex(v['allowlist_sha256'], where)
    _req(isinstance(v['membership'], dict) and len(v['membership']) <= 128, where)
    for k, row in v['membership'].items():
        _req(isinstance(k, str) and re.fullmatch(r'[0-9]{4}_[a-z0-9_-]{1,64}', k), where + '.name')
        _keys(row, ('pinned', 'upstream_v4_11_0'), where)
        _opt(row['pinned'], _b, where); _opt(row['upstream_v4_11_0'], _b, where)


def _upstream(v, where):
    _keys(v, ('setup_sha256', 'setup_bytes', 'setup_matches_staging_snapshot', 'cli_sha256', 'cli_version',
              'cli_matches_v4_11_0', 'compose_sha256', 'compose_matches_cli_template',
              'override_entry_sha256', 'override_return_sha256', 'upstream_manager_ran_before_install',
              'upstream_manager'), where)
    for k in ('setup_sha256', 'cli_sha256', 'compose_sha256', 'override_entry_sha256', 'override_return_sha256'):
        _opt(v[k], _hex, where + '.' + k)
    _opt(v['setup_bytes'], lambda x, w: _n(x, 0, 1048576, w), where)
    for k in ('setup_matches_staging_snapshot', 'cli_matches_v4_11_0', 'compose_matches_cli_template',
              'upstream_manager_ran_before_install'):
        _opt(v[k], _b, where + '.' + k)
    _opt(v['cli_version'], lambda x, w: _req(isinstance(x, str) and VERSION.fullmatch(x) and len(x) <= 48, w), where)
    if v['upstream_manager'] is not None:
        u = v['upstream_manager']
        _keys(u, ('id', 'image_id', 'started_at', 'finished_at', 'status', 'image_is_pinned_backend'), where)
        _hex(u['id'], where); _image(u['image_id'], where); _ts(u['started_at'], where); _ts(u['finished_at'], where)
        _enum(u['status'], STATUSES, where); _b(u['image_is_pinned_backend'], where)


EXPORT_KEYS = ('schema', 'label', 'result', 'result_reason', 'cause_class', 'hypotheses', 'install',
               'incident', 'final_snapshot', 'final_identity_changed', 'pre_install', 'timeline',
               'timeline_dropped', 'unarmed_states', 'ticks', 'samples', 'gates_seen_ms', 'deadline', 'basis_crosscheck',
               'identity', 'upstream', 'images', 'migrations', 'probe', 'log_tail', 'collector_errors')


def validate_export(doc):
    _keys(doc, EXPORT_KEYS, 'export')
    _req(doc['schema'] == SCHEMA and doc['label'] == LABEL, 'schema')
    _enum(doc['result'], RESULTS, 'result'); _enum(doc['result_reason'], REASONS, 'reason')
    _opt(doc['cause_class'], lambda x, w: _enum(x, CAUSES, w), 'cause')
    _keys(doc['hypotheses'], tuple(H_VALUES), 'hypotheses')
    for k, vals in H_VALUES.items():
        _enum(doc['hypotheses'][k], vals, 'hypotheses.' + k)
    ins = doc['install']
    _keys(ins, ('return', 'exception_type', 'returncode', 'line291_reached', 'pre291_command', 'quiesced'), 'install')
    _enum(ins['return'], INSTALL_RETURNS, 'install.return')
    _opt(ins['exception_type'], lambda x, w: _enum(x, EXC_TYPES, w), 'install.exc')
    _opt(ins['returncode'], lambda x, w: _n(x, -64, 255, w), 'install.rc')
    _b(ins['line291_reached'], 'install.reached'); _b(ins['quiesced'], 'install.quiesced')
    _opt(ins['pre291_command'], lambda x, w: _enum(x, PRE291_COMMANDS, w), 'install.pre291')
    inc = doc['incident']
    if inc is not None:
        _keys(inc, ('service', 'gate', 'kind', 'active_gate_observed', 'after_command_exit', 't_ms', 'snapshot'), 'incident')
        _b(inc['after_command_exit'], 'incident.after')
        _enum(inc['service'], SERVICES, 'incident'); _req(inc['gate'] == GATE_OF[inc['service']], 'incident.gate')
        _enum(inc['kind'], INCIDENT_KINDS, 'incident'); _opt(inc['active_gate_observed'], lambda x, w: _enum(x, GATES, w), 'incident')
        _n(inc['t_ms'], 0, 10 ** 8, 'incident'); _snap(inc['snapshot'], 'incident.snapshot')
        _req(doc['result'].startswith('REPRODUCED_') and doc['result'] ==
             'REPRODUCED_' + inc['gate'] + '_' + inc['service'].upper().replace('-', '_'), 'incident/result')
        _req(doc['cause_class'] is not None, 'cause required')
    else:
        _req(not doc['result'].startswith('REPRODUCED_') and doc['cause_class'] is None, 'result without incident')
    _services_map(doc['final_snapshot'], _snap, 'final')
    _b(doc['final_identity_changed'], 'final_identity_changed')
    _services_map(doc['pre_install'], _snap, 'pre')
    _services_map(doc['timeline'], _timeline, 'timeline', allow_none=False)
    _keys(doc['timeline_dropped'], SERVICES, 'dropped')
    for v in doc['timeline_dropped'].values():
        _n(v, 0, 10 ** 6, 'dropped')
    _keys(doc['unarmed_states'], SERVICES, 'unarmed')
    for v in doc['unarmed_states'].values():
        _n(v, 0, 10 ** 6, 'unarmed')
    _n(doc['ticks'], 0, CAP_SECONDS + 4, 'ticks'); _n(doc['samples'], 0, (CAP_SECONDS + 4) * 4, 'samples')
    _keys(doc['gates_seen_ms'], GATES, 'gates')
    for v in doc['gates_seen_ms'].values():
        _opt(v, lambda x, w: _n(x, 0, 10 ** 8, w), 'gates')
    dl = doc['deadline']
    _keys(dl, ('basis', 'hit', 'elapsed_ms', 'max_ticks', 'rerender_matches_entry'), 'deadline')
    _opt(dl['rerender_matches_entry'], _b, 'deadline.rerender')
    _opt(dl['basis'], _basis, 'deadline.basis'); _b(dl['hit'], 'deadline.hit')
    _opt(dl['elapsed_ms'], lambda x, w: _n(x, 0, 10 ** 8, w), 'deadline')
    _opt(dl['max_ticks'], lambda x, w: _n(x, 0, CAP_SECONDS + 4, w), 'deadline')
    _services_map(doc['basis_crosscheck'], _hc_row, 'crosscheck')
    idn = doc['identity']
    if idn is not None:
        _keys(idn, ('engine', 'boot', 'socket'), 'identity')
        _req(isinstance(idn['engine'], str) and ENGINE_ID.fullmatch(idn['engine']), 'identity.engine')
        _req(isinstance(idn['boot'], str) and BOOT_ID.fullmatch(idn['boot']), 'identity.boot')
        _req(isinstance(idn['socket'], list) and len(idn['socket']) == 2, 'identity.socket')
        for x in idn['socket']:
            _n(x, 0, 2 ** 64, 'identity.socket')
    _upstream(doc['upstream'], 'upstream')
    _keys(doc['images'], SERVICES + ('upstream-manager',), 'images')
    for v in doc['images'].values():
        _opt(v, _image_row, 'images')
    _migrations(doc['migrations'], 'migrations')
    if doc['probe'] is not None:
        _keys(doc['probe'], ('curl_exit', 'http_status'), 'probe'); _n(doc['probe']['curl_exit'], -1, 255, 'probe')
        _opt(doc['probe']['http_status'], lambda x, w: _n(x, 100, 599, w), 'probe')
    _opt(doc['log_tail'], _tail, 'log_tail')
    _req(isinstance(doc['collector_errors'], list) and len(doc['collector_errors']) <= 32 and
         all(isinstance(e, str) and e in COLLECTOR_ERRORS for e in doc['collector_errors']), 'collector_errors')
    return True


# ---------------------------------------------------------------- assembly ---
def empty_upstream():
    return {'setup_sha256': None, 'setup_bytes': None, 'setup_matches_staging_snapshot': None,
            'cli_sha256': None, 'cli_version': None, 'cli_matches_v4_11_0': None,
            'compose_sha256': None, 'compose_matches_cli_template': None,
            'override_entry_sha256': None, 'override_return_sha256': None,
            'upstream_manager_ran_before_install': None, 'upstream_manager': None}


def empty_migrations(allowlist_sha256):
    return {'state': 'not_attempted', 'exit_code': None, 'rows': 0, 'unknown_count': 0,
            'membership': {}, 'order_consistent': None, 'allowlist_sha256': allowlist_sha256}


def upstream_manager(obs, expected_backend):
    pre = obs.pre.get('runtipi') if obs else None
    if pre is None:
        return None, None
    if obs.pre_manager_id is None:
        return False, None              # pre-existing manager already ran the pinned image
    ran = ts_ns(pre['started_at']) > 0
    return ran, {'id': pre['id'], 'image_id': pre['image_id'], 'started_at': pre['started_at'],
                 'finished_at': pre['finished_at'], 'status': pre['status'],
                 'image_is_pinned_backend': pre['image_id'] == expected_backend}


def assemble(obs, st):
    """Build the closed export from guest state; the caller validates it."""
    inc = obs.incident if obs else None
    install = st['install']
    probe = st.get('probe'); tail = st.get('log_tail')
    cause = cause_class(inc, probe)
    result, reason = classify({
        'incident': inc, 'install': install, 'refusal': st.get('refusal'),
        'violation': obs.violation if obs else None, 'deadline_hit': st.get('deadline_hit', False),
        'pre291_budget_exhausted': st.get('pre291_budget_exhausted', False),
        'basis_mismatch': [bool(v) for v in obs.basis_mismatch.values()] if obs else [],
        'line291_reached': install['line291_reached']})
    final = st.get('final') or {s: None for s in SERVICES}
    upstream = dict(st.get('upstream') or empty_upstream())
    if obs is not None and upstream.get('upstream_manager') is None:
        ran, row = upstream_manager(obs, st.get('expected_backend'))
        upstream['upstream_manager_ran_before_install'] = ran; upstream['upstream_manager'] = row
    basis = obs.basis if obs else None
    migrations = st.get('migrations') or empty_migrations(st['allowlist_sha256'])
    doc = {
        'schema': SCHEMA, 'label': LABEL, 'result': result, 'result_reason': reason,
        'cause_class': cause,
        'hypotheses': hypotheses(result, inc, cause, final, migrations, tail, basis),
        'install': install, 'incident': inc, 'final_snapshot': final,
        'final_identity_changed': bool(st.get('final_identity_changed')),
        'pre_install': dict(obs.pre) if obs else {s: None for s in SERVICES},
        'timeline': {s: list(obs.timeline[s]) for s in SERVICES} if obs else {s: [] for s in SERVICES},
        'timeline_dropped': dict(obs.timeline_dropped) if obs else {s: 0 for s in SERVICES},
        'unarmed_states': dict(obs.unarmed_states) if obs else {s: 0 for s in SERVICES},
        'ticks': obs.ticks if obs else 0, 'samples': obs.samples if obs else 0,
        'gates_seen_ms': dict(obs.gate_ms) if obs else {g: None for g in GATES},
        'deadline': {'basis': basis, 'hit': bool(st.get('deadline_hit', False)),
                     'elapsed_ms': st.get('elapsed_ms'), 'max_ticks': obs.max_ticks if obs else None,
                     'rerender_matches_entry': st.get('rerender_matches_entry')},
        'basis_crosscheck': st.get('crosscheck') or {s: None for s in SERVICES},
        'identity': obs.baseline if obs else None, 'upstream': upstream,
        'images': st.get('images') or {k: None for k in SERVICES + ('upstream-manager',)},
        'migrations': migrations, 'probe': probe, 'log_tail': tail,
        'collector_errors': list(dict.fromkeys(st.get('collector_errors') or []))[:32]}
    validate_export(doc)
    return doc


def image_row(doc, image_id):
    if not isinstance(doc, dict) or doc.get('Id') != image_id:
        raise Violation('identity_image_swap')
    out = {'image_id': image_id, 'repo_digests': [], 'repo_tags': []}
    for key, dst, rx in (('RepoDigests', 'repo_digests', REPO_DIGEST), ('RepoTags', 'repo_tags', REPO_TAG)):
        vals = doc.get(key) or []
        if not isinstance(vals, list):
            raise Violation('identity_response_bound')
        out[dst] = sorted(v for v in vals if isinstance(v, str) and rx.fullmatch(v))[:MAX_REPO_REFS]
    return out
