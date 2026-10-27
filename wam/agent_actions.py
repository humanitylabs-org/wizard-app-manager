"""Finite system-side agent operations; never execute a supplied host path."""
from __future__ import annotations
import base64
import binascii
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading

from . import builder, host_policy, links

SOURCE_LIMIT = 25 * 1024 * 1024
FILE_LIMIT = 10 * 1024 * 1024
FILE_COUNT = 4096
BODY_LIMIT = 36 * 1024 * 1024
LOCK = threading.RLock()


def backup(c, action, body):
    """Only the box's configured repository; no repo URL, key or CLI passthrough."""
    from .backup import cli
    from .backup.box import Box
    if action not in ('run', 'snapshots', 'restore', 'undo'):
        raise ValueError('unknown backup action')
    if set(body) - ({'snapshot'} if action == 'restore' else set()):
        raise ValueError('backup repository and credentials are administrator-configured')
    argv = [action]
    if action == 'restore':
        snap = body.get('snapshot', 'latest')
        if not isinstance(snap, str) or not re.fullmatch(r'latest|[0-9a-f]{8,64}', snap):
            raise ValueError('invalid snapshot id')
        argv = ['import', '--from-repo', snap, '--yes']
    box = Box(home=c.home, runtipi_root=c.runtipi_root)
    # Never discover/execute the alongside agent's writable Hermes as wizard-system.
    # Separated backups cover manager-owned apps/data/files; Hermes remains at its uid.
    from .backup.crypto import BackupCryptoError
    with LOCK:
        try:
            return cli._dispatch(cli._parser().parse_args(argv), box)
        except BackupCryptoError as exc:
            raise ValueError(str(exc)) from None


def unpack_source(files, target):
    """A data-only upload, not tar extraction or a wizard-system read of an agent path."""
    if not isinstance(files, list) or not 1 <= len(files) <= FILE_COUNT:
        raise ValueError('source must contain 1–4096 regular files')
    total, seen = 0, set()
    for row in files:
        if not isinstance(row, dict) or set(row) != {'path', 'data'}:
            raise ValueError('source accepts regular file bytes only (no links or modes)')
        name, data = row['path'], row['data']
        if not isinstance(name, str) or not isinstance(data, str) or len(name) > 512:
            raise ValueError('invalid source entry')
        rel = PurePosixPath(name)
        if (not name or '\\' in name or '\x00' in name or rel.is_absolute()
                or any(p in ('', '.', '..') for p in name.split('/')) or name in seen):
            raise ValueError('source path escapes snapshot or is duplicated')
        if any(p in builder.SECRET_FILES or p.endswith(('.pem', '.key')) for p in rel.parts):
            raise ValueError('potential secret in source')
        if len(data) > (FILE_LIMIT + 2) // 3 * 4:
            raise ValueError('file exceeds 10 MiB')
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('invalid source encoding') from None
        total += len(raw)
        if len(raw) > FILE_LIMIT or total > SOURCE_LIMIT:
            raise ValueError('source exceeds 25 MiB or file exceeds 10 MiB')
        seen.add(name)
        dest = target.joinpath(*rel.parts)
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with dest.open('xb') as out:
            out.write(raw)
        dest.chmod(0o600)


