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
        'network-state.json', 'linked.json', 'builder/python', 'bin/wam')]
    paths += [rt / 'app-data/wizard-os/apps.json']
    for base in (rt / 'apps', rt / 'repos'):
        pattern = '*/*' if base.name == 'apps' else '*/apps/*'
        for name in ('docker-compose.yml', 'docker-compose.generated.yml', '.wizard-source', 'config.json'):
            paths += list(base.glob(pattern + '/' + name))
    for name in ('docker-compose.yml', 'app.env'):
        paths += list((rt / 'user-config').glob('*/*/' + name))
    return sorted(set(paths))


def builder_links(state, old, new):
    """Move generated discovery links, not the immutable hashed bundle itself."""
    from .builder_bundle import COMPANION_SKILLS, MARKER
    base = state / 'builder'
    if not base.exists():
        return []
    if base.resolve() != base or (base / '.owner').is_symlink() or (base / '.owner').read_text().strip() != MARKER:
        raise ValueError('Unmanaged builder destination during migration')
    changed = []
    current = base / 'current'
    if current.is_symlink():
        target = replace(str(current.readlink()), old, new)
        resolved = (current.parent / target).resolve()
        if not resolved.is_relative_to(base / 'releases'):
            raise ValueError('Builder current escapes its protected releases')
        if target != str(current.readlink()):
            current.unlink(); current.symlink_to(target)
            changed.append(str(current))
    for name in ('wizard-builder', *COMPANION_SKILLS):
        link = state / 'system-skills' / name
        if link.is_symlink():
            target = replace(str(link.readlink()), old, new)
            if target != str(base / 'current/system-skills' / name):
                raise ValueError('Unmanaged builder discovery link during migration')
            if target != str(link.readlink()):
                link.unlink(); link.symlink_to(target)
                changed.append(str(link))
    # This host binding lives outside the content-addressed release. The new
    # service uses distro Python, not a venv in the old agent-writable checkout.
    interpreter = base / 'python'
    if interpreter.is_symlink():
        raise ValueError('Unsafe builder interpreter symlink')
    if interpreter.is_file():
        interpreter.write_text('/usr/bin/python3\n')
        changed.append(str(interpreter))
    return changed


def rewrite(rt, state, old, new):
    changed = builder_links(state, old, new)
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


def regenerate(rt, old=(), new=()):
    """Use the normal queue-backed renderer, retaining the full saved form.

    This handles runtime-owned app.env without a privileged payload scan or
    rewriting arbitrary application files. Runtipi regenerates Compose on start.
    """
    def relocate(value):
        if isinstance(value, str): return replace(value, old, new)
        if isinstance(value, list): return [relocate(v) for v in value]
        if isinstance(value, dict): return {k: relocate(v) for k, v in value.items()}
        return value
    refreshed = []
    for row in rt.installed():
        urn = (row.get('info') or {}).get('urn')
        if not urn:
            continue
        app = (rt.app(urn) or {}).get('app') or {}
        form = dict(app.get('config') or {})
        for key in ('exposed', 'exposedLocal', 'openPort', 'port', 'domain',
                    'localSubdomain', 'isVisibleOnGuestDashboard', 'enableAuth', 'maxBackups'):
            # DB records use null for unset optional settings, but the normal
            # update-config DTO accepts absence, not JSON null. Omission writes
            # those same columns back as null; retain false/zero explicitly.
            if key in form and form[key] is None:
                del form[key]
            if key not in form and app.get(key) is not None:
                form[key] = app[key]
        rt.req('PATCH', f'/app-lifecycle/{urn}/update-config', relocate(form), timeout=120)
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
