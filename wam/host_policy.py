"""The single app-admission allowlist. Approval covers an app's risk set, never a CLI flag.

This guards WAM lifecycle paths, not Docker from a process already in the docker group.
Slice 3 moves agents off that uid/group and makes the service-owned registry a boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import yaml

PREFIX = '/wizard-os-home/store-install/_approve'
EXIT_APPROVAL = 23
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
SHA = re.compile(r'[0-9a-f]{64}')
LOCK = threading.RLock()
STATE = 'approval-registry.json'
# Deliberately finite: a new Compose feature is reviewed before admission. Extension
# metadata is inert, except x-runtipi.overrides, whose executable services are checked too.
TOP_KEYS = {'name', 'version', 'services', 'volumes', 'networks', 'secrets', 'configs'}
SERVICE_KEYS = set('image build command entrypoint user working_dir hostname domainname restart depends_on '
                   'environment ports expose volumes networks network_mode pid ipc userns_mode uts privileged '
                   'devices device_cgroup_rules cap_add cap_drop security_opt sysctls env_file secrets configs '
                   'read_only init tmpfs shm_size pids_limit mem_limit memswap_limit mem_reservation cpus '
                   'cpu_count cpu_percent cpu_period cpu_quota cpu_shares cpuset healthcheck labels logging '
                   'stop_grace_period stop_signal stdin_open tty ulimits deploy platform pull_policy profiles '
                   'group_add dns dns_search dns_opt extra_hosts isolation runtime cgroup cgroup_parent '
                   'oom_kill_disable oom_score_adj storage_opt'.split())
# Reducing privileges is safe for ALL apps, unlike arbitrary seccomp/apparmor options.
SAFE_SECURITY = {'no-new-privileges:true', 'no-new-privileges=true'}
# Adding these back after cap_drop gives no more power than a default Docker
# container. CAP_ prefixes and case are accepted by Docker as well.
DEFAULT_CAPS = set('AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD NET_BIND_SERVICE '
                   'NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_CHROOT'.split())
# Explicit app + exact mount exceptions, never a system-app bypass. Only read-only
# projections of manager/history metadata and recipe catalogs; no runtime sockets.
REVIEWED_MOUNTS = {
    ('wizard-agent-service', '${ROOT_FOLDER_HOST}/app-data/wizard-os/hermes-view:/hermes:ro'),
    ('wizard-agent-service', '${ROOT_FOLDER_HOST}/app-data/wizard-os:/wizard-os:ro'),
    ('wizard-connection-router', '${ROOT_FOLDER_HOST}/app-data/wizard-os:/run/wizard:ro'),
    ('wizard-connection-router', '${ROOT_FOLDER_HOST}/repos:/run/recipes:ro'),
}


def _plain(value):
    from .network import _Tagged
    if isinstance(value, _Tagged):
        return _plain(value.value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _expand(source: str, variables: dict) -> str:
    """Only manager-provided path variables, never os.environ or app form values.
    Unknown variables (even ones with arbitrary host defaults) fail closed."""
    pat = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^{}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)')
    for _ in range(12):
        def sub(m):
            key = m[1] or m[4]
            if key in variables:
                return str(variables[key])
            # Optional defaults are safe only when they resolve back under allowed roots.
            return m[3] if m[2] else m[0]
        new = pat.sub(sub, source)
        if new == source:
            break
        source = new
    return source


def _pinned_source(app_data, app_id, store, variables):
    source = (variables or {}).get('APP_SOURCE_DIR')
    if source is None:
        return True  # Runtipi derives this from its validated .wizard-commit.
    expected = app_data.parent.parent / '.wizard-source' / store
    path = Path(source)
    return (path.parent == expected and '..' not in path.parts and path.resolve() == path
            and bool(re.fullmatch(re.escape(app_id or '') + r'@[A-Za-z0-9][A-Za-z0-9._-]*', path.name)))


def _managed_source(source, root, app_data, app_id, store):
    """Read-only code projections, lexically AND physically inside this app's pins.

    Release directories also have version/owner suffixes, not just hex revisions.
    Never follow a symlink out of the manager's tree or accept a sibling app.
    """
    path = Path(source)
    if not path.is_absolute() or '..' in path.parts or '$' in source or '~' in source:
        return False
    prefixes = (root / 'repos' / store / 'apps' / (app_id or '_candidate') / 'releases',
                app_data.parent.parent / '.wizard-source' / store)
    for prefix in prefixes:
        try:
            rel = path.relative_to(prefix)
            first = rel.parts[0]
            valid = (bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', first)) if prefix.name == 'releases'
                     else bool(re.fullmatch(re.escape(app_id or '') + r'@[A-Za-z0-9][A-Za-z0-9._-]*', first)))
            # Compare against the lexical prefix: resolving the prefix itself would
            # accidentally bless a manager directory symlinked to host secrets.
            if valid and path.resolve().is_relative_to(prefix) and (prefix / first).resolve() == prefix / first:
                return True
        except (IndexError, ValueError, OSError, RuntimeError):
            pass
    return False


def risks(compose, app_id=None, store='wizardapps', **context):
    try:
        return _risks(compose, app_id, store, **context)
    except (TypeError, ValueError, AttributeError, RecursionError, yaml.YAMLError) as exc:
        return [f'compose shape is not allowlisted ({type(exc).__name__})']


def _risks(compose: dict | str, app_id: str | None = None, store='wizardapps', *,
          app_data: Path | None = None, files_root: Path | None = None,
          root: Path | None = None, base: Path | None = None, variables: dict | None = None,
          managed_mounts=()) -> list[str]:
    from . import network
    if isinstance(compose, str):
        try:
            compose = network.load_yaml(compose)
        except yaml.YAMLError:
            return ['compose does not parse']
    compose = _plain(compose)
    if not isinstance(compose, dict):
        return ['compose must be a mapping']
    root = Path(root or '/var/lib/runtipi').resolve()
    app_data = Path(app_data or root / 'app-data' / store / (app_id or '_candidate')).resolve()
    files_root = Path(files_root or root / 'media').resolve()
    base = Path(base or root / 'apps' / store / (app_id or '_candidate')).resolve()
    values = {'APP_DATA_DIR': str(app_data), 'ROOT_FOLDER_HOST': str(root), **(variables or {})}
    def expand_value(value):
        if isinstance(value, str):
            return _expand(value, values)
        if isinstance(value, list):
            return [expand_value(v) for v in value]
        if isinstance(value, dict):
            return {k: expand_value(v) for k, v in value.items()}
        return value
    network_doc = {'networks': expand_value(compose.get('networks') or {}), 'services': {
        n: {'network_mode': expand_value(s.get('network_mode'))} for n, s in (compose.get('services') or {}).items()
        if isinstance(s, dict)}}
    out = network.network_escapes(network_doc, app_id, store)
    for key in compose:
        if key not in TOP_KEYS and not str(key).startswith('x-'):
            out.append(f'compose key {key} is not allowlisted')
    for name, spec in (compose.get('networks') or {}).items():
        if isinstance(spec, dict) and (spec.get('driver') not in (None, 'bridge') or spec.get('driver_opts')):
            out.append(f'network {name}: custom host driver settings: {json.dumps(expand_value(spec), sort_keys=True)}')
    for kind in ('secrets', 'configs'):
        definitions = compose.get(kind) or {}
        if not isinstance(definitions, dict):
            out.append(f'{kind}: invalid definitions')
            continue
        for name, spec in definitions.items():
            if isinstance(spec, dict) and spec.get('file') is not None:
                out.append(f'{kind} {name}: reads a host file {expand_value(spec["file"])}')
    for name, spec in (compose.get('volumes') or {}).items():
        if spec and (not isinstance(spec, dict) or (spec.get('driver_opts') or spec.get('driver') not in (None, 'local'))):
            out.append(f'volume {name}: external/custom host storage: {json.dumps(expand_value(spec), sort_keys=True)}')
    services = compose.get('services') or {}
    if not isinstance(services, dict):
        return out + ['services must be a mapping']
    for name, svc in services.items():
        if not isinstance(svc, dict):
            out.append(f'{name}: service must be a mapping')
            continue
        for key in svc:
            if key not in SERVICE_KEYS and not str(key).startswith('x-'):
                out.append(f'{name}: compose key {key} is not allowlisted')
        if svc.get('privileged') not in (None, False):
            out.append(f'{name}: privileged (full control of the server)')
        for key in ('pid', 'ipc', 'userns_mode', 'uts', 'cgroup'):
            if svc.get(key) and str(svc[key]).lower() not in ('private', 'shareable'):
                out.append(f'{name}: {key}: {svc[key]}')
        for key in ('devices', 'device_cgroup_rules', 'sysctls', 'env_file', 'runtime', 'cgroup_parent', 'storage_opt'):
            if svc.get(key):
                out.append(f'{name}: {key} requires host access: {json.dumps(expand_value(svc[key]), sort_keys=True)}')
        caps = svc.get('cap_add') or []
        if not isinstance(caps, list):
            out.append(f'{name}: invalid cap_add')
        else:
            extra = sorted({str(cap).upper().removeprefix('CAP_') for cap in caps} - DEFAULT_CAPS)
            out.extend(f'{name}: cap_add {cap}' for cap in extra)
        opts = svc.get('security_opt') or []
        if not isinstance(opts, list) or any(x not in SAFE_SECURITY for x in opts):
            out.append(f'{name}: security_opt {opts}')
        reservations = ((svc.get('deploy') or {}).get('resources') or {}).get('reservations') or {}
        if reservations.get('devices'):
            out.append(f'{name}: deploy device access requires owner approval')
        build = svc.get('build')
        if build:
            source = build.get('context', '.') if isinstance(build, dict) else build
            source = _expand(str(source), values)
            # A build context is another host read, even without a bind mount.
            # The builder's staged source and recipe-local contexts are safe;
            # sending / or ../credentials to the daemon is not.
            if not re.match(r'^(?:https?://|git://|git@)', source):
                source_path = (Path(source) if Path(source).is_absolute() else base / source).resolve()
                pinned = str((build or {}).get('context', '')) == '${APP_SOURCE_DIR}' if isinstance(build, dict) else str(build) == '${APP_SOURCE_DIR}'
                pinned = pinned and _pinned_source(app_data, app_id, store, variables)
                if ('$' in source and not pinned) or (not pinned and not any(source_path.is_relative_to(p) for p in (base, app_data, files_root))):
                    out.append(f'{name}: build context reads host path {source}')
                if isinstance(build, dict) and build.get('dockerfile'):
                    file = _expand(str(build['dockerfile']), values)
                    dockerfile = (Path(file) if Path(file).is_absolute() else source_path / file).resolve()
                    if '$' in file or not dockerfile.is_relative_to(source_path):
                        out.append(f'{name}: build Dockerfile reads outside its context')
        if isinstance(build, dict):
            for key in build:
                if key not in {'context', 'dockerfile', 'dockerfile_inline', 'args', 'target', 'cache_from', 'cache_to',
                               'labels', 'no_cache', 'pull', 'platforms', 'shm_size'}:
                    out.append(f'{name}: build.{key} requires owner approval: {json.dumps(expand_value(build[key]), sort_keys=True)}')
            if build.get('network') == 'host':
                out.append(f'{name}: build host network')
        for kind in ('secrets', 'configs'):
            # All file-backed definitions are already checked above, including unused ones.
            for ref in svc.get(kind) or []:
                if isinstance(ref, dict) and ref.get('file') is not None:
                    out.append(f'{name}: {kind} reads a host file')
        for mount in svc.get('volumes') or []:
            if isinstance(mount, str):
                # Interpolation defaults can themselves contain ':': split AFTER expansion.
                expanded = _expand(mount, values)
                parts = expanded.split(':')
                if len(parts) == 1 and parts[0].startswith('/'):
                    continue  # anonymous container volume
                source = parts[0]
                bind = source.startswith(('/', '.', '~')) or '/' in source or '$' in source
                original = mount
            elif isinstance(mount, dict):
                source = _expand(str(mount.get('source', mount.get('src', ''))), values)
                typ = mount.get('type')
                if typ not in ('bind', 'volume', 'tmpfs'):
                    out.append(f'{name}: unknown mount type {typ}')
                bind = typ == 'bind' or source.startswith(('/', '.', '~')) or '/' in source or '$' in source
                original = None
            else:
                out.append(f'{name}: invalid mount')
                continue
            if any(x in source for x in ('docker.sock', 'containerd.sock', 'podman.sock', '/run/docker', '/run/containerd')):
                out.append(f'{name}: mounts the container runtime socket {source}')
                continue
            if not bind:
                continue  # app-private Compose named volume
            # APP_SOURCE_DIR is manager-created pinned code, mounted read-only only.
            if isinstance(mount, str) and re.fullmatch(r'\$\{APP_SOURCE_DIR\}(?:/[\w./-]+)?:/[^:]+:ro', mount) and not re.search(r'(?:^|/)\.\.(?:/|:|$)', mount):
                if _pinned_source(app_data, app_id, store, variables):
                    continue
            read_only = (len(parts) == 3 and parts[2] == 'ro') if isinstance(mount, str) else mount.get('read_only') is True
            target = (parts[1] if len(parts) >= 2 else '') if isinstance(mount, str) else mount.get('target', mount.get('dst', ''))
            if read_only and _managed_source(source, root, app_data, app_id, store):
                continue
            # Derived from this box's validated agent registry, not a recipe marker.
            if (name, source, target, read_only) in managed_mounts:
                continue
            if original and (app_id, original) in REVIEWED_MOUNTS and expanded == _expand(
                    original, {'ROOT_FOLDER_HOST': str(root), 'APP_DATA_DIR': str(app_data)}):
                continue
            try:
                path = Path(source)
                path = (path if path.is_absolute() else base / path).resolve()
                safe = '$' not in source and '~' not in source and any(path.is_relative_to(p) for p in (app_data, files_root))
            except (OSError, ValueError, RuntimeError):
                safe = False
            if not safe:
                out.append(f'{name}: mounts host path {source} at {target} ({"read-only" if read_only else "writable"}) outside app-data and owner files')
    xr = compose.get('x-runtipi') or {}
    for override in xr.get('overrides') or []:
        if isinstance(override, dict):
            out.extend(risks({'services': override.get('services') or {}}, app_id, store, app_data=app_data,
                             files_root=files_root, root=root, base=base, variables=variables, managed_mounts=managed_mounts))
    return list(dict.fromkeys(out))


def _read(home: Path) -> dict:
    try:
        return json.loads((Path(home) / STATE).read_text())
    except FileNotFoundError:
        return {'schema': 1, 'approvals': {}, 'pending': {}}


def _write(home: Path, state: dict):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / (STATE + '.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(home / STATE)


def digest(texts: list[str]) -> str:
    if len(texts) == 1:
        return hashlib.sha256(texts[0].encode()).hexdigest()
    return hashlib.sha256(json.dumps(texts, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


class NeedsOwnerApproval(RuntimeError):
    def __init__(self, app_id, sha, found):
        self.payload = {'error': 'needs_owner_approval', 'app': app_id, 'risks': found,
                        'approve_url': f'{PREFIX}/{app_id}/{sha}',
                        'command': f'wam approve {app_id} {sha}'}
        super().__init__(json.dumps(self.payload))


def require(home: Path, app_id: str, texts: list[str], *, paths=(), **context) -> str:
    if not ID.fullmatch(app_id):
        raise ValueError('invalid app id')
    # The token still identifies the exact pending review (and stale pages fail),
    # but consent covers resolved risks, not image tags, comments or recipe bytes.
    found = sorted({r for text in texts for r in risks(text, app_id, **context)})
    key = approval_key(app_id, context.get('store', 'wizardapps'))
    sha = risk_digest(app_id, found, context.get('store', 'wizardapps'))
    with LOCK:
        state = _read(home)
        if not found or covers(state.get('approvals', {}).get(key), found):
            if state.get('pending', {}).pop(app_id, None):
                _write(home, state)
            return sha
        state.setdefault('pending', {})[app_id] = {'sha256': sha, 'risks': found, 'key': key,
            'paths': [str(p) for p in paths], 'path_hashes': [digest([p.read_text()]) for p in paths]}
        _write(home, state)
    raise NeedsOwnerApproval(app_id, sha, found)


def pending(home, app_id, sha):
    with LOCK:
        record = _read(home).get('pending', {}).get(app_id)
        if not ID.fullmatch(app_id) or not SHA.fullmatch(sha) or not record or record['sha256'] != sha:
            raise ValueError('no matching pending recipe; retry the refused operation')
        paths = record.get('paths') or []
        if paths and [digest([Path(p).read_text()]) for p in paths] != record.get('path_hashes'):
            raise ValueError('recipe changed; retry the refused operation for a fresh approval')
        return record


def approve(home: Path, app_id: str, sha: str) -> dict:
    """Only called by the root-peer owner route or the local administrator command."""
    with LOCK:
        record = pending(home, app_id, sha)
        state = _read(home)
        state.setdefault('approvals', {})[record.get('key', app_id)] = {
            'risks': sorted(set(record['risks'])), 'source': 'owner approved', 'timestamp': timestamp()}
        state.get('pending', {}).pop(app_id, None)
        _write(home, state)
    return {'app': app_id, 'sha256': sha, 'approved': True}


def timestamp():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def approval_key(app_id, store='wizardapps'):
    return app_id if store == 'wizardapps' else f'{app_id}:{store}'


def risk_digest(app_id, found, store='wizardapps'):
    return digest([approval_key(app_id, store), json.dumps(sorted(set(found)), separators=(',', ':'))])


def covers(record, found):
    # A legacy SHA/string, malformed record or arbitrary marker is not consent.
    return (isinstance(record, dict) and record.get('source') in ('owner approved', 'grandfathered at upgrade')
            and isinstance(record.get('timestamp'), str) and isinstance(record.get('risks'), list)
            and all(isinstance(r, str) for r in record['risks']) and set(found) <= set(record['risks']))


def installed_risks(c):
    """Disk-only installed inventory; no Config, API, Docker, approvals or writes.

    Installed packages under apps/ are the snapshot, NEVER the newer repos/ copy.
    Unreadable packages abort migration rather than seal an incomplete snapshot.
    """
    root = Path(c.runtipi_root)
    rows = []
    for recipe in sorted((root / 'apps').glob('*/*/docker-compose.yml')):
        app_id, store = recipe.parent.name, recipe.parent.parent.name
        if not ID.fullmatch(app_id) or not ID.fullmatch(store):
            raise ValueError('invalid installed app/store id')
        ctx = context(c, app_id, store)
        recipe_risks = risks(recipe.read_text(), app_id, **ctx)
        override = root / 'user-config' / store / app_id / 'docker-compose.yml'
        override_risks = []
        if override.is_file():
            doc = normalized_override(c, app_id, store, override.read_text(), offline=True)
            override_risks = risks(doc, app_id, **ctx) if doc else []
        rows.append({'app': app_id, 'store': store, 'risks': sorted(set(recipe_risks + override_risks)),
                     'recipe_risks': recipe_risks, 'override_risks': override_risks})
    return rows


def migration_plan(c):
    """The exact upgrade snapshot, also used by the read-only dry-run harness."""
    now = timestamp()
    return {approval_key(row['app'], row['store']): {
        'risks': row['risks'], 'source': 'grandfathered at upgrade', 'timestamp': now}
        for row in installed_risks(c)}


def migrate(c):
    """Once, before lifecycle mutation: preserve current installed risk sets.

    Config startup seals even an empty installation. A later install/restore cannot
    enroll itself by calling require_app. No installed source or service is changed.
    """
    with LOCK:
        state = _read(c.home)
        if state.get('risk_set_migration'):
            return
        plan = migration_plan(c)
        approvals = state.setdefault('approvals', {})
        for key, row in plan.items():
            # Keep explicit newer risk-set consent when migrating a partial rollout.
            if not covers(approvals.get(key), row['risks']):
                approvals[key] = row
        state['schema'] = 2
        state['risk_set_migration'] = timestamp()
        state['pending'] = {}  # pre-upgrade recipe SHA reviews are no longer valid
        _write(c.home, state)


def grandfathered_health(home):
    rows = []
    for app, row in sorted(_read(home).get('approvals', {}).items()):
        if covers(row, []) and row['source'] == 'grandfathered at upgrade' and row['risks']:
            categories = risk_categories(row['risks'])
            rows.append((f'{app}: existing host access', True,
                'Kept working during the upgrade: ' + ', '.join(categories) +
                '. This access was already in use; additional host access still needs your approval.'))
    return rows


def risk_categories(found):
    labels = set()
    for risk in found:
        if 'network_mode' in risk:
            labels.add('uses the server network')
        elif risk.startswith('network '):
            labels.add('joins a shared network')
        elif 'mounts host path' in risk:
            labels.add('reads or writes files outside its own storage')
        elif 'cap_add' in risk:
            labels.add('adds elevated container capabilities')
        elif 'runtime socket' in risk:
            labels.add('controls the container runtime')
        else:
            labels.add('uses additional host permissions')
    return sorted(labels)


def context(c, app_id, store='wizardapps', given=None, filesystem=None):
    from .runtipi import read_env
    path_type = filesystem.Path if filesystem else Path
    root = path_type(c.runtipi_root)
    env = read_env(root / '.env') if (root / '.env').is_file() else {}
    host = Path(env.get('ROOT_FOLDER_HOST') or root)
    # Runtipi supports a separate appDataPath. Read its manager-owned settings.
    settings_path = root / 'state' / 'settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    data = path_type(settings.get('appDataPath') or env.get('RUNTIPI_APP_DATA_PATH') or host) / 'app-data' / store / app_id
    values = dict(env)
    paths = [root / 'user-config' / store / app_id / 'app.env']
    if not getattr(c, 'migration_metadata_only', False):
        paths.insert(0, data / 'app.env')
    for p in paths:
        if p.is_file():
            values.update(read_env(p))
    values.update(given or {})
    from .managed_overrides import agent_mounts
    return {'root': host, 'app_data': data, 'files_root': host / 'media',
            'base': host / 'apps' / store / app_id, 'store': store, 'variables': values,
            'managed_mounts': () if getattr(c, 'migration_metadata_only', False) else agent_mounts(c, app_id, store)}


def normalized_override(c, app_id, store, text, *, offline=False):
    from . import network
    doc = network.load_yaml(text) or {}
    if not isinstance(doc, dict):
        return doc
    from .managed_overrides import normalize
    doc = normalize(c, app_id, store, doc)
    if doc is None:
        return None
    mark = doc.get(network.MARK)
    if offline or not isinstance(mark, dict):
        return doc
    # A forged x-wizard-networks marker is NOT authority. Only exclude a block
    # matching the topology independently derived from this box's current grants.
    try:
        plan, _ = network.build(c, extra={app_id})
        app = next(a for a in plan['apps'] if a['id'] == app_id)
        expected = network.overlay_for(app)
        actual_services = {n: _plain((doc.get('services') or {}).get(n, {}).get('networks'))
                           for n in mark.get('services') or []}
        actual_networks = {n: (doc.get('networks') or {}).get(n) for n in mark.get('networks') or []}
        if actual_services == expected['services'] and actual_networks == expected['networks']:
            return network.strip_overlay(doc)
    except (OSError, RuntimeError, ValueError, AttributeError, KeyError, StopIteration):
        pass  # unavailable planner means no exemption, not a trusted marker
    return doc


def require_app(c, app_id, store='wizardapps', where='any', filesystem=None, given=None):
    path_type = filesystem.Path if filesystem else Path
    root = path_type(c.runtipi_root)
    candidates = [root / 'repos' / store / 'apps' / app_id / 'docker-compose.yml',
                  root / 'apps' / store / app_id / 'docker-compose.yml']
    if where == 'installed':
        candidates.reverse()
    recipe = next((p for p in candidates if p.is_file()), None)
    if recipe is None:
        raise RuntimeError(f'{app_id}: no recipe available for safety check')
    paths = [recipe]
    override = root / 'user-config' / store / app_id / 'docker-compose.yml'
    if override.is_file():
        # Network topology written by WAM is not user-provided host access.
        from .network import dump_yaml
        doc = normalized_override(c, app_id, store, override.read_text())
        paths.append(override)
        texts = [recipe.read_text()] + ([dump_yaml(doc)] if doc else [])
        return require(c.home, app_id, texts, paths=paths, **context(c, app_id, store, given, filesystem))
    return require(c.home, app_id, [recipe.read_text()], paths=paths, **context(c, app_id, store, given, filesystem))
