"""Explicit local private test pins. Never queried from, or uploaded to, the public index."""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def load(path=None):
    path = path or os.environ.get('WAM_PRIVATE_CATALOG')
    if not path:
        return []
    doc = json.loads(Path(path).read_text())
    if doc.get('schema') != 1 or doc.get('scope') != 'private-test':
        raise ValueError('invalid private test catalog')
    rows = doc['apps']
    seen = set()
    for a in rows:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}', a['id']) or a['id'] in seen:
            raise ValueError('invalid or duplicate app id')
        seen.add(a['id'])
        if not REPO.fullmatch(a['repo']) or not SHA.fullmatch(a['commit']):
            raise ValueError('private source must use owner/repo and a full commit')
        for arch, asset in a.get('images', {}).items():
            if arch not in ('amd64', 'arm64') or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256']):
                raise ValueError('invalid archive architecture/checksum')
            if not re.fullmatch(r'[A-Za-z0-9._-]+', asset['asset']) or not re.fullmatch(r'[A-Za-z0-9._-]+', a['tag']):
                raise ValueError('invalid release asset/tag')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['image_id']):
                raise ValueError('missing immutable image identity')
    return rows


def resolve(ref):
    return next((a for a in load() if ref in (a['id'], a['repo'], 'https://github.com/' + a['repo'])), None)


def preload(entry, tok):
    """Download with existing GitHub authorization; verify archive AND loaded image on the target daemon."""
    from .private import PrivateError
    r = subprocess.run(['docker', 'info', '--format', '{{.Architecture}}'], capture_output=True, text=True, check=True)
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(r.stdout.strip(), r.stdout.strip())
    if arch not in entry['architectures']:
        raise PrivateError(f"{entry['id']}: release does not support {arch}")
    if not entry.get('images'):
        return
    asset = entry['images'].get(arch)
    if not asset:
        raise PrivateError(f"{entry['id']}: no verified image archive for {arch}")
    with tempfile.TemporaryDirectory(prefix='wam-private-') as tmp:
        r = subprocess.run(['gh', 'release', 'download', entry['tag'], '--repo', entry['repo'],
                            '--pattern', asset['asset'], '--dir', tmp],
                           env={**os.environ, 'GH_TOKEN': tok}, capture_output=True, text=True, timeout=900)
        if r.returncode:
            raise PrivateError(f"{entry['id']}: private release download failed; repository read access required")
        archive = Path(tmp) / asset['asset']
        with archive.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        if digest != asset['sha256']:
            raise PrivateError('release archive checksum mismatch; refusing docker load')
        subprocess.run(['docker', 'load', '-i', str(archive)], capture_output=True, check=True, timeout=900)
    r = subprocess.run(['docker', 'image', 'inspect', asset['image']], capture_output=True, text=True, check=True)
    image = json.loads(r.stdout)[0]
    if image['Id'] != asset['image_id'] or image['Architecture'] != arch:
        raise PrivateError('loaded image identity/architecture mismatch')
    return asset


def gateway_fragment(entry, port):
    """A fragment for an EXISTING authenticated Caddy site, never a new public listener."""
    prefix = entry['gateway']['path']
    if not re.fullmatch(r'/[a-z0-9-]+/', prefix) or not 1024 <= int(port) <= 65535:
        raise ValueError('invalid gateway path/port')
    mode = 'handle_path' if entry['gateway']['strip_prefix'] else 'handle'
    return (f'# Inside the existing authenticated site only; preserve its auth gate.\n'
            f'{mode} {prefix}* {{\n  reverse_proxy 127.0.0.1:{int(port)} {{\n    flush_interval -1\n  }}\n}}\n')


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['list', 'gateway'])
    p.add_argument('id', nargs='?')
    p.add_argument('--port', type=int)
    a = p.parse_args()
    if a.command == 'list':
        print(json.dumps(load(), indent=2))
    else:
        entry = resolve(a.id)
        if not entry or not a.port:
            p.error('known catalog id and --port required')
        print(gateway_fragment(entry, a.port), end='')


if __name__ == '__main__':
    main()
