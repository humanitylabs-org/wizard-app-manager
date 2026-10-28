"""Portable builder candidate. Local stage is distinct from production Runtipi install.

Invoke from a source checkout (templates and system-skills ship in the source
bundle). No public writes, signing, agent restart or global registry mutation.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from . import app_standard, wizardmd, system_functions

REPO = Path(__file__).resolve().parent.parent
ID = re.compile(r'[a-z0-9][a-z0-9-]{1,40}')
REPO_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*')
IGNORED = {'.git', '__pycache__', '.pytest_cache', '.builder.json', '.data'}
SECRET_FILES = {'.env', '.env.local', 'auth.json', 'credentials.json', 'token', 'api-token', 'mcp-token'}


def _id(value):
    if not ID.fullmatch(value):
        raise ValueError('App id must be 2–41 lowercase letters, digits or hyphens.')
    return value


def _home():
    return Path(os.environ.get('WIZARD_OS_HOME', Path.home() / '.wizard-os')).expanduser()


def _json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


def source_files(root):
    root = Path(root).resolve()
    files = []
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(x in IGNORED for x in rel.parts):
            continue
        if p.is_symlink():
            raise ValueError(f'Source symlink excluded: {rel}')
        if any(x in SECRET_FILES or x.endswith(('.pem', '.key')) for x in rel.parts):
            raise ValueError(f'Potential secret file excluded: {rel}')
        if p.is_file():
            if p.stat().st_size > 10 * 1024 * 1024:
                raise ValueError(f'File exceeds source bundle limit: {rel}')
            files.append(p)
    if sum(p.stat().st_size for p in files) > 25 * 1024 * 1024:
        raise ValueError('Source bundle exceeds 25 MiB.')
    return files


def source_hash(root):
    root = Path(root).resolve()
    h = hashlib.sha256()
    for p in source_files(root):
        b = p.read_bytes()
        h.update(str(p.relative_to(root)).encode() + b'\0' + len(b).to_bytes(8, 'big') + b)
    return h.hexdigest()


def local_references(doc):
    """Every manifest value that names a file or folder inside the app."""
    refs = {'skill': doc.get('skill'), 'icon': doc.get('icon')}
    rt = doc.get('runtime') if isinstance(doc.get('runtime'), dict) else {}
    refs.update({'runtime.config': rt.get('config'), 'runtime.compose': rt.get('compose')})
    api = doc.get('api') if isinstance(doc.get('api'), dict) else {}
    spec = api.get('spec')
    if isinstance(spec, str) and not re.match(r'^[a-z]+://', spec):
        refs['api.spec'] = spec
    src = (doc.get('view') or {}).get('source') if isinstance(doc.get('view'), dict) else None
    if isinstance(src, dict) and not src.get('repo'):
        refs.update({'view.source.path': src.get('path'), 'view.source.dist': src.get('dist')})
    return {k: v for k, v in refs.items() if v is not None}


def confinement_rows(root, doc):
    """Builder rule: a bundle is only what is inside it. Rejects absolute, escaping or symlinked
    references, so check, staging and publication all describe the same self-contained files."""
    rows = []
    for key, value in local_references(doc).items():
        ok, detail = isinstance(value, str) and bool(value.strip()), str(value)
        if ok:
            rel = Path(value)
            ok = not rel.is_absolute() and '..' not in rel.parts
            if ok:
                target, walk = root / rel, root
                for part in rel.parts:
                    walk = walk / part
                    if walk.is_symlink():
                        ok, detail = False, f'{value} passes through a symlink'
                        break
                ok = ok and target.exists() and target.resolve().is_relative_to(root)
        rows.append({'rule': f'builder: {key} stays inside the app', 'ok': ok, 'severity': 'error', 'detail': detail})
    return rows


def kit_rows(root):
    """The shell swaps a hosted app's vendored wizard-ui.css for its live kit, so app edits to the kit vanish."""
    pinned = REPO / 'templates' / 'hello-wizard' / 'web' / 'wizard-ui.css'
    rows = []
    for p in sorted(Path(root).rglob('wizard-ui.css')):
        if any(part in {'.git', 'node_modules'} for part in p.parts) or not pinned.is_file():
            continue
        if p.read_bytes() != pinned.read_bytes():
            rows.append({'rule': 'appearance: kit is shared, not forked', 'ok': False, 'severity': 'warning',
                         'detail': f'{p.relative_to(root)} differs from the current Wizard UI kit. Inside Wizard the shell '
                                   'replaces it with /wizard-os-home/kit/wizard-ui.css, so put app styles in styles.css and '
                                   'refresh the copy from templates/hello-wizard/web/.'})
    return rows


