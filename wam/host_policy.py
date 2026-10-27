"""The single app-admission allowlist. Approval is an (app id, recipe SHA-256), never a CLI flag.

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
# Reviewed minimum capabilities: Files drops ALL then switches uid for filebrowser.
# A changed set or a different app is NOT covered by these exact service-specific entries.
REVIEWED_CAPS = {('files', 'files'): ['CHOWN', 'SETUID', 'SETGID']}
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
    return (Path(source).resolve().parent == expected.resolve()
            and bool(re.fullmatch(re.escape(app_id or '') + r'@[0-9a-f]{12}', Path(source).name)))


def risks(compose, app_id=None, store='wizardapps', **context):
    try:
        return _risks(compose, app_id, store, **context)
    except (TypeError, ValueError, AttributeError, RecursionError, yaml.YAMLError) as exc:
        return [f'compose shape is not allowlisted ({type(exc).__name__})']


def _risks(compose: dict | str, app_id: str | None = None, store='wizardapps', *,
          app_data: Path | None = None, files_root: Path | None = None,
          root: Path | None = None, base: Path | None = None, variables: dict | None = None) -> list[str]:
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
    out = network.network_escapes(compose, app_id, store)
    for key in compose:
        if key not in TOP_KEYS and not str(key).startswith('x-'):
            out.append(f'compose key {key} is not allowlisted')
    for name, spec in (compose.get('networks') or {}).items():
        if isinstance(spec, dict) and (spec.get('driver') not in (None, 'bridge') or spec.get('driver_opts')):
            out.append(f'network {name}: host/custom network driver settings')
    for kind in ('secrets', 'configs'):
        definitions = compose.get(kind) or {}
        if not isinstance(definitions, dict):
            out.append(f'{kind}: invalid definitions')
            continue
        for name, spec in definitions.items():
            if isinstance(spec, dict) and spec.get('file') is not None:
                out.append(f'{kind} {name}: reads a host file')
    for name, spec in (compose.get('volumes') or {}).items():
        if spec and (not isinstance(spec, dict) or (spec.get('driver_opts') or spec.get('driver') not in (None, 'local'))):
            out.append(f'volume {name}: external/custom host storage')
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
                out.append(f'{name}: {key} requires host access')
        if svc.get('cap_add') and not (svc['cap_add'] == REVIEWED_CAPS.get((app_id, name)) and svc.get('cap_drop') == ['ALL']):
            out.append(f'{name}: cap_add {svc["cap_add"]}')
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
                    out.append(f'{name}: build.{key} requires owner approval')
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
                out.append(f'{name}: mounts host path {source} outside app-data and owner files')
    xr = compose.get('x-runtipi') or {}
    for override in xr.get('overrides') or []:
        if isinstance(override, dict):
            out.extend(risks({'services': override.get('services') or {}}, app_id, store, app_data=app_data,
                             files_root=files_root, root=root, base=base, variables=variables))
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
    variables = context.get('variables') or {}
    # Bind runtime interpolation to consent too. An env-file/form edit cannot
    # reuse approval of a recipe whose mount now resolves to a different source.
    path_variables = {}
    for text in texts:
        try:
            from .network import load_yaml
            doc = _plain(load_yaml(text)) or {}
            for svc in (doc.get('services') or {}).values():
                sensitive = json.dumps({k: svc.get(k) for k in ('volumes', 'env_file', 'build', 'secrets', 'configs')})
                for key in re.findall(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)', sensitive):
                    if key in variables:
                        path_variables[key] = variables[key]
        except (TypeError, AttributeError, ValueError, RecursionError, yaml.YAMLError):
            pass  # the actual shape validator refuses malformed input below
    sha = digest(texts + ([json.dumps(path_variables, sort_keys=True)] if path_variables else []))
    found = list(dict.fromkeys(r for text in texts for r in risks(text, app_id, **context)))
    with LOCK:
        state = _read(home)
        approved = state.get('approvals', {}).get(app_id)
        if approved and approved != sha:
            state['approvals'].pop(app_id, None)
            _write(home, state)
        if not found or approved == sha:
            return sha
        state.setdefault('pending', {})[app_id] = {'sha256': sha, 'risks': found, 'paths': [str(p) for p in paths],
                                                        'path_hashes': [digest([p.read_text()]) for p in paths]}
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
        pending(home, app_id, sha)
        state = _read(home)
        state.setdefault('approvals', {})[app_id] = sha
        _write(home, state)
    return {'app': app_id, 'sha256': sha, 'approved': True}


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
    for p in (data / 'app.env', root / 'user-config' / store / app_id / 'app.env'):
        if p.is_file():
            values.update(read_env(p))
    values.update(given or {})
    return {'root': host, 'app_data': data, 'files_root': host / 'media',
            'base': host / 'apps' / store / app_id, 'store': store, 'variables': values}


def normalized_override(c, app_id, store, text):
    from . import network
    doc = network.load_yaml(text) or {}
    mark = doc.get(network.MARK)
    if not isinstance(mark, dict):
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
