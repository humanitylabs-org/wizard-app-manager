"""Mirror of App Manager-accepted host access for the backend's final Compose gate.

`packages/backend/src/modules/docker/wizard-host-guard.ts` refuses, immediately before Docker changes a
container, every host-reaching setting in Compose's merged/interpolated model that is not listed here for that
exact app. This module is the only writer. It lists an app's tokens only when the App Manager policy accepts
the app's current recipe and override: no risks at all (for example manager-generated mounts that
`managed_overrides.normalize` recognised exactly) or risks covered by an owner approval.

Tokens use the same rules as the TypeScript gate. Shared vectors: tests/fixtures/final-host-tokens.json.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import hashlib
import fcntl
import threading
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

FILE = 'wizard-host-approvals.json'
OURS = re.compile(r'(?:wizard-(?:app|link|gw)-[a-z0-9-]+|wizard-core)')
SAFE_SECURITY = {'no-new-privileges:true', 'no-new-privileges=true', 'no-new-privileges'}
DEFAULT_CAPS = set('AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD NET_BIND_SERVICE '
                   'NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_CHROOT'.split())
TOP_KEYS = {'name', 'version', 'services', 'volumes', 'networks', 'secrets', 'configs'}
SERVICE_KEYS = set((
    'image build command entrypoint user working_dir hostname domainname restart depends_on environment ports expose '
    'volumes networks network_mode pid ipc userns_mode uts privileged devices device_cgroup_rules cap_add cap_drop '
    'security_opt sysctls env_file secrets configs read_only init tmpfs shm_size pids_limit mem_limit memswap_limit '
    'mem_reservation cpus cpu_count cpu_percent cpu_period cpu_quota cpu_shares cpuset healthcheck labels logging '
    'stop_grace_period stop_signal stdin_open tty ulimits deploy platform pull_policy profiles group_add dns dns_search '
    'dns_opt extra_hosts isolation runtime cgroup cgroup_parent oom_kill_disable oom_score_adj storage_opt '
    'container_name scale attach annotations mac_address post_start pre_stop').split())
FLAGGED = ('devices', 'device_cgroup_rules', 'sysctls', 'runtime', 'cgroup_parent', 'storage_opt', 'isolation')
BUILD_KEYS = {'context', 'dockerfile', 'dockerfile_inline', 'args', 'target', 'cache_from', 'cache_to', 'labels',
              'no_cache', 'pull', 'platforms', 'shm_size', 'tags'}
SOCKET = re.compile(r'docker\.sock|containerd\.sock|podman\.sock|/run/docker|/run/containerd')


def value_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def bind_identity(service, mount):
    return service + '|' + value_digest({'source': _clean(mount['source']), 'target': _clean(mount['target']),
                                        'read_only': mount['read_only']})


def unsupported_features(config):
    """Unreviewed features are hard refusals, never authority the writer can mint."""
    if not isinstance(config, dict):
        return ['unsupported:compose']
    out = []
    for key in config:
        if key not in TOP_KEYS and not str(key).startswith('x-'):
            out.append(f'unsupported:top:{key}')
    for kind in ('secrets', 'configs'):
        if config.get(kind): out.append(f'unsupported:{kind}')
    for name, v in (config.get('volumes') or {}).items():
        if v and (v.get('external') or v.get('driver') not in (None, 'local') or v.get('driver_opts')):
            out.append(f'unsupported:volume:{name}')
        for k in v or {}:
            if k not in ('name', 'driver', 'driver_opts', 'external', 'labels'):
                out.append(f'unsupported:volume:{name}:{k}')
    for name, v in (config.get('networks') or {}).items():
        if v and (v.get('driver') not in (None, 'bridge') or v.get('driver_opts')):
            out.append(f'unsupported:network:{name}')
        for k in v or {}:
            if k not in ('name', 'driver', 'driver_opts', 'external', 'labels', 'attachable', 'internal', 'enable_ipv6', 'enable_ipv4', 'ipam'):
                out.append(f'unsupported:network:{name}:{k}')
        ipam = (v or {}).get('ipam') or {}
        if ipam.get('driver') not in (None, 'default') or ipam.get('options'):
            out.append(f'unsupported:network:{name}:ipam')
    for svc, s in (config.get('services') or {}).items():
        if not isinstance(s, dict):
            out.append(f'unsupported:{svc}:invalid'); continue
        for k in s:
            if k not in SERVICE_KEYS and not k.startswith('x-'):
                out.append(f'unsupported:{svc}:{k}')
        for k in ('build', 'post_start', 'pre_stop', 'secrets', 'configs', 'env_file', 'develop', 'annotations'):
            if s.get(k): out.append(f'unsupported:{svc}:{k}')
        logging = s.get('logging') or {}
        if logging.get('driver') not in (None, 'json-file', 'local', 'none') or any(k not in ('max-size', 'max-file', 'compress', 'labels', 'labels-regex', 'env', 'env-regex', 'mode', 'max-buffer-size') for k in logging.get('options') or {}):
            out.append(f'unsupported:{svc}:logging')
        deploy = s.get('deploy') or {}
        if (((deploy.get('resources') or {}).get('reservations') or {}).get('devices')):
            out.append(f'unsupported:{svc}:deploy_devices')
        if any(k not in ('replicas', 'resources', 'restart_policy', 'labels') for k in deploy):
            out.append(f'unsupported:{svc}:deploy')
        for raw in s.get('volumes') or []:
            if not isinstance(raw, dict): continue
            if raw.get('type') not in (None, 'bind', 'volume', 'tmpfs'): out.append(f'unsupported:{svc}:mount_type')
            for k in raw:
                if k not in ('type', 'source', 'target', 'read_only', 'bind', 'volume', 'tmpfs', 'consistency'):
                    out.append(f'unsupported:{svc}:mount:{k}')
            opts = raw.get('bind') or {}
            if any(k not in ('create_host_path', 'propagation') for k in opts) or opts.get('propagation') not in (None, 'rprivate'):
                out.append(f'unsupported:{svc}:bind_options')
            if any(k != 'nocopy' for k in raw.get('volume') or {}): out.append(f'unsupported:{svc}:volume_options')
            if any(k not in ('size', 'mode') for k in raw.get('tmpfs') or {}): out.append(f'unsupported:{svc}:tmpfs_options')
    return sorted(out)


_held = threading.local()
_threads = threading.RLock()


@contextmanager
def authority_lock(c):
    """Shared kernel lock with DockerService; always acquire before host_policy.LOCK."""
    file = path_for(c).parent / '.wizard-host-authority.lock'
    with _threads:
        held = getattr(_held, 'paths', set())
        if str(file) in held:
            yield
            return
        file.parent.mkdir(parents=True, exist_ok=True)
        _control_parent(c)
        fd = os.open(file, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held.paths = held | {str(file)}
            yield
        finally:
            _held.paths = held
            os.close(fd)


def _clean(p: str) -> str:
    out = posixpath.normpath(p) if p else '.'
    if p.startswith('//'):
        out = '/' + out.lstrip('/')
    return out.rstrip('/') or '/'


def _under(p, root):
    a, b = _clean(p), _clean(root)
    return '..' not in a.split('/') and (a == b or a.startswith(b + '/'))


def _truthy(v):
    return v is True or str(v).lower() == 'true'


def _nonempty(v):
    return bool(v) if not isinstance(v, (list, dict)) else len(v) > 0


def volume_of(v):
    if isinstance(v, str):
        parts = v.split(':')
        if len(parts) == 1:
            return {'type': 'volume', 'source': '', 'target': parts[0], 'read_only': False}
        source, target = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 else ''
        bind = source.startswith(('/', '.', '~')) or '/' in source or '$' in source
        return {'type': 'bind' if bind else 'volume', 'source': source, 'target': target,
                'read_only': 'ro' in mode.split(',')}
    if not isinstance(v, dict):
        return None
    return {'type': str(v.get('type') or 'volume'), 'source': str(v.get('source', v.get('src', ''))),
            'target': str(v.get('target', v.get('dst', ''))), 'read_only': v.get('read_only') is True}


def tokens(config, *, app_id, store, app_data_dir, files_root, source_root, env_dirs=(),
           app_data_root=None, injected=frozenset(), shared_network='runtipi_tipi_main_network'):
    """Same rules as finalHostTokens() in wizard-host-guard.ts."""
    out = set(unsupported_features(config))
    if not isinstance(config, dict):
        return ['compose:invalid']
    for key in config:
        if key not in TOP_KEYS and not str(key).startswith('x-'):
            out.add(f'top:{key}')
    for name, raw in (config.get('networks') or {}).items():
        spec = raw if isinstance(raw, dict) else {}
        docker_name = str(spec.get('name') or name)
        if spec.get('external'):
            if not OURS.fullmatch(docker_name) and docker_name != shared_network:
                out.add(f'network:external:{docker_name}')
        elif (spec.get('driver') and spec['driver'] != 'bridge') or _nonempty(spec.get('driver_opts')):
            out.add(f'network:driver:{name}')
    for name, raw in (config.get('volumes') or {}).items():
        spec = raw if isinstance(raw, dict) else {}
        if spec.get('external'):
            out.add(f'volume:external:{spec.get("name") or name}')
        elif (spec.get('driver') and spec['driver'] != 'local') or _nonempty(spec.get('driver_opts')):
            out.add(f'volume:driver:{name}')
    for kind in ('secrets', 'configs'):
        for name, raw in (config.get(kind) or {}).items():
            if isinstance(raw, dict) and raw.get('file') is not None:
                out.add(f'{kind}:file:{name}:{_clean(str(raw["file"]))}')
    services = config.get('services') or {}
    if not isinstance(services, dict):
        return sorted(out | {'services:invalid'})
    src_prefix = _clean(source_root) + '/' + app_id + '@'
    for svc, s in services.items():
        if not isinstance(s, dict):
            out.add(f'{svc}|invalid')
            continue
        for key in s:
            if key not in SERVICE_KEYS and not str(key).startswith('x-'):
                out.add(f'{svc}|key:{key}')
        if _truthy(s.get('privileged')):
            out.add(f'{svc}|privileged')
        nm = '' if s.get('network_mode') is None else str(s['network_mode'])
        if nm and nm != 'none' and not nm.startswith('service:'):
            out.add(f'{svc}|network_mode:{nm}')
        for key in ('pid', 'ipc', 'userns_mode', 'uts', 'cgroup'):
            v = '' if s.get(key) is None else str(s[key]).lower()
            if v and v not in ('private', 'shareable') and not v.startswith('service:'):
                out.add(f'{svc}|{key}:{v}')
        for key in FLAGGED:
            if _nonempty(s.get(key)):
                out.add(f'{svc}|{key}:{value_digest(s[key])}')
        caps = s.get('cap_add')
        for cap in caps if isinstance(caps, list) else (['invalid'] if caps else []):
            c = str(cap).upper().removeprefix('CAP_')
            if c not in DEFAULT_CAPS:
                out.add(f'{svc}|cap:{c}')
        opts = s.get('security_opt')
        for opt in opts if isinstance(opts, list) else (['invalid'] if opts else []):
            if str(opt) not in SAFE_SECURITY:
                out.add(f'{svc}|security_opt:{opt}')
        if _nonempty((((s.get('deploy') or {}).get('resources') or {}).get('reservations') or {}).get('devices')):
            out.add(f'{svc}|deploy_devices')
        env_files = s.get('env_file')
        for f in env_files if isinstance(env_files, list) else ([env_files] if env_files else []):
            file = _clean(str(f.get('path', '') if isinstance(f, dict) else f))
            if not any(_under(file, d) for d in env_dirs):
                out.add(f'{svc}|env_file:{file}')
        if s.get('build'):
            b = s['build'] if isinstance(s['build'], dict) else {'context': s['build']}
            context = str(b.get('context') or '.')
            remote = re.match(r'^(?:https?://|git://|git@)', context)
            pinned = context.startswith(src_prefix) and '..' not in context.split('/')
            if not remote and not pinned and not _under(context, app_data_dir):
                out.add(f'{svc}|build:context:{_clean(context)}')
            for key in b:
                if key not in BUILD_KEYS:
                    out.add(f'{svc}|build:{key}')
            if b.get('network') and b['network'] not in ('default', 'none'):
                out.add(f'{svc}|build:network:{b["network"]}')
        vols = s.get('volumes')
        for raw in vols if isinstance(vols, list) else (['invalid'] if vols else []):
            v = volume_of(raw)
            if not v:
                out.add(f'{svc}|volume:invalid')
                continue
            if SOCKET.search(v['source']):
                out.add(f'{svc}|socket:{_clean(v["source"])}:{_clean(v["target"])}:{"ro" if v["read_only"] else "rw"}')
                continue
            if v['type'] in ('volume', 'tmpfs'):
                continue
            if v['type'] != 'bind':
                out.add(f'{svc}|mount:{v["type"]}')
                continue
            source, mode = _clean(v['source']), 'ro' if v['read_only'] else 'rw'
            if not source.startswith('/') or '$' in source:
                out.add(f'{svc}|bind:{source}:{v["target"]}:{mode}')
                continue
            if _under(source, app_data_dir) or _under(source, files_root):
                continue
            if v['read_only'] and source.startswith(src_prefix) and '..' not in source.split('/'):
                continue
            if app_data_root and _under(source, app_data_root) and bind_identity(svc, v) in injected:
                continue
            out.add(f'{svc}|bind:{source}:{v["target"]}:{mode}')
    return sorted(out)


def _expanded(doc, values):
    from .host_policy import _expand, _plain
    doc = _plain(doc)

    def walk(v):
        if isinstance(v, str):
            return _expand(v, values)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(doc)


def _merge(base: dict, extra: dict) -> dict:
    """Union view for acceptance only: every service key from either file is listed."""
    out = json.loads(json.dumps(base))
    for top, value in (extra or {}).items():
        if top == 'services' and isinstance(value, dict):
            services = out.setdefault('services', {})
            for name, svc in value.items():
                cur = services.setdefault(name, {})
                if isinstance(svc, dict) and isinstance(cur, dict):
                    for k, v in svc.items():
                        if isinstance(v, list) and isinstance(cur.get(k), list):
                            cur[k] = cur[k] + v
                        else:
                            cur[k] = v
        elif isinstance(value, dict) and isinstance(out.get(top), dict):
            out[top].update(value)
        else:
            out[top] = value
    return out


def app_tokens(c, app_id, store='wizardapps', where='installed', given=None):
    """Tokens of the recipe + RAW override this box would start, expanded like the policy check."""
    from . import host_policy, network
    root = Path(c.runtipi_root)
    candidates = [root / 'apps' / store / app_id / 'docker-compose.yml',
                  root / 'repos' / store / 'apps' / app_id / 'docker-compose.yml']
    if where != 'installed':
        candidates.reverse()
    recipe = next((p for p in candidates if p.is_file()), None)
    if recipe is None:
        return None
    ctx = host_policy.context(c, app_id, store, given)
    values = {'APP_DATA_DIR': str(ctx['app_data']), 'ROOT_FOLDER_HOST': str(ctx['root']), **ctx['variables']}
    doc = network.load_yaml(recipe.read_text()) or {}
    xr = doc.get('x-runtipi') if isinstance(doc, dict) else None
    merged = _expanded(doc, values) if isinstance(doc, dict) else doc
    for override in ((xr or {}).get('overrides') or []) if isinstance(xr, dict) else []:
        if isinstance(override, dict):
            merged = _merge(merged, {'services': _expanded(override.get('services') or {}, values)})
    user = root / 'user-config' / store / app_id / 'docker-compose.yml'
    if user.is_file():
        raw = network.load_yaml(user.read_text()) or {}
        if isinstance(raw, dict):
            merged = _merge(merged, _expanded(raw, values))
    if not isinstance(merged, dict):
        return ['unsupported:compose']
    services = merged.get('services') or {}
    if not isinstance(services, dict):
        return ['unsupported:services']
    for service in services.values():
        if not isinstance(service, dict):
            continue
        if isinstance(service.get('devices'), list):
            devices = []
            for item in service['devices']:
                if isinstance(item, str) and item.startswith('/'):
                    parts = item.split(':')
                    devices.append({'source': parts[0], 'target': parts[1] if len(parts) > 1 else parts[0],
                                    'permissions': parts[2] if len(parts) > 2 else 'rwm'})
                else:
                    devices.append(item)
            service['devices'] = devices
        if isinstance(service.get('sysctls'), list):
            service['sysctls'] = dict(item.split('=', 1) for item in service['sysctls'])
        if isinstance(service.get('sysctls'), dict):
            service['sysctls'] = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in service['sysctls'].items()}
    app_data_root = Path(ctx['app_data']).parent.parent
    return tokens(merged, app_id=app_id, store=store, app_data_dir=str(ctx['app_data']),
                  files_root=str(ctx['files_root']), source_root=str(app_data_root / '.wizard-source' / store),
                  app_data_root=str(app_data_root))


def accepted(c, app_id, store='wizardapps'):
    """The app's tokens if current policy accepts it with no new owner decision; otherwise None."""
    from . import host_policy
    try:
        host_policy._require_app(c, app_id, store, where='installed')
    except host_policy.NeedsOwnerApproval:
        return None
    except (RuntimeError, OSError, ValueError, SystemExit):
        return None
    return app_tokens(c, app_id, store)