_VENDORED_DIRS = {'.git', 'node_modules', 'vendor', 'third_party', 'dist', 'build'}
_KIT_NAMES = {'wizard-ui.css', 'wizard-tokens.css', 'wizard-sheet.js', 'boot.js', 'theme-client.js'}
_FRAME = {'.wz-app', '.wz-scroll', 'html', 'body', ':root', 'main', '#app', '.app'}
_COLOUR = re.compile(r'#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(')
_DIALOG = re.compile(r'(?<![\w.$])(?:window\.)?(alert|confirm|prompt)\s*\(')
_RULES_URL = 'https://github.com/humanitylabs-org/wizard-os/blob/main/style/APP-DESIGN-RULES.md'
# Shell features that reach outside the app's window (Chats, navigation). Newer Wizards add them, so an app
# that calls one unguarded breaks on every older Wizard. Guarded = feature-detected before use.
_SHELL_CALL = re.compile(r'WizardApp\.(nav|chat)\.(\w+)\s*\(')


def _own_files(root, suffixes):
    """The app's own view files: vendored dirs, kit copies and untouched template files are not the app's."""
    template = REPO / 'templates' / 'hello-wizard' / 'web'
    for p in sorted(root.rglob('*')):
        if p.suffix not in suffixes or not p.is_file() or p.is_symlink() or p.name in _KIT_NAMES:
            continue
        if any(part in _VENDORED_DIRS for part in p.relative_to(root).parts) or p.name.endswith('.min.js'):
            continue
        same = template / p.name
        if p.name != 'index.html' and same.is_file() and same.read_bytes() == p.read_bytes():
            continue
        yield p


def _frame_tokens(root):
    tokens = set(_FRAME)
    html = root / 'web' / 'index.html'
    if html.is_file():
        m = re.search(r'<body[^>]*>\s*<\w+([^>]*)>', html.read_text(errors='replace'), re.I)
        if m:
            for c in re.findall(r'class\s*=\s*["\']([^"\']*)', m.group(1)):
                tokens |= {'.' + x for x in c.split()}
            tokens |= {'#' + i for i in re.findall(r'\bid\s*=\s*["\']([^"\']+)', m.group(1))}
    return tokens


def _simple(compound):
    return set(re.findall(r'[.#:]?[\w-]+', re.sub(r'::?[\w-]+(\([^)]*\))?$', '', compound)))


def design_rows(root):
    """Warnings for app/design rule breaks (wizard-os style/APP-DESIGN-RULES.md). Never errors."""
    root = Path(root)
    frame, capped, colours, dialogs, unguarded = _frame_tokens(root), [], [], [], []
    for p in _own_files(root, {'.css', '.scss', '.less'}):
        css = re.sub(r'/\*.*?\*/', '', p.read_text(errors='replace'), flags=re.S)
        for sel, decl in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
            if re.search(r'(?<![\w-])max-width\s*:\s*(?!none\b)', decl):
                for one in sel.split(','):
                    parts = [_simple(x) for x in re.split(r'[\s>+~]+', one.strip()) if x]
                    # Only the frame itself: a frame token reached through root/frame ancestors, never a nested `main`.
                    if parts and parts[-1] & frame and all(x & (frame | {'html', 'body', ':root'}) for x in parts[:-1]):
                        capped.append(f'{p.relative_to(root)}: {one.strip()}')
            if _COLOUR.search(decl):
                colours.append(str(p.relative_to(root)))
    for p in _own_files(root, {'.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.html'}):
        text = re.sub(r'/\*.*?\*/', '', p.read_text(errors='replace'), flags=re.S)
        text = re.sub(r'(?m)^\s*//.*$', '', text)
        if any(not re.search(r'function\s*$', text[max(0, m.start() - 40):m.start()]) for m in _DIALOG.finditer(text)):
            dialogs.append(str(p.relative_to(root)))
        for m in _SHELL_CALL.finditer(text):
            fn = m.group(2)
            if not re.search(r'(\?\.\s*' + fn + r'\b)|(typeof\s+[\w.?]*\b' + fn + r'\b)|([\'"]' + fn + r'[\'"]\s+in\b)', text):
                unguarded.append(f'{p.relative_to(root)}: WizardApp.{m.group(1)}.{fn}')
    rows = []
    if capped:
        rows.append({'rule': 'design: the window is the page', 'ok': False, 'severity': 'warning',
                     'detail': 'max-width on the app frame (' + '; '.join(capped[:5]) + '). Fill the window; use '
                               '.wz-grid for more columns and .wz-prose only for long text. See ' + _RULES_URL})
    if colours:
        rows.append({'rule': 'design: colours come from the kit', 'ok': False, 'severity': 'warning',
                     'detail': 'hex/rgb/hsl colour literals in ' + ', '.join(sorted(set(colours))[:5])
                               + '. Use the kit variables (var(--ink), var(--accent)...) so every skin applies. See ' + _RULES_URL})
    if dialogs:
        rows.append({'rule': 'design: one pop-up, the sheet', 'ok': False, 'severity': 'warning',
                     'detail': 'window.alert/confirm/prompt in ' + ', '.join(dialogs[:5]) + '. Use <dialog class="wz-sheet"> '
                               'with WzSheet.open (wizard-sheet.js), or .wz-status for messages. See ' + _RULES_URL})
    if unguarded:
        rows.append({'rule': 'compatibility: works on every Wizard', 'ok': False, 'severity': 'warning',
                     'detail': 'unguarded shell call (' + '; '.join(sorted(set(unguarded))[:5]) + '). Older Wizards lack '
                               'newer shell features, so check first (typeof WizardApp?.nav?.<fn> === \'function\') and '
                               'hide the control when it is missing; one store release then runs on every Wizard.'})
    return rows


