#!/usr/bin/env python3
"""Pure, finite classification + schema for the manager-start discriminator.

No Docker, VM or network. Shared by the guest (builds the export), the host
runner (re-validates it) and the synthetic fixtures. Bounded log parsing and
migration membership are REUSED from the health-capture collector.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re

_HERE = Path(__file__).resolve().parent


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path); assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


HC = _module('msq_hc_collector', _HERE / 'hc-collector.py' if (_HERE / 'hc-collector.py').is_file()
             else _HERE / 'health_capture_collector.py')

SCHEMA = 'privsep-manager-start-v1'
LABEL = 'manager-start-discriminator'
ARMS = ('A', 'B', 'C')
ARM_NAMES = {'A': 'clean', 'B': 'upstream-premigrated', 'C': 'product-wired'}
PROJECTS = {'A': 'msq-a', 'B': 'msq-b', 'C': 'runtipi'}   # C: install.sh:116 exact `-p runtipi`
RESULTS = ('STARTS_HEALTHY', 'EXITS_CLEAN_SILENT', 'EXITS_ERROR', 'DEADLINE', 'INSUFFICIENT')
FAILS = ('EXITS_CLEAN_SILENT', 'EXITS_ERROR', 'DEADLINE')
VERDICTS = ('IMAGE_NEVER_STARTS', 'UPSTREAM_SCHEMA_BREAKS', 'PRODUCT_WIRING_BREAKS', 'NOT_REPRODUCED',
            'MIXED', 'INSUFFICIENT')
INSUFFICIENT_REASONS = ('none', 'arm_not_run', 'arm_exception', 'previous_arm_cleanup_failed', 'basis_refused',
                        'base_up_failed', 'db_not_healthy', 'queue_not_running', 'upstream_up_failed',
                        'upstream_not_healthy', 'upstream_not_migrated', 'upstream_stop_failed',
                        'manager_up_failed', 'manager_not_unique', 'image_identity_mismatch', 'observe_failed',
                        'diag_invalid', 'events_unreadable', 'wiring_invalid')
STOP_REASONS = ('healthy', 'exits_observed', 'deadline', 'not_started')
STATUSES = HC.STATUSES
HEALTHS = HC.HEALTHS
MILESTONES = ('preload_loaded', 'main_module_evaluated', 'env_file_generated', 'nest_create_started',
              'nest_create_finished', 'bootstrap_finished', 'listen_called', 'listening_3000')
RESOURCES = ('CloseReq', 'FSEventWrap', 'FSReqCallback', 'FSReqPromise', 'GetAddrInfoReqWrap',
             'GetNameInfoReqWrap', 'HTTPINCOMINGMESSAGE', 'HTTPCLIENTREQUEST', 'Immediate', 'JSSTREAM',
             'MessagePort', 'PipeConnectWrap', 'PipeServerWrap', 'PipeWrap', 'ProcessWrap',
             'PromiseResolveThenableJob', 'QueryReqWrap', 'ShutdownWrap', 'SignalWrap', 'StatWatcher',
             'TCPConnectWrap', 'TCPServerWrap', 'TCPWrap', 'TTYWrap', 'Timeout', 'UDPSendWrap', 'UDPWrap',
             'Worker', 'WriteWrap', 'ZlibWrap', 'OTHER')
CTORS = ('AbortError', 'AggregateError', 'CircularDependencyException', 'DatabaseError', 'DrizzleError',
         'DrizzleQueryError', 'Error', 'EvalError', 'HttpException', 'InvalidClassModuleException',
         'PostgresError', 'RangeError', 'ReferenceError', 'RuntimeException', 'SyntaxError',
         'TransactionRollbackError', 'TypeError', 'URIError', 'UndefinedDependencyException',
         'UnknownDependenciesException', 'UnknownElementException', 'ValidationError', 'OTHER')
ORIGINS = ('uncaughtException', 'unhandledRejection', 'unhandledRejection_event', 'OTHER')
LOADER_STATES = ('not_attempted', 'registered', 'failed')
REFLECT_STATES = ('not_armed', 'armed', 'absent')
MIGRATION_CLASSES = ('pinned_only', 'includes_upstream_only', 'unknown_rows', 'empty', 'unreadable',
                     'not_attempted')
ERROR_CODE = re.compile(r'E[A-Z0-9_]{2,30}')
ID64 = re.compile(r'[0-9a-f]{64}')
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
COLLECTOR_ERRORS = ('diag_unreadable', 'events_bound', 'logs_unreadable', 'inspect_failed', 'arm_cleanup_failed',
                    'migration_read_failed', 'identity_changed', 'collector_internal_error')
MAX_DIE = 8
WIRED_PATHS = ('.', '.env', 'user-config', 'state', 'data')
MAX_DIAG = 8
DIAG_BYTES = 8192
EXIT_EVENTS_STOP = 8          # arm window ends early after this many manager exits
ALLOWANCE_S = 30              # Compose/start allowance added ONLY to the base-service wait


class Violation(Exception):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_sha(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


# ---------------------------------------------------------------- deadline ---
def basis(config):
    """Template healthcheck budgets from the ACTUAL rendered (no-interpolate) arm config.

    budget = start_period + retries * (interval + timeout)  (HC.schedule). Returns
    (basis, None) or (None, 'basis_refused').
    """
    try:
        services = config['services']
        mgr = HC.schedule(services['runtipi'].get('healthcheck'))
        db = HC.schedule(services['runtipi-db'].get('healthcheck'))
    except (KeyError, TypeError, AttributeError):
        return None, 'basis_refused'
    if not isinstance(mgr, dict) or not isinstance(db, dict) or mgr['budget_s'] > 900 or db['budget_s'] > 900:
        return None, 'basis_refused'
    return {'source': 'rendered-compose-config-no-interpolate', 'runtipi': mgr, 'runtipi-db': db,
            'manager_window_s': mgr['budget_s'], 'upstream_window_s': mgr['budget_s'],
            'base_wait_s': db['budget_s'] + ALLOWANCE_S}, None


# -------------------------------------------------------------- diag JSON ---
def _int(v, lo, hi):
    return type(v) is int and lo <= v <= hi


def _code(v):
    return v == 'undefined' or v == 'OTHER' or _int(v, -255, 255)


def validate_diag(raw):
    """Strict projection of one preload JSON (bytes). Raises Violation if not finite/allowlisted."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= DIAG_BYTES:
        raise Violation('diag_bytes')
    try:
        d = json.loads(raw)
    except ValueError:
        raise Violation('diag_json') from None
    keys = {'schema', 'slot', 'node_major', 'milestones', 't_ms', 'loader_hook', 'reflect_hook', 'exit',
            'before_exit', 'uncaught', 'exit_calls', 'hook_errors'}
    if not isinstance(d, dict) or set(d) != keys or d['schema'] != 'msq-diag-v1':
        raise Violation('diag_keys')
    ok = (_int(d['slot'], 0, MAX_DIAG - 1) and _int(d['node_major'], 0, 99) and
          isinstance(d['milestones'], list) and len(d['milestones']) == len(set(d['milestones'])) and
          all(m in MILESTONES for m in d['milestones']) and
          isinstance(d['t_ms'], dict) and set(d['t_ms']) == set(d['milestones']) and
          all(_int(v, 0, 3600000) for v in d['t_ms'].values()) and
          d['loader_hook'] in LOADER_STATES and d['reflect_hook'] in REFLECT_STATES and
          _int(d['hook_errors'], 0, 99))
    e = d['exit']; b = d['before_exit']; u = d['uncaught']; calls = d['exit_calls']
    ok = ok and isinstance(e, dict) and set(e) == {'fired', 'code'} and type(e['fired']) is bool and \
        ((e['code'] is None and not e['fired']) or (e['fired'] and _code(e['code'])))
    ok = ok and isinstance(b, dict) and set(b) == {'fired', 'count', 'resources'} and type(b['fired']) is bool and \
        _int(b['count'], 0, 99) and (b['count'] > 0) == b['fired'] and isinstance(b['resources'], dict) and \
        len(b['resources']) <= len(RESOURCES) and \
        all(k in RESOURCES and _int(v, 1, 9999) for k, v in b['resources'].items())
    ok = ok and isinstance(u, dict) and set(u) == {'count', 'first'} and _int(u['count'], 0, 99) and \
        ((u['count'] == 0 and u['first'] is None) or (u['count'] > 0 and isinstance(u['first'], dict) and
         set(u['first']) == {'ctor', 'code', 'origin'} and u['first']['ctor'] in CTORS and
         u['first']['origin'] in ORIGINS and (u['first']['code'] in (None, 'OTHER') or
         (isinstance(u['first']['code'], str) and ERROR_CODE.fullmatch(u['first']['code'])))))
    ok = ok and isinstance(calls, list) and len(calls) <= 4 and all(_code(c) for c in calls)
    if not ok:
        raise Violation('diag_values')
    order = [m for m in MILESTONES if m in d['milestones']]
    return {'slot': d['slot'], 'node_major': d['node_major'], 'milestones': order,
            'last_milestone': order[-1] if order else None,
            't_ms': {m: d['t_ms'][m] for m in order}, 'loader_hook': d['loader_hook'],
            'reflect_hook': d['reflect_hook'], 'exit_fired': e['fired'], 'exit_code': e['code'],
            'before_exit_fired': b['fired'], 'before_exit_count': b['count'],
            'active_resources_at_before_exit': dict(sorted(b['resources'].items())),
            'uncaught_count': u['count'], 'uncaught_first': u['first'], 'process_exit_calls': list(calls),
            'hook_errors': d['hook_errors']}


