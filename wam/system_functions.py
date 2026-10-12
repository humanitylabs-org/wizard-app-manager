"""Builder source gate for James-approved shared app functions (v1).

A source lint, not a JavaScript sandbox. Do not infer trust from a filename.
Only a release-owner supplied exact path + SHA256 exempts shared assets.
The normal builder deliberately supplies no exemptions.
"""
from __future__ import annotations
import hashlib
import re
from pathlib import Path

CLIENT_URL = '/wizard-os-home/core/app-client.js'
SHARED_NAMES = {'app-client.js', 'app-services.js', 'app-capture.js'}
EXTENSIONS = {'.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte', '.css'}
SKIP = {'.git', 'node_modules', '.venv', '__pycache__', '.pytest_cache'}


def _code(text):
    # Preserve quoted values (bracket access / theme keys), erase comments without
    # accidentally treating https:// inside a string as a comment.
    token = re.compile(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(//[^\n]*|/\*[\s\S]*?\*/|<!--[\s\S]*?-->)''')
    return token.sub(lambda m: m.group(1) or '\n' * m.group(0).count('\n') + ' ', text)


def _member(name):
    return rf'''(?:\.\s*{name}\b|\[\s*['"]{name}['"]\s*\])'''


RULES = [
    ('capture', r'\b(?:getUserMedia|webkitGetUserMedia|mozGetUserMedia|MediaRecorder|AudioContext|webkitAudioContext)\b', 'Use WizardApp.capture; permission, recording, levels and durable chunks belong to core.'),
    ('share', r'\bnavigator\s*' + _member('share'), 'Use WizardApp.share.text/file and await its honest outcome.'),
    ('close-watcher', r'\bCloseWatcher\b', 'Use WizardApp.back; only shared core owns the platform exit guard.'),
    ('service-worker', r'\bnavigator\s*' + _member('serviceWorker') + r'|\b(?:importScripts|skipWaiting)\s*\(|\bself\s*\.\s*clients\s*\.\s*claim\s*\(', 'Hosted apps do not own service workers or update reload policy.'),
    ('install-prompt', r'\bbeforeinstallprompt\b|\b(?:deferredPrompt|installPrompt)\s*\.\s*prompt\s*\(', 'Installation UI belongs to Wizard, not a hosted app.'),
    ('theme-storage', r'''\b(?:localStorage|sessionStorage)\s*\.\s*(?:setItem|getItem)\s*\(\s*['"](?:theme|dark(?:Mode|-mode)?|color[-_]?scheme|wos\.skin|skin)['"]''', 'System skin/dark/light state belongs to WizardApp.theme, not app storage.'),
    ('theme-switch', r'''\b(?:toggleDarkMode|toggleTheme|setDarkMode)\s*\(|(?:id|data-action)\s*=\s*['"][^'"]*(?:theme-toggle|dark-mode|dark-toggle)[^'"]*['"]''', 'Remove app-local system theme switches; keep app reading/art controls.'),
    ('root-guard', r'''\b(?:wzGuard|wosGuard|rootGuard|backGuard|__wosNativeBack)\b|Press\s+back\s+again\s+to\s+exit''', 'Use WizardApp.back; root exit/standalone guards live only in shared core.'),
]


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
        if re.search(r'''history\s*\.\s*forward\s*\(|history\s*\.\s*pushState\s*\([^;]*?location\s*\.\s*(?:href|pathname)|history\s*\.\s*pushState\s*\([^,;]*,\s*['"][^'"]*['"]\s*\)''', body):
            return True
    return False


def check(root: Path, approved_assets: dict[str, str] | None = None) -> list[dict]:
    root = Path(root).resolve()
    approved_assets = approved_assets or {}
    rows = []
    scanned = 0
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(part in SKIP for part in rel.parts) or p.suffix.lower() not in EXTENSIONS:
            continue
        if p.is_symlink():
            rows.append({'rule': 'system functions: confined source', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': symlink cannot hide platform code'})
            continue
        if not p.is_file():
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
            text = _code(p.read_text(encoding='utf-8'))
        except (UnicodeError, OSError) as e:
            rows.append({'rule': 'system functions: readable source', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': ' + str(e)})
            continue
        for name, expression, remedy in RULES:
            match = re.search(expression, text, re.I if name in {'theme-storage', 'theme-switch', 'root-guard'} else 0)
            if match:
                line = text.count('\n', 0, match.start()) + 1
                rows.append({'rule': 'system functions: ' + name, 'ok': False, 'severity': 'error', 'detail': f'{rel}:{line}: {remedy}'})
        # A bounce in popstate recreates an exit sentinel. Real route history,
        # popstate render callbacks and history.back in domain handlers are valid.
        if _popstate_bounce(text):
            rows.append({'rule': 'system functions: root-history-bounce', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': move history sentinel rearming into shared core; use back.useHistory for routes'})
    rows.insert(0, {'rule': 'system functions: shared platform implementations only', 'ok': not rows, 'severity': 'error', 'detail': f'Scanned {scanned} source files. Client: {CLIENT_URL}. Static lint requires the browser checklist too.'})
    return rows
