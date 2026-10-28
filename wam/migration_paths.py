"""Finite management-path relocation; application/agent payloads stay opaque.

Never search the entire copied tree for strings: that would open private data
and corrupt documents containing an old path. Source pins here mean the manager's
Compose and .wizard-source descriptors, not files in the pinned source checkout.
"""
from pathlib import Path
import json
import re


def replace(text, old, new):
    for a, b in sorted(zip(old, new), key=lambda pair: -len(str(pair[0]))):
        # A sibling such as runtipi-extra is not the relocated directory.
        text = re.sub(re.escape(str(a)) + r'(?=$|[/\s\"\'`:},\]])', lambda _: str(b), text)
    return text


def metadata(rt, state):
    paths = [rt / name for name in ('.env', 'state/settings.json', 'docker-compose.yml',
                                   'user-config/tipi-compose.yml')]
    paths += [state / name for name in ('alongside.json', 'backup.json', 'backup-state.json',
        'app-manager.json', 'apps.json', 'app-sources.json', 'approval-registry.json',
        'network-state.json', 'linked.json', 'builder/current/bundle.json')]
    paths += [rt / 'app-data/wizard-os/apps.json']
    for base in (rt / 'apps', rt / 'repos'):
        pattern = '*/*' if base.name == 'apps' else '*/apps/*'
        for name in ('docker-compose.yml', 'docker-compose.generated.yml', '.wizard-source', 'config.json'):
            paths += list(base.glob(pattern + '/' + name))
    for name in ('docker-compose.yml', 'app.env'):
        paths += list((rt / 'user-config').glob('*/*/' + name))
    return sorted(set(paths))


def rewrite(rt, state, old, new):
    changed = []
    for p in metadata(rt, state):
        if p.resolve() != p:
            raise ValueError('Unsafe manager metadata symlink: ' + str(p))
        if not p.is_file():
            continue
        text = p.read_text()
        relocated = replace(text, old, new)
        if p == state / 'approval-registry.json':
            # Pending reviews bind hashes of pre-relocation files. They are not
            # approvals and must not be silently retargeted to different bytes.
            registry = json.loads(relocated)
            if registry.get('pending'):
                registry['pending'] = {}
                relocated = json.dumps(registry, indent=2) + '\n'
        if relocated != text:
            p.write_text(relocated)
            changed.append(str(p.relative_to(rt.parent)))
    assert_clean(rt, state, old)
    return changed


def regenerate(rt):
    """Use the normal queue-backed renderer, retaining the full saved form.

    This handles runtime-owned app.env without a privileged payload scan or
    rewriting arbitrary application files. Runtipi regenerates Compose on start.
    """
    refreshed = []
    for row in rt.installed():
        urn = (row.get('info') or {}).get('urn')
        if not urn:
            continue
        app = (rt.app(urn) or {}).get('app') or {}
        form = dict(app.get('config') or {})
        for key in ('exposed', 'exposedLocal', 'openPort', 'port', 'domain',
                    'localSubdomain', 'isVisibleOnGuestDashboard', 'enableAuth', 'maxBackups'):
            if key in app and key not in form:
                form[key] = app[key]
        rt.req('PATCH', f'/app-lifecycle/{urn}/update-config', form, timeout=120)
        refreshed.append(urn)
    return refreshed


def assert_clean(rt, state, old):
    stale = []
    for p in metadata(rt, state):
        if p.resolve() != p:
            raise ValueError('Unsafe manager metadata symlink: ' + str(p))
        if p.is_file() and replace(p.read_text(), old, ['__MOVED__'] * len(old)) != p.read_text():
            stale.append(str(p))
    if stale:
        raise ValueError('Legacy management paths remain: ' + ', '.join(stale))
