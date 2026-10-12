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
from . import app_standard, wizardmd

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


def check_app(root, proposal=False):
    root = Path(root).resolve()
    approved = wizardmd.check(root)
    rows = [{'rule': n, 'ok': ok, 'severity': 'error', 'detail': d} for n, ok, d in approved]
    if wizardmd.passed(approved):
        doc = wizardmd.front_matter((root / 'wizard.md').read_text())
        rows += confinement_rows(root, doc)
        if proposal:
            rows += app_standard.check(root, doc)
    return {'ok': all(r['ok'] or r['severity'] == 'warning' for r in rows),
            'policy': 'PR24 proposed (opt-in)' if proposal else 'approved checker',
            'rules': rows, 'warnings': [r for r in rows if not r['ok'] and r['severity'] == 'warning']}


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
        subprocess.run(['docker', 'compose', '-p', rec['project'], '-f', rec['compose'], 'up', '-d', '--build'], check=True)
        rec['state'] = 'started_health_unverified'
        _json(stage / 'stage.json', rec)
    return rec


def install_skill(os_home=None, hermes_home=None):
    os_home = Path(os_home) if os_home is not None else _home()
    hermes_home = Path(hermes_home) if hermes_home is not None else Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
    target = os_home / 'system-skills' / 'wizard-builder'
    src = REPO / 'system-skills' / 'wizard-builder'
    link = hermes_home / 'skills' / 'wizard-builder'
    if target.is_symlink():
        raise ValueError('System skill destination is a symlink; preserving it.')
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise ValueError('Hermes already has a different wizard-builder; preserving it.')
    shutil.copytree(src, target, dirs_exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to(target.resolve(), target_is_directory=True)
    return {'state': 'installed', 'skill': str(target / 'SKILL.md'), 'hermes_link': str(link),
            'generated_skills_touched': False, 'gateway_connections_added': 0}


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
    sk = sub.add_parser('builder-skill'); sk.add_argument('--hermes-home', type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'new':
            out = {'source': str(new_app(a.id, a.dev_root, a.request, name=a.name)), 'state': 'local_draft'}
        elif a.cmd == 'check':
            out = check_app(a.source, a.proposal)
            print(json.dumps(out, indent=2)); return 0 if out['ok'] else 1
        elif a.cmd == 'install-local':
            out = install_local(a.source, stage_only=a.stage_only, run=a.run, port=a.port, trusted_origins=a.trust_origin)
        elif a.cmd == 'builder-skill':
            out = install_skill(hermes_home=a.hermes_home)
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