def check_app(root, proposal=False):
    root = Path(root).resolve()
    approved = wizardmd.check(root)
    rows = [{'rule': n, 'ok': ok, 'severity': 'error', 'detail': d} for n, ok, d in approved]
    rows += system_functions.check(root)
    rows += kit_rows(root)
    rows += design_rows(root)
    if wizardmd.passed(approved):
        doc = wizardmd.front_matter((root / 'wizard.md').read_text())
        rows += confinement_rows(root, doc)
        if proposal:
            rows += app_standard.check(root, doc)
    return {'ok': all(r['ok'] or r['severity'] == 'warning' for r in rows),
            'policy': 'PR24 proposed (opt-in)' if proposal else 'approved checker',
            'rules': rows, 'warnings': [r for r in rows if not r['ok'] and r['severity'] == 'warning']}


def validate_style_snapshot(template):
    """Fail closed on edits to the generated canonical style distribution assets."""
    lock = json.loads((template / 'appearance-provenance.json').read_text())
    mapping = {'style/tokens.css': 'web/wizard-tokens.css', 'style/ui.css': 'web/wizard-ui.css',
               'style/boot.js': 'web/boot.js', 'style/sheet.js': 'web/wizard-sheet.js'}
    if (lock.get('schema') != 3 or lock.get('origin_repo') != 'https://github.com/humanitylabs-org/wizard-os'
            or not re.fullmatch(r'[0-9a-f]{40}', str(lock.get('revision', '')))
            or set(lock.get('files', {})) != set(mapping)):
        raise ValueError('Invalid canonical style/ snapshot lock; refresh with tools/sync_wizard_style.py.')
    for src, dst in mapping.items():
        item = lock['files'][src]
        path = template / dst
        if (item.get('destination') != dst or not path.is_file() or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256')):
            raise ValueError('Canonical style/ snapshot drifted: ' + dst)


def new_app(app_id, dev_root=None, request='', *, name=None):
    _id(app_id)
    dev_root = Path(dev_root) if dev_root is not None else _home() / 'dev'
    dev_root.mkdir(parents=True, exist_ok=True)
    target = dev_root / app_id
    if target.exists() or target.is_symlink():
        raise ValueError(f'{target} already exists; preserving it.')
    template = REPO / 'templates' / 'hello-wizard'
    if not template.is_dir():
        raise ValueError('Source bundle missing templates/hello-wizard; use the complete source checkout.')
    validate_style_snapshot(template)
    display = name or app_id.replace('-', ' ').title()
    scratch = Path(tempfile.mkdtemp(prefix='.builder-', dir=dev_root))
    try:
        shutil.copytree(template, scratch, dirs_exist_ok=True)
        # Replacement is restricted to identifiers in source files. User prose
        # stays metadata; it is never evaluated as source or shell arguments.
        for p in source_files(scratch):
            if p.suffix in {'.py', '.json', '.md', '.yml', '.html', '.js'}:
                p.write_text(p.read_text().replace('hello-wizard', app_id))
        wm = scratch / 'wizard.md'
        doc = wizardmd.front_matter(wm.read_text())
        doc['name'] = display
        doc['source'] = 'https://github.com/owner/' + app_id  # placeholder, publication preparation patches in snapshot
        if 'habit' in request.lower():
            doc['summary'] = 'Track daily habits through a screen, documented HTTP API and MCP.'
            doc['provides'] = ['habit-tracking']
        wm.write_text('---\n' + yaml.safe_dump(doc, sort_keys=False) + '---\n\n' + wizardmd.body(wm.read_text()) + '\n')
        cfgp = scratch / 'runtipi/config.json'
        cfg = json.loads(cfgp.read_text()); cfg.update(name=display, source=doc['source'], short_desc=doc['summary'])
        _json(cfgp, cfg)
        _json(scratch / '.builder.json', {'request': request, 'template': 'hello-wizard', 'state': 'local_draft', 'name': display})
        from .builder_assets import package_inline
        package_inline(scratch)
        report = check_app(scratch, proposal=True)
        if not report['ok']:
            raise ValueError('Generated template failed checks: ' + json.dumps(report))
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(scratch)], check=True)
        scratch.rename(target)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return target


