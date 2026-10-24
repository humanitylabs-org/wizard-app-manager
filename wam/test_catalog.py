"""Explicit local private test pins. Never queried from, or uploaded to, the public index."""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import tarfile
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


def host_architecture():
    """The Docker daemon's architecture (amd64/arm64). Never raises: a manager service that can't reach Docker
    yet (fresh box before docker group membership is active, rootless socket not in its env) falls back to
    `sudo -n docker`, then to this machine's own CPU, which is the local daemon's architecture."""
    import platform
    norm = {'aarch64': 'arm64', 'x86_64': 'amd64'}
    for argv in (['docker'], ['sudo', '-n', 'docker']):
        try:
            r = subprocess.run(argv + ['info', '--format', '{{.Architecture}}'], capture_output=True, text=True,
                               timeout=20, check=True)
            if r.stdout.strip():
                return norm.get(r.stdout.strip(), r.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            continue
    m = platform.machine().lower()
    return norm.get(m, m)


def check_architecture(entry):
    """Reject incompatible pinned releases before source or asset downloads."""
    from .private import PrivateError
    arch = host_architecture()
    supported = entry.get('architectures') or []
    if supported and arch not in supported:
        raise PrivateError(f"{entry['id']}: this release runs on {', '.join(supported)}, not this Wizard ({arch}). Choose a compatible release.")
    return arch


def _loaded_identity(asset, archive, arch):
    """Bind Docker's local identity to the approved config on classic AND containerd stores.

    Docker 29's containerd store uses a manifest ID, not the config ID reported by classic
    Docker. Never accept the tag alone: verify both content-addressed blobs in the already
    checksummed archive before returning the daemon-local immutable ID to compose.
    """
    from .private import PrivateError
    try:
        r = subprocess.run(['docker', 'image', 'inspect', asset['image_id']],
                           capture_output=True, text=True, check=True)
        image = json.loads(r.stdout)[0]
        if image['Id'] != asset['image_id'] or image['Architecture'] != arch:
            raise PrivateError('loaded image identity/architecture mismatch')
        return asset
    except subprocess.CalledProcessError:
        if not asset.get('image'):
            raise PrivateError('loaded config identity not found') from None
    r = subprocess.run(['docker', 'image', 'inspect', asset['image']],
                       capture_output=True, text=True, check=True)
    image = json.loads(r.stdout)[0]
    descriptor = image.get('Descriptor') or {}
    manifest_id = descriptor.get('digest', '')
    if (not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_id)
            or image['Id'] != manifest_id or image['Architecture'] != arch):
        raise PrivateError('loaded image identity/architecture mismatch')
    expected = {manifest_id, asset['image_id']}
    blobs = {}
    wanted = {'blobs/sha256/' + key.removeprefix('sha256:'): key for key in expected}
    with tarfile.open(archive, mode='r|*') as tar:
        for member in tar:
            key = wanted.get(member.name)
            if key is None:
                continue
            if key in blobs or not member.isfile() or member.size > 4 * 1024 * 1024:
                raise PrivateError('invalid loaded image descriptor blob')
            source = tar.extractfile(member)
            if source is None:
                raise PrivateError('missing loaded image descriptor blob')
            data = source.read()
            if 'sha256:' + hashlib.sha256(data).hexdigest() != key:
                raise PrivateError('loaded image descriptor checksum mismatch')
            blobs[key] = json.loads(data)
    if set(blobs) != expected:
        raise PrivateError('loaded image descriptor missing from approved archive')
    config = blobs[asset['image_id']]
    if (blobs[manifest_id].get('config', {}).get('digest') != asset['image_id']
            or config.get('architecture') != arch or config.get('os') != 'linux'):
        raise PrivateError('loaded manifest does not bind the approved config/architecture')
    return {**asset, 'image_id': manifest_id, 'config_id': asset['image_id']}


def preload(entry, tok):
    """Download with existing GitHub authorization; verify archive AND loaded image on the target daemon."""
    from .private import PrivateError
    arch = check_architecture(entry)
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
        return _loaded_identity(asset, archive, arch)


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
