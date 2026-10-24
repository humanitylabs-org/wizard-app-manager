"""Admission for owner-installed, in-document Chats modules (trusted code, not a sandbox)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

CONTRACT = 'wizard.chats-view/1'
MAX_BYTES = 128 * 1024
BUILTIN = 'wizard-chats'
SEAL = '.wizard-chats-view.json'
BANNED = re.compile(r'\b(?:addEventListener|setTimeout|setInterval|requestAnimationFrame|fetch|XMLHttpRequest|__wos|__wux|localStorage|sessionStorage|innerHTML|outerHTML|history|eval|Function|WebSocket|import)\b|document\s*\.\s*cookie')


def validate(source: Path, manifest: dict) -> tuple[str, bytes] | None:
    view = manifest.get('chats_view')
    provides = manifest.get('provides') or []
    if view is None and 'chats' not in provides:
        return None
    if (not isinstance(provides, list) or not all(isinstance(p, str) for p in provides)
            or not isinstance(view, dict) or set(view) != {'contract', 'module'}
            or view.get('contract') != CONTRACT or 'chats' not in provides):
        raise ValueError('chats requires provides: [chats] and chats_view {contract, module}')
    if manifest.get('id') == BUILTIN:
        raise ValueError('wizard-chats is reserved for the built-in view')
    module = view.get('module')
    if (not isinstance(module, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.js', module)):
        raise ValueError('chats_view.module must be a relative .js package file')
    path = source.resolve() / module
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(source.resolve()):
        raise ValueError('Chats module symlinks are forbidden')
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError('Chats module missing or exceeds 128 KiB')
    body = path.read_bytes()
    if len(body) > MAX_BYTES or BANNED.search(body.decode('utf-8')):
        raise ValueError('Chats module uses a banned API or exceeds 128 KiB')
    return module, body


def prepare(source: Path, destination: Path, manifest: dict, commit: str) -> None:
    """Called only on the checked source at install/update, never on registry refresh."""
    checked = validate(source, manifest)
    if not checked:
        return
    module, body = checked
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Chats module requires a pinned source commit')
    target = destination / module
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    (destination / SEAL).write_text(json.dumps({
        'contract': CONTRACT, 'module': module, 'sha256': hashlib.sha256(body).hexdigest(),
        'bytes': len(body), 'commit': commit,
    }) + '\n')


def installed(directory: Path, manifest: dict, commit: str) -> dict | None:
    if manifest.get('chats_view') is None and 'chats' not in (manifest.get('provides') or []):
        return None
    checked = validate(directory, manifest)
    if checked is None:
        raise ValueError('Chats module is not declared')
    module, body = checked
    seal = json.loads((directory / SEAL).read_text())
    expected = {'contract': CONTRACT, 'module': module, 'sha256': hashlib.sha256(body).hexdigest(),
                'bytes': len(body), 'commit': commit}
    if seal != expected:
        raise ValueError('Chats module install integrity check failed; reinstall the checked package')
    return seal


def publish(reg: dict, root: Path) -> None:
    """Copy only sealed modules, never source trees; old URLs disappear on uninstall."""
    base = root / 'chats-views'
    base.mkdir(parents=True, exist_ok=True)
    keep = set()
    for app in reg['apps']:
        view = app.get('chats_view')
        if not view:
            continue
        keep.add(app['id'])
        destination = base / app['id']
        if destination.is_symlink():
            raise ValueError('Chats share symlinks are forbidden')
        destination.mkdir(exist_ok=True)
        source = Path(app.get('_chats_src') or destination / (view['sha256'] + '.js'))
        if not 0 < source.stat().st_size == view['bytes'] <= MAX_BYTES:
            raise ValueError('Chats module size changed after admission')
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != view['sha256']:
            raise ValueError('Chats module changed after admission')
        target = destination / (view['sha256'] + '.js')
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(body)
        temporary.chmod(0o644)
        temporary.replace(target)
        for old in destination.iterdir():
            if old != target:
                old.unlink()
    for old in base.iterdir():
        if old.name not in keep:
            if old.is_symlink() or not old.is_dir():
                old.unlink()
            else:
                shutil.rmtree(old)


def choose(root: Path, provider: str) -> dict:
    """Only the existing privileged Settings listener calls this mutation."""
    from . import slots
    path = root / 'apps.json'
    reg = json.loads(path.read_text())
    candidates = [BUILTIN, *[a['id'] for a in reg.get('apps', []) if a.get('chats_view') and 'chats' in a.get('provides', [])]]
    if provider not in candidates:
        raise ValueError('not an installed Chats provider')
    reg.setdefault('slots', {})['chats'] = {'default': provider, 'candidates': candidates}
    slots.merge_into_registry(reg)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(reg, indent=2) + '\n')
    temporary.replace(path)
    return reg['slots']['chats']
