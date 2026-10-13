"""Builder source gate for James-approved shared app functions (v1).

A source lint, not a JavaScript sandbox. Do not infer trust from a filename.
Only a release-owner supplied exact path + SHA256 exempts shared assets.
The normal builder deliberately supplies no exemptions.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
from pathlib import Path

import yaml

CLIENT_URL = '/wizard-os-home/core/app-client.js'
SHARED_NAMES = {'app-client.js', 'app-services.js', 'app-capture.js'}
EXTENSIONS = {'.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte', '.css', '.svg'}
SKIP = {'.git', 'node_modules', '.venv', '__pycache__', '.pytest_cache'}


def _code(text):
    # Preserve quoted values (bracket access / theme keys), erase comments without
    # accidentally treating https:// inside a string as a comment.
    token = re.compile(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(//[^\n]*|/\*[\s\S]*?\*/|<!--[\s\S]*?-->)''')
    return token.sub(lambda m: m.group(1) or '\n' * m.group(0).count('\n') + ' ', text)


def _member(name):
    return rf'''(?:\.\s*{name}\b|\[\s*['"]{name}['"]\s*\])'''


RULES = [
    ('capture', r'''\b(?:getUserMedia|webkitGetUserMedia|mozGetUserMedia|MediaRecorder|AudioContext|webkitAudioContext)\b|\bnavigator\s*''' + _member('permissions') + r'''\s*''' + _member('query') + r'''\s*\([^)]*?\bname\s*:\s*['"](?:microphone|camera)['"]''', 'Use WizardApp.capture; permission, recording, levels and durable chunks belong to core.'),
    ('share', r'\bnavigator\s*' + _member('share'), 'Use WizardApp.share.text/file and await its honest outcome.'),
    ('close-watcher', r'\bCloseWatcher\b', 'Use WizardApp.back; only shared core owns the platform exit guard.'),
    ('service-worker', r'\bnavigator\s*' + _member('serviceWorker') + r'''|\b(?:importScripts|skipWaiting)\s*\(|\bself\s*\.\s*clients\s*\.\s*claim\s*\(|(?<![\w.])(?:self\s*\.\s*)?addEventListener\s*\(\s*['"](?:install|activate|fetch)['"]|\bself\s*\.\s*on(?:install|activate|fetch)\s*=''', 'Hosted apps do not own service workers or update reload policy.'),
    ('install-prompt', r'\bbeforeinstallprompt\b|\b(?:deferredPrompt|installPrompt)\s*\.\s*prompt\s*\(', 'Installation UI belongs to Wizard, not a hosted app.'),
    ('theme-storage', r'''\b(?:localStorage|sessionStorage)\s*\.\s*(?:setItem|getItem)\s*\(\s*['"](?:theme|dark(?:Mode|-mode)?|color[-_]?scheme|wos\.skin|skin)['"]''', 'System skin/dark/light state belongs to WizardApp.theme, not app storage.'),
    ('theme-switch', r'''\b(?:toggleDarkMode|toggleTheme|setDarkMode)\s*\(|(?:id|data-action)\s*=\s*['"][^'"]*(?:theme-toggle|dark-mode|dark-toggle)[^'"]*['"]|\bdocument\s*\.\s*documentElement\s*\.\s*classList\s*\.\s*(?:toggle|add|remove)\s*\(\s*['"](?:dark|light|dark-mode|light-mode)['"]|\bdocument\s*\.\s*documentElement\s*\.\s*setAttribute\s*\(\s*['"]data-(?:theme|color-scheme)['"]\s*,\s*['"](?:dark|light)['"]''', 'Remove app-local system theme switches; keep app reading/art controls.'),
    ('root-guard', r'''\b(?:wzGuard|wosGuard|rootGuard|backGuard|__wosNativeBack)\b|Press\s+back\s+again\s+to\s+exit''', 'Use WizardApp.back; root exit/standalone guards live only in shared core.'),
]


def _bounce_in(body):
    # Domain route state (including same-URL state) and constructed route URLs
    # are legal. Empty/primitive same-document pushState in popstate is a
    # sentinel, as is moving forward; marker-named guards have a separate rule.
    return bool(re.search(r'''history\s*\.\s*forward\s*\(|history\s*\.\s*pushState\s*\(\s*(?:\{\s*\}|null|true|false|[0-9]+)\s*,\s*['"][^'"]*['"]\s*(?:\)|,\s*(?:window\s*\.\s*)?location\s*\.\s*(?:href|pathname)\s*\))''', body))


def _popstate_bounce(text):
    # Restrict the check to the actual handler, not the next route function.
    starts = re.finditer(r'''addEventListener\s*(\()\s*['"]popstate['"]|onpopstate\s*=\s*(?:function[^\{]*|[^=;]*=>\s*)(\{)''', text)
    for start in starts:
        opening = '(' if start.group(1) else '{'
        closing = ')' if opening == '(' else '}'
        pos = start.start(1) if start.group(1) else start.start(2)
        depth = 0
        tokens = re.finditer(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[(){}]''', text[pos:])
        end = len(text)
        for token in tokens:
            v = token.group(0)
            if v == opening:
                depth += 1
            elif v == closing:
                depth -= 1
                if not depth:
                    end = pos + token.end()
                    break
        body = text[pos:end]
        # A domain redirect to a different route from a popstate callback is
        # legal. Only rearming the current entry / moving forward is an exit
        # sentinel here; route callbacks are not a blanket ban on history.
        if _bounce_in(body):
            return True
    # Expression-bodied assignments have no brace to balance. Stop at a
    # statement boundary so a later route function is never swept in.
    for expression in re.finditer(r'''onpopstate\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z_$][\w$]*)\s*=>\s*(?!\{)([^;\n]+)''', text):
        if _bounce_in(expression.group(1)):
            return True
    return False


def _scan_text(raw, rel):
    text = _code(raw)
    rows = []
    for name, expression, remedy in RULES:
        match = re.search(expression, text, re.I if name in {'theme-storage', 'theme-switch', 'root-guard'} else 0)
        if match:
            line = text.count('\n', 0, match.start()) + 1
            rows.append({'rule': 'system functions: ' + name, 'ok': False, 'severity': 'error', 'detail': f'{rel}:{line}: {remedy}'})
    if _popstate_bounce(text):
        rows.append({'rule': 'system functions: root-history-bounce', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': move history sentinel rearming into shared core; use back.useHistory for routes'})
    return rows


def _packed_rows(root):
    # The builder's own inline Runtipi format is known, not arbitrary code to
    # evaluate. Inspect its shipped files too so a stale payload cannot smuggle
    # an old theme client/Back guard through a clean-looking source directory.
    p = root / 'runtipi/docker-compose.yml'
    if not p.is_file() or p.is_symlink():
        return []
    rows = []
    try:
        compose = yaml.safe_load(p.read_text())
        for service in (compose or {}).get('services', {}).values():
            cmd = service.get('command')
            if not isinstance(cmd, list) or len(cmd) != 3 or cmd[1] != '-c' or not isinstance(cmd[2], str):
                continue
            match = re.search(r'''files\s*=\s*json\.loads\(base64\.b64decode\(['"]([A-Za-z0-9+/=]*)['"]\)\)''', cmd[2])
            if not match:
                continue
            if len(match.group(1)) > 8 * 1024 * 1024:
                raise ValueError('Known inline source payload exceeds 8 MiB')
            payload = json.loads(base64.b64decode(match.group(1), validate=True))
            if not isinstance(payload, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in payload.items()):
                raise ValueError('Known inline source payload must map file names to text')
            for name, text in payload.items():
                if Path(name).suffix.lower() in EXTENSIONS:
                    rows += _scan_text(text, 'runtipi/docker-compose.yml::' + name)
    except (ValueError, OSError, yaml.YAMLError, AttributeError) as e:
        rows.append({'rule': 'system functions: readable inline source', 'ok': False, 'severity': 'error', 'detail': str(e)})
    return rows


def check(root: Path, approved_assets: dict[str, str] | None = None) -> list[dict]:
    root = Path(root).resolve()
    approved_assets = approved_assets or {}
    rows = []
    scanned = 0
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(part in SKIP for part in rel.parts):
            continue
        if p.is_symlink():
            rows.append({'rule': 'system functions: confined source', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': symlink cannot hide platform code'})
            continue
        if p.suffix.lower() not in EXTENSIONS or not p.is_file():
            continue
        scanned += 1
        digest = approved_assets.get(str(p.resolve()))
        if digest is not None:
            # No name-only trust; a path, exact bytes and a release-owned core name
            # must all match. This map must never be loaded from app metadata.
            if p.name in SHARED_NAMES and hashlib.sha256(p.read_bytes()).hexdigest() == digest:
                continue
            rows.append({'rule': 'system functions: approved asset hash', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': approved core bytes do not match'})
        try:
            text = p.read_text(encoding='utf-8')
        except (UnicodeError, OSError) as e:
            rows.append({'rule': 'system functions: readable source', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': ' + str(e)})
            continue
        rows += _scan_text(text, rel)
    rows += _packed_rows(root)
    rows.insert(0, {'rule': 'system functions: shared platform implementations only', 'ok': not rows, 'severity': 'error', 'detail': f'Scanned {scanned} source files. Client: {CLIENT_URL}. Static lint requires the browser checklist too.'})
    return rows
