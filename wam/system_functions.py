"""Builder source gate for James-approved shared app functions (v1).

A source lint, not a JavaScript sandbox. Do not infer trust from a filename.
Only a release-owner supplied exact path + SHA256 exempts shared assets.
The normal builder deliberately supplies no exemptions.
"""
from __future__ import annotations
import base64
import hashlib
import io
import json
import re
import shlex
import tokenize
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
    # James, 22 Oct 2026: "why for every app … are you implementing some bootstrap thing from scratch when we already
    # have a fully functional chat interface?" A conversation with an AI is the real Chats thread, never an app copy.
    ('app-chat', r'''<textarea\b[^>]*\b(?:placeholder|aria-label)\s*=\s*['"][^'"]*\b(?:ask|message|reply|chat|talk to)\b[^'"]*['"]|\.\s*placeholder\s*=\s*['"`][^'"`]*\b(?:ask|message|reply|chat|talk to)\b[^'"`]*['"`]|\b(?:class|id)\s*=\s*['"][^'"]*\b(?:chat|ask-ai|ai-chat|assistant)[-_](?:composer|input|messages|transcript|thread|log)\b[^'"]*['"]''',
     'An AI conversation opens the real Chats thread: WizardApp.nav.openThread(sessionId, {reply}) on an agent bound to this app. See references/chat.md.'),
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
        match = re.search(expression, text, re.I if name in {'theme-storage', 'theme-switch', 'root-guard', 'app-chat'} else 0)
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


def _reference_text(path: Path, text: str) -> str:
    """Strip language comments, retaining quoted paths and plain YAML scalars."""
    if path.suffix.lower() in {'.yaml', '.yml'}:
        return json.dumps(yaml.safe_load(text), default=str)
    if path.suffix.lower() == '.py':
        return tokenize.untokenize((t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)
                                  if t.type != tokenize.COMMENT)
    if path.suffix.lower() in {'.sh', '.bash', '.zsh'} or path.name.startswith(('Dockerfile', 'Containerfile', 'Procfile')):
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split = True
        return ' '.join(lexer)
    return _code(text)


def _package_runtime_text(packages: list[dict], entrypoints: str) -> str:
    """Do not mistake unused npm test scripts for runtime; follow invoked ones."""
    result = []
    for package in packages:
        scripts = package.get('scripts') or {}
        if not isinstance(scripts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in scripts.items()):
            raise ValueError('Unknown package scripts')
        active = {name for name in scripts if not re.match(r'^(test|lint|check|typecheck|format|qa)(:|$)', name)}
        while True:
            text = entrypoints + '\n' + '\n'.join(scripts[name] for name in active)
            invoked = {name for name in scripts if re.search(
                r'''\b(?:npm|pnpm|yarn|bun)[\s,"'\[\]]+(?:run[\s,"'\[\]]+)?''' + re.escape(name) + r'''(?=[\s,"'\[\];]|$)''', text)}
            if invoked <= active:
                break
            active |= invoked
        result.extend(scripts[name] for name in active)
        result.append(json.dumps({key: package[key] for key in ('main', 'browser', 'exports', 'bin') if key in package}))
    return '\n'.join(result)


def _development_roots(root: Path) -> set[str]:
    """Top-level harnesses outside an explicit, confined view source are not UI.

    This is not an app-controlled ignore list. Unknown/legacy view layouts keep
    the full scan, as do served test directories and runtime references to a
    development tree. Nested web/tests paths and inline Compose are always linted.
    """
    development = {name for name in ('tests', 'test', 'qa', 'ops') if (root / name).exists()}
    if not development:
        return set()
    try:
        from .wizardmd import front_matter
        source = (front_matter((root / 'wizard.md').read_text()).get('view') or {}).get('source') or {}
        paths = []
        for key in ('path', 'dist'):
            value = source.get(key)
            if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value:
                return set()
            parts = value.split('/')
            if any(p in {'', '.', '..'} for p in parts):
                return set()
            path = root / value
            if path.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(root):
                return set()
            paths.append(parts[0])
        development -= set(paths)
        # Inspect entrypoints too: Dockerfile has no suffix, and Compose can
        # invoke a package script or use an unquoted YAML command/build path.
        reference_extensions = EXTENSIONS | {'.py', '.yml', '.yaml', '.json', '.sh', '.bash', '.zsh'}
        while development:
            texts, packages = [], []
            for p in root.rglob('*'):
                rel = p.relative_to(root)
                if rel.parts[0] in development or any(part in SKIP for part in rel.parts):
                    continue
                if p.is_symlink() or not p.is_file():
                    continue
                if p.suffix.lower() not in reference_extensions and not p.name.startswith(('Dockerfile', 'Containerfile', 'Procfile')):
                    continue
                text = p.read_text(encoding='utf-8')
                if p.name == 'package.json':
                    packages.append(json.loads(text))
                else:
                    texts.append(_reference_text(p, text))
            text = '\n'.join(texts)
            text += '\n' + _package_runtime_text(packages, text)
            referenced = {name for name in development if re.search(r'(?<![\w.-])' + name + r'(?![\w.-])', text)}
            if not referenced:
                break
            # A referenced ops/start.sh can itself serve tests/. Follow such
            # references to a fixed point (at most four development trees).
            development -= referenced
        return development
    except (OSError, ValueError, UnicodeError, AttributeError, TypeError, tokenize.TokenError, IndentationError, yaml.YAMLError):
        return set()


def check(root: Path, approved_assets: dict[str, str] | None = None) -> list[dict]:
    root = Path(root).resolve()
    approved_assets = approved_assets or {}
    rows = []
    development = _development_roots(root)
    scanned = 0
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(part in SKIP for part in rel.parts):
            continue
        if p.is_symlink():
            rows.append({'rule': 'system functions: confined source', 'ok': False, 'severity': 'error', 'detail': str(rel) + ': symlink cannot hide platform code'})
            continue
        if rel.parts[0] in development:
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