def install_local(root, os_home=None, *, stage_only=False, run=False, port=18799, trusted_origins=()):
    if not stage_only:
        raise ValueError('Local candidate requires --stage-only; production lifecycle belongs to App installation lane.')
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged local port (1024–65535).')
    origins = []
    for o in trusted_origins:
        m = re.fullmatch(r'(https?)://([a-z0-9.-]+(?::\d{1,5})?)', str(o).strip().lower())
        if not m:
            raise ValueError(f'Trusted origin must be scheme://host[:port], got {o!r}.')
        origins.append(m.group(0))
    root = Path(root).resolve()
    os_home = (Path(os_home) if os_home is not None else _home()).resolve()
    os_home.mkdir(parents=True, exist_ok=True)
    pre = wizardmd.front_matter((root / 'wizard.md').read_text())
    app_id = _id(str(pre.get('id', '')))
    stage = os_home / 'staged-apps' / app_id
    if stage.exists() or stage.is_symlink():
        raise ValueError(f'{stage} already exists; preserving candidate and data. Choose another isolated WIZARD_OS_HOME.')
    scratch = Path(tempfile.mkdtemp(prefix='.builder-stage-', dir=os_home))
    try:
        # Snapshot first. Everything after this reads only the snapshot: what is checked, hashed,
        # built and reported is exactly what was staged.
        snapshot = scratch / 'source'; snapshot.mkdir()
        before = source_hash(root)
        for p in source_files(root):
            dest = snapshot / p.relative_to(root); dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(p, dest)
        digest = source_hash(snapshot)
        if digest != before or source_hash(root) != before:
            raise ValueError('Source changed while staging; nothing was staged. Try again when edits finish.')
        report = check_app(snapshot, proposal=True)
        if not report['ok']:
            raise ValueError('App check failed: ' + json.dumps([r for r in report['rules'] if not r['ok'] and r['severity'] == 'error']))
        doc = wizardmd.front_matter((snapshot / 'wizard.md').read_text())
        if doc.get('id') != app_id:
            raise ValueError('App id changed while staging; nothing was staged.')
        if not (snapshot / 'app/Dockerfile').is_file():
            raise ValueError('Local runner currently requires app/Dockerfile; keep other apps as source-only drafts.')
        # One Docker project per developer home + app: two homes staging the same app never share
        # containers or data. Compose namespaces the volume by project.
        project = f'wizard-dev-{app_id}-' + hashlib.sha256(str(stage).encode()).hexdigest()[:10]
        hosts = [f'127.0.0.1:{port}', f'localhost:{port}'] + [o.split('://', 1)[1] for o in origins]
        final_source = stage / 'source'
        compose = {'services': {app_id: {'build': {'context': str(final_source), 'dockerfile': 'app/Dockerfile'},
                   'image': f'{project}:{digest[:12]}', 'user': '1000:1000',
                   'ports': [f'127.0.0.1:{port}:8799'],
                   'environment': {'PORT': '8799', 'DATA_DIR': '/data',
                                   'WIZARD_TRUSTED_HOSTS': ','.join(hosts),
                                   'WIZARD_TRUSTED_ORIGINS': ','.join([f'http://{h}' for h in hosts[:2]] + origins)},
                   'volumes': ['app-data:/data'], 'read_only': True, 'cap_drop': ['ALL'],
                   'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp'], 'mem_limit': '128m'}},
                   'volumes': {'app-data': {}}}
        (scratch / 'compose.yml').write_text(yaml.safe_dump(compose, sort_keys=False))
        rec = {'id': app_id, 'name': doc['name'], 'state': 'staged_not_running', 'source_sha256': digest,
               'source': str(final_source), 'compose': str(stage / 'compose.yml'),
               'url': f'http://127.0.0.1:{port}/', 'mcp_url': f'http://127.0.0.1:{port}/mcp',
               'project': project, 'trusted_origins': compose['services'][app_id]['environment']['WIZARD_TRUSTED_ORIGINS'].split(','),
               'lifecycle_owner': 'App installation lane',
               **{k: doc[k] for k in ('api', 'mcp', 'view', 'backend', 'replaces_view', 'provides', 'uses', 'needs') if k in doc}}
        _json(scratch / 'stage.json', rec)
        stage.parent.mkdir(parents=True, exist_ok=True); scratch.rename(stage)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    if run:
        subprocess.run(['docker', 'compose', '-p', rec['project'], '-f', rec['compose'], 'up', '-d', '--build'], check=True,
                       stdout=sys.stderr)  # build logs never pollute the JSON receipt on stdout
        rec['state'] = 'started_health_unverified'
        _json(stage / 'stage.json', rec)
    return rec