def build(c, action, body):
    if action not in ('install-local', 'update-local', 'check'):
        raise ValueError('unsupported builder action')
    if set(body) - {'files', 'run', 'port', 'trusted_origins'}:
        raise ValueError('unknown builder option')
    run = body.get('run', False)
    port = body.get('port', 18799)
    if not isinstance(run, bool) or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid builder run/port')
    origins = body.get('trusted_origins', [])
    if not isinstance(origins, list) or not all(isinstance(s, str) for s in origins):
        raise ValueError('invalid trusted origins')
    # c.home and its ancestors belong to wizard-system, never the agent.
    work = c.home / 'build-work'
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    work.chmod(0o700)
    with LOCK, tempfile.TemporaryDirectory(prefix='source-', dir=work) as temp:
        root = Path(temp)
        unpack_source(body.get('files'), root)
        doc = builder.wizardmd.front_matter((root / 'wizard.md').read_text())
        app_id = builder._id(str(doc.get('id', '')))
        # Check supplied recipes as well as the generated execution compose. The
        # builder must not silently discard dangerous runtime.compose overrides.
        texts = []
        for p in builder.source_files(root):
            if p.name in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
                texts.append(p.read_text())
        runtime = doc.get('runtime') or {}
        compose = runtime.get('compose') if isinstance(runtime, dict) else None
        if compose:
            rel = PurePosixPath(compose)
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('runtime.compose escapes source')
            texts.append((root / compose).read_text())
        if texts:
            host_policy.require(c.home, app_id, texts, base=root,
                                app_data=c.home / 'staged-apps' / app_id / 'data')
        if action == 'check':
            return builder.check_app(root, proposal=True)
        if action == 'install-local':
            return builder.install_local(root, os_home=c.home, stage_only=True, run=run,
                                         port=port, trusted_origins=origins)
        return builder.update_local(root, os_home=c.home, run=run)


def private_link(c, reg, app_id, publish):
    """Publish exactly one installed app's own port; never arbitrary Serve arguments."""
    page = next((p for p in links.pages(reg) if p['id'] == app_id), None)
    if page is None:
        raise ValueError('installed running app with a loopback page required')
    ts = links.tailnet()  # executed only by the service account / operator
    result = {'app': app_id, 'provider': 'tailscale', 'state': ts['state'],
              'operator_uid': os.getuid(), 'published': False}
    if ts['state'] != 'Running' or not ts['host']:
        return {**result, 'note': 'Tailscale is not connected. Ask the owner to sign in; no private link was changed.'}
    port, target = page['port'], f"http://127.0.0.1:{page['port']}"
    with LOCK:
        config = links.serve_status()
        status = links.route_status(config, ts['host'], port, '/', target)
        if status == 'conflict':
            raise ValueError('existing private route differs; left untouched')
        routes = [(port, '/', target)] if publish else []
        if not publish:
            for authority, site in (config.get('Web') or {}).items():
                host, _, served_port = authority.rpartition(':')
                if host != ts['host'] or not served_port.isdigit():
                    continue
                for path, handler in (site.get('Handlers') or {}).items():
                    proxy = handler.get('Proxy', '')
                    if proxy == target or proxy.startswith(target + '/'):
                        routes.append((int(served_port), path, proxy))
        for served_port, path, proxy in routes:
            status = links.route_status(links.serve_status(), ts['host'], served_port, path, proxy)
            if status == 'conflict':
                raise ValueError('existing private route differs; left untouched')
            if (publish and status == 'missing') or (not publish and status == 'match'):
                cmd = ['tailscale', 'serve', '--bg', f'--https={served_port}', f'--set-path={path}']
                p = links._run(cmd + ([proxy] if publish else ['off']), 60)
                if p.returncode:
                    raise ValueError((p.stderr or p.stdout).strip()[:200] or 'Tailscale Serve failed')
            expected = 'match' if publish else 'missing'
            if links.route_status(links.serve_status(), ts['host'], served_port, path, proxy) != expected:
                raise ValueError('private route readback differs')
        # Do not change the automatic shell/owner routes or any other app link.
        saved = c.home / 'links.json'
        state = json.loads(saved.read_text()) if saved.exists() else {'links': []}
        state['links'] = [p for p in state.get('links', []) if p.get('id') != app_id]
        suppressed = set(state.get('unpublished', []))
        suppressed.discard(app_id) if publish else suppressed.add(app_id)
        state['unpublished'] = sorted(suppressed)
        if publish:
            state['links'].append({**page, 'url': f"https://{ts['host']}:{port}{page['path']}"})
        builder._json(saved, state)
        return {**result, 'published': publish, 'url': f"https://{ts['host']}:{port}{page['path']}" if publish else None}
