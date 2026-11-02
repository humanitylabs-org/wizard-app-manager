"""Browser-trust admission state that the App Manager owns (default-off).

The Agent Service decides each browser data/state request, but only from facts this module
writes into apps.json. A frame or app never supplies any of them:

- installation_id: one Runtipi install of one app (new id after uninstall + reinstall);
- source_id:       `<store source>:<owner>/<repo>` recorded at install, so two publishers of
                   the same app id (`profiles`) are different principals;
- commit:          the installed commit (an update changes it and revokes open bindings);
- contract:        the normalized `browser_contract` from the INSTALLED wizard.md;
- granted:         owner browser grants that still match this installation and source;
- permission_revision: digest over contract + effective grants (any change revokes bindings).

A manifest declaration alone grants nothing; a grant alone admits nothing that the installed
contract does not declare. Browser grants are separate from WAM network/agent grants
(`wizard-grants.json`) and live in <WIZARD_OS_HOME>/browser-grants.json, written only here.
Nothing is projected unless WIZARD_BROWSER_BOUNDARY=1, so apps.json is unchanged by default.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

SCHEMA = 1
MAX_PERSISTENT_BYTES = 5 * 1024 * 1024
# Reviewed operation registry v1 (wizard-os spec/BROWSER_TRUST_ADAPTERS.md). The Service holds the
# matching handlers; an id missing here is refused at install projection AND at admission.
OPERATIONS = {
    'state.local': {'kind': 'state', 'scope': 'self'},
    'notifications.list': {'kind': 'read', 'scope': 'self', 'slot': 'notifications'},
    'notifications.mark_read': {'kind': 'write', 'scope': 'self', 'slot': 'notifications'},
    # Card 2 (wizard-os spec/browser-trust-card2-notifications): the real Notifications screens. Full inbox read
    # (views, counts, marks) and one compare-and-set mark write (read/unread, flag, archive, mark all read).
    'notifications.inbox': {'kind': 'read', 'scope': 'self', 'slot': 'notifications'},
    'notifications.mark': {'kind': 'write', 'scope': 'self', 'slot': 'notifications'},
    # Card 3 media (wizard-os ADAPTER-SKETCH §6): a 60 s read ticket for one picture inside one of this installation's
    # notifications. The frame names notification + picture index, never a path; the Service resolves it.
    'notifications.media': {'kind': 'media', 'scope': 'self', 'slot': 'notifications'},
    # Shell intents (wizard-os spec/browser-trust-shell-intents): the ChatGPT and Claude connection pages. Each is the
    # installation's OWN accounts page (scope self); `consent: owner` ops run only after the trusted-shell confirmation
    # (bound to exact args, source and permission revision, consumed once). `signin: True` ops may hand the shell a
    # provider sign-in URL, which the shell opens only if it matches the installed contract's `navigation` entry.
    'ai_accounts.inventory': {'kind': 'read', 'scope': 'self'},
    'ai_accounts.change': {'kind': 'write', 'scope': 'self', 'consent': 'owner'},
    'ai_accounts.progress': {'kind': 'read', 'scope': 'self', 'signin': True},
    'ai_accounts.step': {'kind': 'write', 'scope': 'self'},
    'claude_code.status': {'kind': 'read', 'scope': 'self'},
    'claude_code.connect': {'kind': 'write', 'scope': 'self', 'consent': 'owner', 'signin': True},
    'claude_code.code': {'kind': 'write', 'scope': 'self'},
    'claude_code.cancel': {'kind': 'write', 'scope': 'self'},
    'claude_code.logout': {'kind': 'write', 'scope': 'self', 'consent': 'owner'},
    'claude_code.test': {'kind': 'write', 'scope': 'self'},
}
# Narrow sign-in navigation (census label N): an app may declare up to four provider sign-in pages, each one exact
# https origin + path. Query parameter NAMES may be listed; values come only from the app's upstream at run time and
# are re-checked by the Service and the shell. No wildcard, port, userinfo, fragment or credential-shaped name.
MAX_NAVIGATION = 4
_NAV_ID = re.compile(r'[a-z][a-z0-9-]{0,31}')
_NAV_HOST = re.compile(r'(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
_NAV_PATH = re.compile(r'/(?:[A-Za-z0-9._~-]+/?)*')
_NAV_PARAM = re.compile(r'[a-z][a-z0-9_]{0,31}')
_NAV_FORBIDDEN = re.compile(r'token|secret|password|passwd|api_?key|session|cookie|assertion|credential|auth_?code$')
_REPO = re.compile(r'[a-z0-9][a-z0-9-]{0,38}/[a-z0-9._-]{1,100}')
_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get('WIZARD_BROWSER_BOUNDARY') == '1'


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalize_contract(w: dict) -> dict:
    """{'contract': {...}} for a valid declaration, {'contract': None} when absent, {'error': ...} otherwise."""
    raw = w.get('browser_contract') if isinstance(w, dict) else None
    if raw is None:
        return {'contract': None}
    if not isinstance(raw, dict) or set(raw) - {'version', 'isolation', 'operations', 'state', 'navigation'}:
        return {'error': 'browser_contract has unknown fields'}
    if raw.get('version') != 1 or raw.get('isolation') != 'opaque':
        return {'error': 'browser_contract needs version 1 and isolation opaque'}
    ops = raw.get('operations') or []
    if not isinstance(ops, list) or len(ops) > 32 or not all(isinstance(o, str) for o in ops):
        return {'error': 'browser_contract.operations must be a list of operation ids'}
    unknown = sorted({o for o in ops if o not in OPERATIONS})
    if unknown:   # no wildcards, no app-invented ids, no handler code in the manifest
        return {'error': 'unknown browser operations: ' + ', '.join(unknown)[:200]}
    state = raw.get('state') or {}
    if not isinstance(state, dict) or set(state) - {'schema_version', 'persistent_bytes'}:
        return {'error': 'browser_contract.state supports schema_version and persistent_bytes only'}
    persistent = state.get('persistent_bytes', 0)
    if not isinstance(persistent, int) or isinstance(persistent, bool) or persistent < 0:
        return {'error': 'browser_contract.state.persistent_bytes must be a non-negative integer'}
    nav = normalize_navigation(raw.get('navigation'), ops)
    if isinstance(nav, str):
        return {'error': nav}
    contract = {'version': 1, 'isolation': 'opaque', 'operations': sorted(set(ops)),
                'state': {'schema_version': 1, 'persistent_bytes': min(persistent, MAX_PERSISTENT_BYTES)}}
    if nav:
        contract['navigation'] = nav
    return {'contract': contract}


def normalize_navigation(raw, ops) -> list | str:
    """The reviewed sign-in pages, or an error string. Absent = []. A page needs an op that can produce one."""
    from urllib.parse import urlsplit
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_NAVIGATION:
        return f'browser_contract.navigation must be a list of at most {MAX_NAVIGATION} sign-in pages'
    if raw and not any(OPERATIONS.get(o, {}).get('signin') for o in ops):
        return 'browser_contract.navigation needs an operation that starts a sign-in'
    out, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {'id', 'url', 'query'} or not {'id', 'url'} <= set(entry):
            return 'browser_contract.navigation entries take id, url and optional query only'
        nid, url, query = entry['id'], entry['url'], entry.get('query') or []
        if not isinstance(nid, str) or not _NAV_ID.fullmatch(nid) or nid in seen:
            return 'browser_contract.navigation ids are unique lowercase names'
        seen.add(nid)
        if not isinstance(url, str) or len(url) > 300 or any(c in url for c in '*?#@\\ ') :
            return f'navigation {nid}: url must be one exact https origin and path (no wildcard, query, fragment or userinfo)'
        u = urlsplit(url)
        if u.scheme != 'https' or u.port is not None or not u.hostname or u.hostname != u.netloc \
                or not _NAV_HOST.fullmatch(u.hostname) or not _NAV_PATH.fullmatch(u.path or '/'):
            return f'navigation {nid}: url must be https://<host>/<path> on the default port'
        if not isinstance(query, list) or len(query) > 12 or not all(isinstance(q, str) and _NAV_PARAM.fullmatch(q) for q in query):
            return f'navigation {nid}: query lists at most 12 lowercase parameter names'
        bad = sorted({q for q in query if _NAV_FORBIDDEN.search(q)})
        if bad:
            return f'navigation {nid}: credential-shaped query names are refused: ' + ', '.join(bad)
        out.append({'id': nid, 'url': f'https://{u.hostname}{u.path or "/"}', 'query': sorted(set(query))})
    return sorted(out, key=lambda e: e['id'])


def source_id(source: str | None, repo: str | None) -> str | None:
    """Platform-recorded provenance. Unattributable installs get None and are never admitted."""
    if not isinstance(repo, str) or not isinstance(source, str) or not _ID.fullmatch(source):
        return None
    repo = repo.strip().lower()
    return f'{source}:{repo}' if _REPO.fullmatch(repo) else None


def installation_id(row: dict, store: str, app_id: str) -> str | None:
    """Stable per Runtipi install row (its serial id + creation time); a reinstall is a new row."""
    app = (row or {}).get('app') or {}
    serial, created = app.get('id'), app.get('createdAt', app.get('created_at'))
    if not isinstance(serial, int) or isinstance(serial, bool) or not isinstance(created, (int, str)) or created in ('', None):
        return None
    return 'inst-' + _digest(['wizard-installation-v1', store, app_id, serial, str(created)])[:32]


def grants_path(home: Path) -> Path:
    return Path(home) / 'browser-grants.json'


def read_grants(home: Path) -> dict:
    try:
        doc = json.loads(grants_path(home).read_text())
    except FileNotFoundError:
        return {'schema': SCHEMA, 'grants': []}
    except (OSError, ValueError) as e:
        # Unreadable grant state must never widen access: refuse every browser grant.
        raise ValueError(f'browser grants unreadable: {type(e).__name__}') from None
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA or not isinstance(doc.get('grants'), list):
        raise ValueError('browser grants have an unknown schema')
    return doc


def _write_grants(home: Path, doc: dict) -> None:
    path = grants_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.browser-grants.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if json.loads(path.read_text()) != doc:   # durable readback before the CLI reports success
        raise OSError('browser grants readback mismatch')


def _match(grant: dict, inst: str, src: str) -> bool:
    return isinstance(grant, dict) and grant.get('installation_id') == inst and grant.get('source_id') == src


def project(reg: dict, installed: list[dict], store: str, host: str, runtipi_root: Path, home: Path) -> dict:
    """Add `browser` admission facts to each app built by registry.build. Never raises for one app."""
    from .registry import app_meta
    try:
        grants = read_grants(home)['grants']
        grants_error = None
    except ValueError as e:
        grants, grants_error = [], str(e)
    rows = {}
    for row in installed:
        urn = ((row or {}).get('info') or {}).get('urn') or ''
        name, _, st = urn.partition(':')
        rows.setdefault(name, []).append((st, row))
    for app in reg.get('apps', []):
        app_id = app.get('id')
        candidates = rows.get(app_id, [])
        st, row = candidates[0] if len(candidates) == 1 else (None, None)
        meta = app_meta(runtipi_root, st, app_id) if st else None
        out = {'schema': SCHEMA, 'installation_id': None, 'source_id': None, 'commit': app.get('commit'),
               'contract': None, 'granted': [], 'grant_revision': 0, 'admissible': False}
        if len(candidates) != 1 or not meta:
            out['reason'] = 'ambiguous_installation' if len(candidates) > 1 else 'not_installed'
        else:
            norm = normalize_contract(meta['w'])
            inst = installation_id(row, st, app_id)
            src = source_id(meta['source'], meta['repo'])
            out.update(installation_id=inst, source_id=src)
            port = (row.get('app') or {}).get('port') or (meta['cfg'] or {}).get('port')
            out['upstream'] = f'http://{host}:{port}' if isinstance(port, int) and not isinstance(port, bool) else None
            if 'error' in norm:
                out['reason'] = 'invalid_contract'
                out['contract_error'] = norm['error']
            elif not norm['contract']:
                out['reason'] = 'no_contract'
            elif not inst or not src or not out['commit']:
                out['reason'] = 'unattributed_installation'
                out['contract'] = norm['contract']
            else:
                out['contract'] = norm['contract']
                mine = [g for g in grants if _match(g, inst, src)]
                declared = set(norm['contract']['operations'])
                granted = sorted({o for g in mine for o in (g.get('operations') or []) if o in declared})
                out['granted'] = granted
                out['grant_revision'] = max([int(g.get('revision') or 0) for g in mine] or [0])
                out['admissible'] = bool(granted) and not grants_error
                out['reason'] = None if out['admissible'] else ('grants_unreadable' if grants_error else 'no_grant')
        out['contract_digest'] = _digest(out['contract']) if out['contract'] else None
        out['permission_revision'] = 'p-' + _digest([out['contract_digest'], out['granted'], out['grant_revision']])[:24]
        if out.get('installation_id') and st:
            cred = listener_credential(home, runtipi_root, st, app_id, out['installation_id'])
            if cred:
                out['listener_credential'] = cred
        app['browser'] = out
    prune_listener_credentials(home, {a['browser']['installation_id'] for a in reg.get('apps', [])
                                      if (a.get('browser') or {}).get('listener_credential', {}).get('ref')})
    return reg


# ---- Per-installation listener credential (wizard-os #386) ----
# An app whose own listener is reachable straight from the private network (Tailscale Serve publishes its path and
# port to it, not through the shell) cannot treat the owner's network identity as authority once the boundary is on.
# Its owner-data paths then answer only the Agent Service adapter, which proves itself with this credential:
#   - minted here per INSTALLATION (a reinstall is a new installation and a new credential; uninstall removes it);
#   - stored server-side only, <home>/browser-app-credentials/<installation_id>.key (0600), read by the Service;
#   - handed to the app through its own Runtipi user env (WIZARD_APP_CREDENTIAL), only when its INSTALLED compose
#     declares that variable, so apps that don't support the gate are untouched;
#   - never in apps.json, a browser response, a URL, or the shell token (which never goes to an app upstream).
CREDENTIAL_HEADER = 'X-Wizard-App-Credential'
CREDENTIAL_ENV = 'WIZARD_APP_CREDENTIAL'
_KEY = re.compile(r'[a-f0-9]{64}')
_INST = re.compile(r'inst-[a-f0-9]{32}')


def credentials_dir(home: Path) -> Path:
    return Path(home) / 'browser-app-credentials'


def credential_path(home: Path, inst: str) -> Path:
    if not _INST.fullmatch(inst or ''):
        raise ValueError('bad installation id')
    return credentials_dir(home) / f'{inst}.key'


def _ensure_key(home: Path, inst: str) -> str:
    path = credential_path(home, inst)
    try:
        key = path.read_text().strip()
        if _KEY.fullmatch(key):
            return key
    except FileNotFoundError:
        pass
    import secrets
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    key = secrets.token_hex(32)
    fd, tmp = tempfile.mkstemp(prefix='.key.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(key + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if path.read_text().strip() != key:
        raise OSError('listener credential readback mismatch')
    return key


def listener_credential(home: Path, runtipi_root: Path, store: str, app_id: str, inst: str) -> dict | None:
    """Reference (never the value) for apps whose installed compose takes WIZARD_APP_CREDENTIAL, else None."""
    installed = Path(runtipi_root) / 'apps' / store / app_id / 'docker-compose.yml'
    try:
        if CREDENTIAL_ENV not in installed.read_text():
            return None
    except OSError:
        return None
    ref = {'header': CREDENTIAL_HEADER, 'ref': f'browser-app-credentials/{inst}.key', 'env': CREDENTIAL_ENV}
    try:
        key = _ensure_key(home, inst)
        env = Path(runtipi_root) / 'user-config' / store / app_id / 'app.env'
        want = {'WIZARD_BROWSER_BOUNDARY': '1', CREDENTIAL_ENV: key}
        have: dict[str, str] = {}
        for line in env.read_text().splitlines() if env.is_file() else []:
            k, _, v = line.partition('=')
            have[k.strip()] = v
        if any(have.get(k) != v for k, v in want.items()):
            from .cli import write_user_env
            write_user_env(Path(runtipi_root), app_id, want)
        # The running container picks the variable up on its next (re)start; until then its gate fails closed.
        return {**ref, 'state': 'written'}
    except (OSError, ValueError) as e:
        return {**ref, 'state': 'unavailable', 'error': type(e).__name__}


def prune_listener_credentials(home: Path, live: set) -> list[str]:
    """Remove credentials of installations that no longer exist (uninstall, reinstall); returns removed ids."""
    removed = []
    d = credentials_dir(home)
    for f in sorted(d.glob('inst-*.key')) if d.is_dir() else []:
        if f.stem not in live:
            f.unlink(missing_ok=True)
            removed.append(f.stem)
    return removed


def _current(home: Path, app_id: str) -> dict:
    try:
        reg = json.loads((Path(home) / 'apps.json').read_text())
    except (OSError, ValueError):
        raise ValueError('apps.json unavailable; run `wam apps` first') from None
    found = [a for a in reg.get('apps', []) if a.get('id') == app_id]
    if len(found) != 1:
        raise ValueError(f'{app_id}: expected one installed app, found {len(found)}')
    b = found[0].get('browser') or {}
    if not b.get('installation_id') or not b.get('source_id') or not b.get('contract'):
        raise ValueError(f"{app_id}: not eligible for browser grants ({b.get('reason') or 'boundary projection off'})")
    return b


def grant(home: Path, app_id: str, operations: list[str], now: float | None = None) -> dict:
    """Owner grant bound to the CURRENT installation and source. Undeclared operations are refused."""
    b = _current(home, app_id)
    ops = sorted(set(operations))
    if not ops or any(o not in b['contract']['operations'] for o in ops):
        raise ValueError(f'{app_id}: grant only operations its installed browser_contract declares')
    doc = read_grants(home)
    keep = [g for g in doc['grants'] if not (g.get('app_id') == app_id and _match(g, b['installation_id'], b['source_id']))]
    old = [g for g in doc['grants'] if g not in keep]
    revision = max([int(g.get('revision') or 0) for g in doc['grants']] or [0]) + 1
    merged = sorted({o for g in old for o in g.get('operations') or []} | set(ops))
    entry = {'app_id': app_id, 'installation_id': b['installation_id'], 'source_id': b['source_id'],
             'operations': merged, 'revision': revision, 'granted_at': int(now or time.time())}
    _write_grants(home, {'schema': SCHEMA, 'grants': keep + [entry]})
    return entry


def revoke(home: Path, app_id: str, operations: list[str] | None = None) -> dict:
    """Remove some or all operations from every grant recorded for this app id (any source)."""
    doc = read_grants(home)
    revision = max([int(g.get('revision') or 0) for g in doc['grants']] or [0]) + 1
    out, removed = [], []
    for g in doc['grants']:
        if g.get('app_id') != app_id:
            out.append(g)
            continue
        left = [o for o in g.get('operations') or [] if operations and o not in operations]
        removed += [o for o in g.get('operations') or [] if o not in left]
        if left:
            out.append({**g, 'operations': left, 'revision': revision})
        else:
            # Keep a zero-operation tombstone so the revision still moves for this installation.
            out.append({**g, 'operations': [], 'revision': revision})
    _write_grants(home, {'schema': SCHEMA, 'grants': out})
    return {'app_id': app_id, 'removed': sorted(set(removed)), 'revision': revision}