def update_local(root, os_home=None, *, run=False):
    """Update a staged developer app to new source, keeping its project, port and data volume.
    The staged-app equivalent of an app update: the same snapshot-then-check rule as install_local."""
    root = Path(root).resolve()
    os_home = (Path(os_home) if os_home is not None else _home()).resolve()
    app_id = _id(str(wizardmd.front_matter((root / 'wizard.md').read_text()).get('id', '')))
    stage = os_home / 'staged-apps' / app_id
    rec = json.loads((stage / 'stage.json').read_text())
    scratch = Path(tempfile.mkdtemp(prefix='.builder-update-', dir=stage))
    try:
        snapshot = scratch / 'source'; snapshot.mkdir()
        before = source_hash(root)
        for p in source_files(root):
            dest = snapshot / p.relative_to(root); dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(p, dest)
        digest = source_hash(snapshot)
        if digest != before or source_hash(root) != before:
            raise ValueError('Source changed while updating; nothing was changed.')
        report = check_app(snapshot, proposal=True)
        if not report['ok']:
            raise ValueError('App check failed: ' + json.dumps([r for r in report['rules'] if not r['ok'] and r['severity'] == 'error']))
        if digest == rec['source_sha256']:
            return {**rec, 'update': 'already_current'}
        doc = wizardmd.front_matter((snapshot / 'wizard.md').read_text())
        if doc.get('id') != app_id:
            raise ValueError('An update cannot change the app id.')
        previous = stage / 'source.previous'
        if previous.exists():
            shutil.rmtree(previous)
        (stage / 'source').rename(previous)
        snapshot.rename(stage / 'source')
        compose = yaml.safe_load((stage / 'compose.yml').read_text())
        compose['services'][app_id]['image'] = f"{rec['project']}:{digest[:12]}"
        (stage / 'compose.yml.tmp').write_text(yaml.safe_dump(compose, sort_keys=False)); (stage / 'compose.yml.tmp').replace(stage / 'compose.yml')
        rec.update(source_sha256=digest, previous_source_sha256=rec['source_sha256'], version=doc.get('version'),
                   **{k: doc[k] for k in ('api', 'mcp', 'view') if k in doc})
        _json(stage / 'stage.json', rec)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    if run:
        subprocess.run(['docker', 'compose', '-p', rec['project'], '-f', rec['compose'], 'up', '-d', '--build'], check=True,
                       stdout=sys.stderr)  # build logs never pollute the JSON receipt on stdout
    return {**rec, 'update': 'updated'}


def install_skill(os_home=None, hermes_home=None):
    """Compatibility alias: install the complete bundle and register discovery."""
    from . import builder_bundle
    os_home = Path(os_home) if os_home is not None else _home()
    hermes_home = Path(hermes_home) if hermes_home is not None else Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
    cfg = yaml.safe_load((hermes_home / 'config.yaml').read_text()) or {} if (hermes_home / 'config.yaml').exists() else {}
    builder_bundle.check_collisions(hermes_home, os_home, cfg)
    out = builder_bundle.install(os_home)
    out['registered'] = builder_bundle.register(hermes_home, os_home)
    return {**out, 'generated_skills_touched': False, 'gateway_connections_added': 0}


