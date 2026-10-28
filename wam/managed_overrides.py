"""Disk-only recognition of this manager's wiring, never trust recipe comments.

The generators/registries are authoritative. A marker is a hint, not proof. Strip
only an exact regenerated fragment; all remaining executable settings are audited.
No Docker discovery, directory preparation or state writes are allowed here.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def manager_home(c):
    return Path(getattr(c, 'home', Path(c.runtipi_root).parent / '.wizard-os'))


def _json(path):
    try:
        doc = json.loads(path.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def agent_mounts(c, app_id, store):
    """Regenerate the exact home bind from a validated same-agent registry row.

    The portable package uses the legacy app-data home and the override selects
    this box's registered home. Both are manager-generated paths for this agent,
    not permission to mount arbitrary siblings or any agent's home.
    """
    if store != 'wizardapps' or not app_id.startswith('agent-'):
        return ()
    from . import slots
    root, home = Path(c.runtipi_root).resolve(), manager_home(c).resolve()
    roots = [home / 'agents', root / 'app-data/wizard-os/agents']
    providers = {'wizard-agent-service', *[v.get('app') for v in slots.load_defaults().values()
                                         if isinstance(v, dict) and v.get('app')]}
    roots += [root / 'app-data' / store / p / 'data/wizard-os/agents' for p in sorted(providers)]
    found = []
    for parent in roots:
        for meta_file in sorted(parent.glob('*/agent.json')):
            meta = _json(meta_file)
            agent_id = meta_file.parent.name
            from .host_policy import ID
            runtime = meta.get('runtime') or {}
            if not isinstance(runtime, dict) or not ID.fullmatch(agent_id):
                continue
            if (meta.get('schema') != 1 or meta.get('id') != agent_id or
                    runtime.get('kind', 'container') != 'container' or
                    runtime.get('app_id', f'agent-{agent_id}') != app_id):
                continue
            if meta_file.resolve() != meta_file or (meta_file.parent / 'home').resolve() != meta_file.parent / 'home':
                continue
            for source in (meta_file.parent / 'home', root / 'app-data/wizard-os/agents' / agent_id / 'home'):
                if source.resolve() == source:
                    found.append((app_id, str(source), '/opt/data', False))
    return tuple(found)


def normalize(c, app_id, store, doc):
    from . import alongside, builder_bundle, claude_login
    from .host_policy import _plain
    from .network import load_yaml

    if store != 'wizardapps':
        return doc
    home, root = manager_home(c), Path(c.runtipi_root)
    st = alongside.load(home)
    if st:
        # Only roles actually handled by write_shell_override, not an app-id
        # allowlist for arbitrary host-networked owner/local recipes.
        role = {alongside.LEGACY_BUNDLE: 'bundle', 'wizard-ux': 'shell',
                'wizard-agent-service': 'service'}.get(app_id)
        if role:
            try:
                for rootless in (False, True):
                    expected = _plain(load_yaml(alongside.shell_override(st, app_id, home, role,
                        None if role == 'shell' else alongside.host_media_root(root),
                        alongside.transcriber_origin(root, store), rootless, prepare=False)))
                    if expected == _plain(doc):
                        return None
            except (KeyError, ValueError, TypeError, OSError, SystemExit, yaml.YAMLError):
                pass  # malformed/stale state never exempts an override

    services = doc.get('services') or {}
    # fresh() adds a single builder projection to stock Hermes. A copied marker
    # without this box's installed manager bundle is insufficient provenance.
    manifest = _json(home / 'builder/current/bundle.json')
    if app_id == 'hermes-agent' and manifest.get('owner') == builder_bundle.MARKER and manifest.get('schema') == builder_bundle.SCHEMA:
        svc = services.get(app_id + '-engine') or {}
        mount = {'type': 'bind', 'source': str(home.resolve() / 'builder'),
                 'target': builder_bundle.RUNTIME, 'read_only': True}
        if (isinstance(svc, dict) and isinstance(svc.get('volumes'), list)
                and isinstance(svc.get('environment'), dict)
                and svc['environment'].get('WIZARD_BUILDER_ROOT') == builder_bundle.RUNTIME):
            svc['volumes'] = [v for v in svc['volumes'] if v != mount]

    # Derive the Claude share from current alongside state and host configuration,
    # never the untrusted folder in x-wizard-claude-login itself.
    mark = doc.get(claude_login.MARK)
    from .privsep import enabled
    if st and not enabled(home) and not getattr(c, 'migration_metadata_only', False) and isinstance(mark, dict) and claude_login.is_consumer(app_id, []):
        try:
            agent = st['agent']
            hh = Path(agent['hermes_home'])
            cfg = yaml.safe_load((hh / 'config.yaml').read_text()) or {}
            folder = claude_login.login_dir(hh, agent['uid'])
            engines = claude_login.engine_services(claude_login.recipe_compose(root, store, app_id), app_id)
            if claude_login.uses_claude_subscription(cfg):
                for rootless in (False, True):
                    user = claude_login.engine_user(agent['uid'], agent['gid'], rootless)
                    expected = {'folder': str(folder), 'user': user, 'services': engines}
                    if mark != expected:
                        continue
                    for engine in engines:
                        svc = services.get(engine) or {}
                        mount = f'{folder}:{claude_login.TARGET}'
                        if isinstance(svc, dict) and svc.get('user') == user and isinstance(svc.get('volumes'), list):
                            svc['volumes'] = [v for v in svc['volumes'] if v != mount]
        except (KeyError, ValueError, TypeError, OSError, yaml.YAMLError):
            pass
    return doc