def diag_mechanism(rows):
    """Finite summary of HOW each instrumented process ended (no inference beyond the enum)."""
    if not rows:
        return 'NO_DIAG'
    kinds = set()
    for r in rows:
        if r['process_exit_calls']:
            kinds.add('PROCESS_EXIT_CALLED')
        elif r['uncaught_count']:
            kinds.add('UNCAUGHT')
        elif r['before_exit_fired'] and r['exit_fired'] and r['exit_code'] == 0:
            kinds.add('EVENT_LOOP_DRAINED')
        elif not r['exit_fired']:
            kinds.add('NO_EXIT_EVENT')
        else:
            kinds.add('OTHER')
    return kinds.pop() if len(kinds) == 1 else 'MIXED'


DIAG_MECHANISMS = ('NO_DIAG', 'PROCESS_EXIT_CALLED', 'UNCAUGHT', 'EVENT_LOOP_DRAINED', 'NO_EXIT_EVENT',
                   'OTHER', 'MIXED')


# ------------------------------------------------------------------ events ---
def parse_events(raw, stop_ns):
    """Docker /events stream (bounded) -> per-run die rows strictly before the QA stop."""
    if not isinstance(raw, bytes) or len(raw) > 262144:
        raise Violation('events_bound')
    dec = json.JSONDecoder(); text = raw.decode('utf-8'); pos = 0; rows = []
    while True:
        while pos < len(text) and text[pos] in ' \r\n\t':
            pos += 1
        if pos >= len(text):
            break
        obj, pos = dec.raw_decode(text, pos)
        if not isinstance(obj, dict):
            raise Violation('events_bound')
        rows.append(obj)
        if len(rows) > 256:
            raise Violation('events_bound')
    last_start = None; dies = []; oom = 0; starts = 0
    for ev in rows:
        action = ev.get('Action') or ev.get('status'); t = ev.get('timeNano')
        if type(t) is not int or t <= 0:
            raise Violation('events_bound')
        if t > stop_ns:
            continue
        if action == 'start':
            last_start = t; starts += 1
        elif action == 'oom':
            oom += 1
        elif action == 'die':
            code = (ev.get('Actor') or {}).get('Attributes', {}).get('exitCode')
            code = int(code) if isinstance(code, str) and re.fullmatch(r'-?[0-9]{1,3}', code) else None
            life = None if last_start is None else max(0, (t - last_start) // 1000000)
            if len(dies) < MAX_DIE:
                dies.append({'exit_code': code if code is not None and -255 <= code <= 255 else None,
                             'lifetime_ms': life if life is None or life <= 3600000 else 3600000})
            last_start = None
    die_count = sum(1 for e in rows if (e.get('Action') or e.get('status')) == 'die' and e['timeNano'] <= stop_ns)
    return {'starts': min(starts, 999), 'dies': dies, 'die_count': min(die_count, 999), 'oom_events': min(oom, 999)}


# ---------------------------------------------------------- classification ---
def arm_result(arm):
    """Finite arm vocabulary. Precedence: INSUFFICIENT > healthy > exits > deadline."""
    if arm['insufficient_reason'] != 'none':
        return 'INSUFFICIENT'
    m = arm['manager']
    if m['health_reached']:
        return 'STARTS_HEALTHY'
    ev = m['events']
    if ev['die_count'] > 0:
        clean = all(d['exit_code'] == 0 for d in ev['dies']) and ev['oom_events'] == 0 and not m['oom_killed']
        silent = m['log']['bytes'] == 0 and m['log']['stream_valid']
        return 'EXITS_CLEAN_SILENT' if clean and silent else 'EXITS_ERROR'
    if m['stop_reason'] == 'deadline':
        return 'DEADLINE'
    return 'INSUFFICIENT'


def verdict(results):
    a, b, c = (results[k] for k in ARMS)
    if a == 'EXITS_CLEAN_SILENT':
        return 'IMAGE_NEVER_STARTS'            # A fails exactly like the capture
    if a == 'INSUFFICIENT':
        return 'INSUFFICIENT'
    if a != 'STARTS_HEALTHY':
        return 'MIXED'                         # A fails, but not like the capture
    if b == 'INSUFFICIENT':
        return 'INSUFFICIENT'
    if b in FAILS:
        return 'UPSTREAM_SCHEMA_BREAKS'
    if c == 'INSUFFICIENT':
        return 'INSUFFICIENT'
    if c in FAILS:
        return 'PRODUCT_WIRING_BREAKS'
    return 'NOT_REPRODUCED'


def migration_class(m, allow):
    if m['state'] == 'not_attempted':
        return 'not_attempted'
    if m['state'] == 'unreadable':
        return 'unreadable'
    if m['state'] == 'table_absent' or m['rows'] == 0:
        return 'empty'
    if m['unknown_count']:
        return 'unknown_rows'
    pinned = allow['pinned']
    up_only = [n for n, row in m['membership'].items() if row['upstream_v4_11_0'] and not row['pinned']
               and (n not in pinned or tuple(allow['upstream_v4_11_0'][n]) != tuple(pinned[n]))]
    return 'includes_upstream_only' if up_only else 'pinned_only'


def upstream_only_applied(m):
    return sorted(n for n, row in m['membership'].items() if row['upstream_v4_11_0'] and not row['pinned'])


def migration_row(m, allow):
    return {'state': m['state'], 'rows': m['rows'], 'unknown_count': m['unknown_count'],
            'order_consistent': m['order_consistent'], 'class': migration_class(m, allow),
            'upstream_only_applied': upstream_only_applied(m) if m['state'] == 'read' else []}


# ------------------------------------------------------------------ schema ---
def _req(cond, where):
    if not cond:
        raise Violation('schema:' + where)


def _sched(v, where):
    _req(isinstance(v, dict) and set(v) == {'interval_s', 'timeout_s', 'start_period_s', 'retries', 'budget_s',
                                            'test_sha256'}, where)
    _req(re.fullmatch('[0-9a-f]{64}', v['test_sha256']) is not None and _int(v['retries'], 1, 1000), where)
    _req(all(isinstance(v[k], (int, float)) and 0 <= v[k] <= 3600 for k in ('interval_s', 'timeout_s',
                                                                           'start_period_s', 'budget_s')), where)


def _mig(v, where):
    _req(isinstance(v, dict) and set(v) == {'state', 'rows', 'unknown_count', 'order_consistent', 'class',
                                            'upstream_only_applied'}, where)
    _req(v['state'] in HC.MIGRATION_STATES and v['class'] in MIGRATION_CLASSES and _int(v['rows'], 0, 256), where)
    _req(_int(v['unknown_count'], 0, 256) and v['order_consistent'] in (None, True, False), where)
    _req(isinstance(v['upstream_only_applied'], list) and len(v['upstream_only_applied']) <= 64 and
         all(isinstance(n, str) and re.fullmatch('[0-9a-z_-]{1,64}', n) for n in v['upstream_only_applied']), where)


def _ids(v, where):
    _req(isinstance(v, dict) and set(v) <= {'runtipi-db', 'runtipi-queue', 'runtipi', 'upstream'}, where)
    _req(all(x is None or (isinstance(x, str) and ID64.fullmatch(x)) for x in v.values()), where)


def _log(v, where):
    keys = {'bytes', 'lines', 'truncated', 'stream_valid', 'codes', 'unknown_code_count', 'rejected',
            'unmatched_lines', 'insufficient'}
    _req(isinstance(v, dict) and set(v) == keys and _int(v['bytes'], 0, HC.LOG_BYTES), where)
    _req(isinstance(v['codes'], dict) and all(k in HC.LOG_CODES and _int(n, 1, 100000) for k, n in v['codes'].items()), where)


def _manager(v, where):
    keys = {'image_ref_is_pinned', 'window_s', 'elapsed_ms', 'stop_reason', 'health_reached', 'health_ms',
            'events', 'restart_count', 'final_status', 'final_exit_code', 'oom_killed', 'log', 'stopped_by_qa'}
    _req(isinstance(v, dict) and set(v) == keys, where)
    _req(type(v['image_ref_is_pinned']) is bool and v['stop_reason'] in STOP_REASONS, where)
    _req(type(v['health_reached']) is bool and (v['health_ms'] is None or _int(v['health_ms'], 0, 3600000)), where)
    _req(_int(v['elapsed_ms'], 0, 3600000) and isinstance(v['window_s'], (int, float)) and 0 <= v['window_s'] <= 900, where)
    ev = v['events']
    _req(isinstance(ev, dict) and set(ev) == {'starts', 'dies', 'die_count', 'oom_events'} and
         _int(ev['starts'], 0, 999) and _int(ev['die_count'], 0, 999) and _int(ev['oom_events'], 0, 999), where)
    _req(isinstance(ev['dies'], list) and len(ev['dies']) <= MAX_DIE and all(
        isinstance(d, dict) and set(d) == {'exit_code', 'lifetime_ms'} and
        (d['exit_code'] is None or _int(d['exit_code'], -255, 255)) and
        (d['lifetime_ms'] is None or _int(d['lifetime_ms'], 0, 3600000)) for d in ev['dies']), where)
    _req(_int(v['restart_count'], 0, 9999) and v['final_status'] in STATUSES + ('absent',), where)
    _req(v['final_exit_code'] is None or _int(v['final_exit_code'], -255, 255), where)
    _req(type(v['oom_killed']) is bool and type(v['stopped_by_qa']) is bool, where)
    _log(v['log'], where + '.log')


def empty_manager(window_s=0):
    return {'image_ref_is_pinned': False, 'window_s': window_s, 'elapsed_ms': 0, 'stop_reason': 'not_started',
            'health_reached': False, 'health_ms': None,
            'events': {'starts': 0, 'dies': [], 'die_count': 0, 'oom_events': 0}, 'restart_count': 0,
            'final_status': 'absent', 'final_exit_code': None, 'oom_killed': False,
            'log': HC.parse_log(b'', truncated=False), 'stopped_by_qa': False}


def empty_migrations():
    return {'state': 'not_attempted', 'rows': 0, 'unknown_count': 0, 'order_consistent': None,
            'class': 'not_attempted', 'upstream_only_applied': []}


ARM_KEYS = ('arm', 'name', 'project', 'result', 'insufficient_reason', 'container_ids', 'basis', 'db_healthy',
            'queue_running', 'upstream', 'manager', 'diag', 'diag_invalid', 'diag_mechanism',
            'migrations_before_manager', 'migrations_after', 'wiring', 'cleanup_absent')


def empty_arm(arm):
    return {'arm': arm, 'name': ARM_NAMES[arm], 'project': PROJECTS[arm], 'result': 'INSUFFICIENT',
            'insufficient_reason': 'arm_not_run', 'container_ids': {}, 'basis': None, 'db_healthy': False,
            'queue_running': False, 'upstream': None, 'manager': empty_manager(), 'diag': [], 'diag_invalid': 0,
            'diag_mechanism': 'NO_DIAG', 'migrations_before_manager': empty_migrations(),
            'migrations_after': empty_migrations(), 'wiring': None, 'cleanup_absent': False}


def _arm(v, arm):
    w = 'arms.' + arm
    _req(isinstance(v, dict) and tuple(sorted(v)) == tuple(sorted(ARM_KEYS)), w)
    _req(v['arm'] == arm and v['name'] == ARM_NAMES[arm] and v['project'] == PROJECTS[arm], w)
    _req(v['result'] in RESULTS and v['insufficient_reason'] in INSUFFICIENT_REASONS, w)
    _req((v['result'] == 'INSUFFICIENT') == (v['insufficient_reason'] != 'none') or
         (v['result'] == 'INSUFFICIENT' and v['insufficient_reason'] == 'none'), w)
    _ids(v['container_ids'], w + '.ids')
    if v['basis'] is not None:
        _req(isinstance(v['basis'], dict) and v['basis'].get('source') == 'rendered-compose-config-no-interpolate', w)
        _sched(v['basis']['runtipi'], w + '.basis.runtipi'); _sched(v['basis']['runtipi-db'], w + '.basis.db')
        _req(set(v['basis']) == {'source', 'runtipi', 'runtipi-db', 'manager_window_s', 'upstream_window_s',
                                 'base_wait_s'}, w + '.basis')
    _req(type(v['db_healthy']) is bool and type(v['queue_running']) is bool and type(v['cleanup_absent']) is bool, w)
    up = v['upstream']
    if arm == 'B' and up is not None:
        _req(isinstance(up, dict) and set(up) == {'image_is_recorded_upstream', 'healthy', 'elapsed_ms',
                                                   'stopped_by_id', 'removed_by_id', 'exit_code'}, w + '.upstream')
        _req(all(type(up[k]) is bool for k in ('image_is_recorded_upstream', 'healthy', 'stopped_by_id',
                                                'removed_by_id')) and _int(up['elapsed_ms'], 0, 3600000), w)
        _req(up['exit_code'] is None or _int(up['exit_code'], -255, 255), w)
    else:
        _req(up is None, w + '.upstream')
    _manager(v['manager'], w + '.manager')
    _req(isinstance(v['diag'], list) and len(v['diag']) <= MAX_DIAG and _int(v['diag_invalid'], 0, MAX_DIAG), w)
    for d in v['diag']:
        _req(isinstance(d, dict) and d['last_milestone'] in MILESTONES + (None,), w + '.diag')
    _req(v['diag_mechanism'] in DIAG_MECHANISMS, w)
    _mig(v['migrations_before_manager'], w + '.mig_before'); _mig(v['migrations_after'], w + '.mig_after')
    if arm == 'C':
        wi = v['wiring']
        _req(wi is None or (isinstance(wi, dict) and set(wi) == {
            'override_sha256', 'image_matches_engine_id', 'base_env_keys', 'dir_modes_applied', 'host_approvals_sync_rc'}
            and re.fullmatch('[0-9a-f]{64}', wi['override_sha256'] or '') is not None
            and type(wi['image_matches_engine_id']) is bool
            and isinstance(wi['base_env_keys'], list) and len(wi['base_env_keys']) <= 64
            and all(isinstance(k, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,63}', k) for k in wi['base_env_keys'])
            and isinstance(wi['dir_modes_applied'], dict) and set(wi['dir_modes_applied']) <= set(WIRED_PATHS)
            and all(isinstance(m, str) and re.fullmatch('0o[0-7]{1,4}', m) for m in wi['dir_modes_applied'].values())
            and (wi['host_approvals_sync_rc'] is None or _int(wi['host_approvals_sync_rc'], -1, 255))), w + '.wiring')
    else:
        _req(v['wiring'] is None, w + '.wiring')


EXPORT_KEYS = ('schema', 'label', 'verdict', 'results', 'arms', 'images', 'backend', 'collector_errors',
               'migration_allowlist_sha256', 'template_sha256', 'engine_identity_stable')


def validate_export(doc):
    _req(isinstance(doc, dict) and tuple(sorted(doc)) == tuple(sorted(EXPORT_KEYS)), 'keys')
    _req(doc['schema'] == SCHEMA and doc['label'] == LABEL and doc['verdict'] in VERDICTS, 'head')
    _req(isinstance(doc['results'], dict) and set(doc['results']) == set(ARMS), 'results')
    _req(isinstance(doc['arms'], dict) and set(doc['arms']) == set(ARMS), 'arms')
    for a in ARMS:
        _arm(doc['arms'][a], a)
        _req(doc['results'][a] == doc['arms'][a]['result'] == arm_result(doc['arms'][a]), 'result.' + a)
    _req(doc['verdict'] == verdict(doc['results']), 'verdict')
    imgs = doc['images']
    _req(isinstance(imgs, dict) and set(imgs) == {'postgres', 'rabbitmq', 'upstream'}, 'images')
    for n, r in imgs.items():
        _req(isinstance(r, dict) and set(r) == {'id', 'id_kind', 'chain_verified'} and
             (r['id'] is None or IMAGE_ID.fullmatch(r['id'])) and r['id_kind'] in ('manifest', 'config', 'unbound')
             and type(r['chain_verified']) is bool, 'images.' + n)
    b = doc['backend']
    _req(isinstance(b, dict) and set(b) == {'engine_image_id', 'matches_capture', 'image_env_node_options_present'} and
         type(b['image_env_node_options_present']) is bool and
         IMAGE_ID.fullmatch(b['engine_image_id'] or '') and type(b['matches_capture']) is bool, 'backend')
    _req(isinstance(doc['collector_errors'], list) and len(doc['collector_errors']) <= 32 and
         all(e in COLLECTOR_ERRORS for e in doc['collector_errors']), 'collector_errors')
    for k in ('migration_allowlist_sha256', 'template_sha256'):
        _req(isinstance(doc[k], str) and re.fullmatch('[0-9a-f]{64}', doc[k]), k)
    _req(type(doc['engine_identity_stable']) is bool, 'engine_identity_stable')
    raw = json.dumps(doc, sort_keys=True).encode()
    _req(len(raw) <= 262144, 'bytes')
    return True


def finish(arms, images, backend, errors, allow_sha, template_sha, stable):
    for a in ARMS:
        arms[a]['result'] = arm_result(arms[a])
    results = {a: arms[a]['result'] for a in ARMS}
    doc = {'schema': SCHEMA, 'label': LABEL, 'verdict': verdict(results), 'results': results, 'arms': arms,
           'images': images, 'backend': backend, 'collector_errors': sorted(set(errors))[:32],
           'migration_allowlist_sha256': allow_sha, 'template_sha256': template_sha,
           'engine_identity_stable': stable}
    validate_export(doc)
    return doc