def prepare_publish(root, repo):
    if not REPO_ID.fullmatch(repo):
        raise ValueError('Expected exact GitHub owner/repository.')
    root = Path(root).resolve()
    report = check_app(root, proposal=True)
    if not report['ok']:
        raise ValueError('Source failed checker.')
    files = source_files(root)
    digest = source_hash(root)
    doc = wizardmd.front_matter((root / 'wizard.md').read_text())
    source = 'https://github.com/' + repo
    return {'schema': 1, 'state': 'prepared_requires_human_approval', 'app_id': doc['id'],
            'source_sha256': digest, 'files': [str(p.relative_to(root)) for p in files],
            'wizard_source': source,
            'manifest_patch': {'wizard.md': {'source': source}, 'runtipi/config.json': {'source': source}},
            'github': {'repo': repo, 'visibility': 'public', 'topics': ['wizard-app'], 'description': doc['summary'],
                       'default_branch': 'main', 'create_command': ['gh', 'repo', 'create', repo, '--public', '--description', doc['summary']],
                       'topics_request': {'method': 'PUT', 'path': 'repos/' + repo + '/topics', 'body': {'names': ['wizard-app']}}},
            'catalog': {'method': 'POST', 'url': 'https://apps.aiwizards.com/v1/publish', 'body': {'repo': repo},
                        'verify': 'Read index listing back and match installed/source commit; listed:true is required.'},
            'signing': {'state': 'not_signed', 'requires': 'Separate owner approval in Wizard signing sheet',
                        'nip34': {'kind': 30617, 'tags': [['d', repo.split('/')[1]], ['t', 'wizard-app'], ['clone', source]],
                                  'euc': 'first public repository commit after approved creation', 'publisher': 'owner-selected npub'}},
            'execution': {'implemented': False, 'public_writes_performed': 0,
                          'next': 'Human approves this exact source hash, repo/visibility/topic/catalog and signing separately. Release owner then patches source in an isolated copy, checks it, creates/pushes repo, relists and reads back.'}}


def main(argv=None):
    ap = argparse.ArgumentParser(prog='wam builder', description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    n = sub.add_parser('new'); n.add_argument('id'); n.add_argument('--request', default=''); n.add_argument('--name'); n.add_argument('--dev-root', type=Path)
    c = sub.add_parser('check'); c.add_argument('source', type=Path); c.add_argument('--proposal', action='store_true')
    i = sub.add_parser('install-local'); i.add_argument('source', type=Path); i.add_argument('--stage-only', action='store_true'); i.add_argument('--run', action='store_true'); i.add_argument('--port', type=int, default=18799); i.add_argument('--trust-origin', action='append', default=[], help='Extra exact origin allowed to call the app, e.g. a private preview proxy.')
    p = sub.add_parser('publish'); p.add_argument('source', type=Path); p.add_argument('--repo', required=True); p.add_argument('--output', type=Path); p.add_argument('--execute', action='store_true')
    up = sub.add_parser('update-local'); up.add_argument('source', type=Path); up.add_argument('--run', action='store_true')
    sk = sub.add_parser('builder-skill'); sk.add_argument('--hermes-home', type=Path)
    inline = sub.add_parser('package-inline'); inline.add_argument('source', type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'new':
            out = {'source': str(new_app(a.id, a.dev_root, a.request, name=a.name)), 'state': 'local_draft'}
        elif a.cmd == 'check':
            out = check_app(a.source, a.proposal)
            print(json.dumps(out, indent=2)); return 0 if out['ok'] else 1
        elif a.cmd == 'install-local':
            out = install_local(a.source, stage_only=a.stage_only, run=a.run, port=a.port, trusted_origins=a.trust_origin)
        elif a.cmd == 'update-local':
            out = update_local(a.source, run=a.run)
        elif a.cmd == 'builder-skill':
            out = install_skill(hermes_home=a.hermes_home)
        elif a.cmd == 'package-inline':
            from .builder_assets import package_inline
            package_inline(a.source)
            out = {'state': 'packaged', 'source': str(a.source)}
        else:
            if a.execute:
                raise ValueError('Public execution is approval-gated and delegated to release authority; only payload preparation is implemented.')
            out = prepare_publish(a.source, a.repo)
            if a.output:
                if a.output.exists():
                    raise ValueError('Publication plan exists; preserving it.')
                _json(a.output, out)
        print(json.dumps(out, indent=2)); return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f'builder: {e}', file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
