"""Update-only authority adapter for the installed pre-privsep host.

REUSE: pure allowlist from host_policy/network at 12b2a868; no approval migration,
network writes, agent privilege routes or new owner listener. This host has no
recipe approval authority: risky recipes are refused, never auto-approved.
"""
from pathlib import Path
import json
import re
import yaml
SHARED = "tipi_main_network"
OURS = re.compile(r"(?:wizard-(?:app|link|gw)-[a-z0-9-]+|wizard-core)")
def ours(name: str) -> bool:
    """A network wam manages. Compose project networks of apps whose id starts with `wizard-` look similar
    (`wizard-claude_wizardapps_wizard-claude_wizardapps_network`) but always contain an underscore."""
    return bool(OURS.fullmatch(name))


class _Tagged:
    def __init__(self, tag: str, value):
        self.tag, self.value = tag, value

    def __eq__(self, other):
        return isinstance(other, _Tagged) and (self.tag, self.value) == (other.tag, other.value)


class _Loader(yaml.SafeLoader):
    pass


def _tagged(loader, tag, node):
    make = {yaml.ScalarNode: loader.construct_scalar, yaml.SequenceNode: loader.construct_sequence,
            yaml.MappingNode: loader.construct_mapping}[type(node)]
    return _Tagged(tag, make(node, deep=True) if not isinstance(node, yaml.ScalarNode) else make(node))


def load_yaml(text: str):
    return yaml.load(text, Loader=_Loader)


def network_escapes(compose: dict, app_id: str | None = None, store: str = "wizardapps") -> list[str]:
    """Networks in a recipe that are not the app's own: a pre-existing (`external`) network, a fixed Docker `name`
    outside the app's compose project (another app's network, `runtipi_tipi_main_network`, a reserved Wizard network),
    a non-bridge driver, or a service on Docker's default bridge or in another container's namespace. Any of them
    would let the app reach apps it never declared, so it is refused unless the owner allows host access.
    Same rule as the App Manager's start-time gate (packages/backend/src/modules/docker/wizard-networks.ts)."""
    out = []
    own = f"{app_id}_{store}_" if app_id else None
    top = (compose.get("networks") or {}) if isinstance(compose, dict) else {}
    for key, d in (top.items() if isinstance(top, dict) else []):
        if str(key) == SHARED:   # Runtipi's own entry: the builder rewrites it, and wam strips it from isolated apps
            continue
        d = _plain(d) or {}
        d = d if isinstance(d, dict) else {}
        ext = _plain(d.get("external"))
        name = str(_plain(d.get("name")) or (ext.get("name") if isinstance(ext, dict) else "") or "")
        driver = str(_plain(d.get("driver")) or "")
        if ours(name) or ours(str(key)):
            out.append(f"network {key}: attaches to the reserved Wizard network {name or key} (reaches other apps)")
        elif ext:
            out.append(f"network {key}: attaches to the existing network {name or key}, which this app does not own "
                       "(reaches other apps)")
        elif name and not (own and name.startswith(own)):
            out.append(f"network {key}: uses the fixed Docker name {name}, outside this app (another app or Runtipi "
                       "can share it)")
        if driver and driver != "bridge":
            out.append(f"network {key}: driver {driver} (not an app-private bridge)")
    for name, s in ((compose.get("services") or {}) if isinstance(compose, dict) else {}).items():
        s = _plain(s)
        if not isinstance(s, dict):
            continue
        mode = str(_plain(s.get("network_mode")) or "").lower()
        if "$" in mode:
            out.append(f"{name}: network_mode {mode} is set at runtime (unknown network access)")
        if mode == "host":
            out.append(f"{name}: network_mode: host (shares the server's network, bypasses app isolation)")
        elif mode.startswith("container:"):   # another container's namespace (e.g. runtipi's); service: stays allowed
            out.append(f"{name}: network_mode: {mode} (shares another container's network, bypasses app isolation)")
        elif mode == "bridge" or mode == "default":
            out.append(f"{name}: network_mode: {mode} (Docker's default bridge, shared with other containers)")
    return out


_Loader.add_multi_constructor("!", lambda loader, tag, node: _tagged(loader, "!" + tag, node))

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
    except (TypeError, ValueError, AttributeError, RecursionError, OSError, RuntimeError, KeyError, yaml.YAMLError) as exc:
        return [f'compose shape is not allowlisted ({type(exc).__name__})']


def _risks(compose: dict | str, app_id: str | None = None, store='wizardapps', *,
          app_data: Path | None = None, files_root: Path | None = None,
          root: Path | None = None, base: Path | None = None, variables: dict | None = None,
          managed_mounts=()) -> list[str]:
    if isinstance(compose, str):
        try:
            compose = load_yaml(compose)
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
    out = network_escapes(network_doc, app_id, store)
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


def require_update(c, app, store="wizardapps"):
    """No writes or implicit approval. Check prepared recipe plus every local override.

    Runtipi's final host guard remains the last authority over the freshly fetched
    recipe. Missing prepared bytes are unknown, not permission to queue.
    """
    from . import packaging
    root = Path(c.runtipi_root)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", app) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", store):
        raise SystemExit("REFUSED: invalid app or store identifier")
    recipe = root / "repos" / store / "apps" / app / "docker-compose.yml"
    if not recipe.is_file():
        raise SystemExit(f"REFUSED: {app}: no prepared recipe for safety check; nothing queued")
    data = packaging.app_data_root(root) / store / app
    directories = [recipe.parent, root / "apps" / store / app, root / "user-config" / store / app]
    if any((p / "docker-compose.json").exists() for p in directories):
        raise SystemExit(f"REFUSED: {app}: owner review required: dynamic recipe not supported by this host adapter; nothing queued")
    paths = [recipe, root / "apps" / store / app / "docker-compose.yml",
             root / "user-config" / store / app / "docker-compose.yml"]
    try:
        found = []
        for p in paths:
            if p.is_file():
                found.extend(risks(p.read_text(), app, store, root=root, app_data=data,
                                   files_root=root / "media", base=p.parent))
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise SystemExit(f"REFUSED: {app}: recipe check unavailable ({type(exc).__name__}); nothing queued") from None
    if found:
        raise SystemExit(f"REFUSED: {app}: owner review required: {'; '.join(found)[:400]}; nothing queued")