def path_for(c) -> Path:
    return Path(c.runtipi_root) / 'user-config' / FILE


def _control_parent(c):
    p = path_for(c).parent
    st = p.lstat()
    manager = Path(c.runtipi_root).stat().st_uid
    if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022 or st.st_uid not in (0, manager):
        raise ValueError('unsafe host authority control directory')


def _read(c):
    try:
        _control_parent(c)
        fd = os.open(path_for(c), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022:
                return None
            doc = json.load(f)
        return doc if isinstance(doc, dict) and doc.get('schema') == 1 and isinstance(doc.get('apps'), dict) else None
    except (OSError, ValueError):
        return None


def write(c, apps: dict) -> bool:
    with authority_lock(c):
        return _write(c, apps)


def _write(c, apps: dict) -> bool:
    body = json.dumps({'schema': 1, 'apps': {k: sorted(set(v)) for k, v in sorted(apps.items())}}, indent=2) + '\n'
    p = path_for(c)
    if _read(c) == json.loads(body):
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    _control_parent(c)
    fd, name = tempfile.mkstemp(prefix=f'.{FILE}.', dir=p.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(body)
            f.flush()
            os.fchmod(f.fileno(), 0o644)  # backend reads it; no secrets
            os.fsync(f.fileno())
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def record(c, app_id, store='wizardapps', found=None):
    with authority_lock(c):
        return _record(c, app_id, store, found)


def _record(c, app_id, store='wizardapps', found=None):
    """Never retain stale authority on refusal or mint approvals for unsupported features."""
    found = app_tokens(c, app_id, store) if found is None else found
    if found is None or any(t.startswith('unsupported:') for t in found):
        found = []
    apps = (_read(c) or {'apps': {}})['apps']
    key = f'{store}/{app_id}'
    if found:
        apps[key] = found
    else:
        apps.pop(key, None)
    return write(c, apps)


def sync(c):
    with authority_lock(c):
        return _sync(c)


def _sync(c):
    """Recompute every installed app. A refused app keeps no stale acceptance."""
    root = Path(c.runtipi_root)
    apps = {}
    for recipe in sorted((root / 'apps').glob('*/*/docker-compose.yml')):
        app_id, store = recipe.parent.name, recipe.parent.parent.name
        found = accepted(c, app_id, store)
        if found and not any(t.startswith('unsupported:') for t in found):
            apps[f'{store}/{app_id}'] = found
    return write(c, apps)
