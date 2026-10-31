#!/usr/bin/env python3
"""Staging-only: seal the three non-product images BY RECORDED DIGEST as OCI archives.

The health-capture export recorded the exact image IDs the real alongside
install used (containerd store: ID == registry index digest). This fetches
each index BY THAT DIGEST (never a tag), verifies every byte against its
digest, selects the single linux/amd64 manifest, and writes a deterministic
OCI image-layout tar whose index.json names that manifest with the familiar
reference annotation (so Compose resolves it locally, no pull). The original
index blob is kept inside the archive so the guest can re-verify the chain
index-digest -> amd64 manifest digest -> config/layers before loading.

Writes only under the given output directory. No Docker, no VM.
"""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import urllib.request

IMAGES = {   # name: (registry, repository, recorded index digest, familiar ref, token URL)
    'postgres': ('registry-1.docker.io', 'library/postgres',
                 '0472f5295d05c211cc42b3daa5e84a128e390c5267b60ced9fe044bcf4edfdb8', 'docker.io/library/postgres:14',
                 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/postgres:pull'),
    'rabbitmq': ('registry-1.docker.io', 'library/rabbitmq',
                 '21577d148f4f7563a87d761862fa353f59348a094833ca8b1b1a46a8102dffce', 'docker.io/library/rabbitmq:4-alpine',
                 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/rabbitmq:pull'),
    'upstream': ('ghcr.io', 'runtipi/runtipi',
                 '8a72852cec1f46baebc86e6aa755e753c82624f9df708939ed51f80ff9b508e0', 'ghcr.io/runtipi/runtipi:v4.11.0',
                 'https://ghcr.io/token?scope=repository:runtipi/runtipi:pull'),
}
INDEX_TYPES = ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json')
MANIFEST_TYPES = ('application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json')
MAX_BLOB = 2147483648
MAX_TOTAL = 3221225472


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def get(url, headers, cap):
    req = urllib.request.Request(url, headers={'User-Agent': 'privsep-manager-start-staging', **headers})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read(cap + 1)
    assert len(raw) <= cap, 'bounded fetch: ' + url
    return raw


def blob_to(path, url, headers, digest, size):
    assert 0 < size <= MAX_BLOB
    h = hashlib.sha256(); n = 0
    req = urllib.request.Request(url, headers={'User-Agent': 'privsep-manager-start-staging', **headers})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, 'wb') as f:
        while True:
            raw = r.read(1048576)
            if not raw:
                break
            n += len(raw); assert n <= size, 'blob larger than descriptor'
            h.update(raw); f.write(raw)
    assert n == size and h.hexdigest() == digest, 'blob digest/size mismatch'


def info(tarinfo_name, size):
    t = tarfile.TarInfo(tarinfo_name); t.size = size; t.mtime = 0; t.uid = t.gid = 0
    t.uname = t.gname = ''; t.mode = 0o644
    return t


def build(name, out, tmp):
    registry, repo, index_digest, ref, token_url = IMAGES[name]
    tok = json.loads(get(token_url, {}, 65536))
    auth = {'Authorization': 'Bearer ' + (tok.get('token') or tok['access_token'])}
    base = 'https://' + registry + '/v2/' + repo
    index_raw = get(base + '/manifests/sha256:' + index_digest, {**auth, 'Accept': ', '.join(INDEX_TYPES)}, 1048576)
    assert sha(index_raw) == index_digest, 'index digest mismatch'
    index = json.loads(index_raw)
    assert index.get('mediaType') in INDEX_TYPES
    amd = [m for m in index['manifests'] if (m.get('platform') or {}).get('os') == 'linux'
           and m['platform'].get('architecture') == 'amd64' and not m['platform'].get('variant')]
    assert len(amd) == 1, 'exactly one linux/amd64 manifest'
    desc = amd[0]; mdig = desc['digest'].split(':', 1)[1]
    assert desc['mediaType'] in MANIFEST_TYPES
    man_raw = get(base + '/manifests/sha256:' + mdig, {**auth, 'Accept': desc['mediaType']}, 1048576)
    assert sha(man_raw) == mdig and len(man_raw) == desc['size'], 'manifest digest mismatch'
    man = json.loads(man_raw)
    assert man.get('mediaType', desc['mediaType']) == desc['mediaType']
    cfg = man['config']; layers = man['layers']
    blobs = {}
    total = 0
    for d in [cfg] + layers:
        dig = d['digest'].split(':', 1)[1]
        assert d['digest'] == 'sha256:' + dig and len(dig) == 64
        if dig in blobs:
            continue
        p = tmp / dig
        blob_to(p, base + '/blobs/sha256:' + dig, auth, dig, d['size'])
        blobs[dig] = p; total += d['size']
        assert total <= MAX_TOTAL
    config = json.loads(blobs[cfg['digest'].split(':', 1)[1]].read_bytes())
    assert config.get('architecture') == 'amd64' and config.get('os') == 'linux'
    familiar = ref.rsplit(':', 1)[1]
    layout_index = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                    'manifests': [{'mediaType': desc['mediaType'], 'digest': desc['digest'], 'size': desc['size'],
                                   'platform': {'architecture': 'amd64', 'os': 'linux'},
                                   'annotations': {'io.containerd.image.name': ref,
                                                   'org.opencontainers.image.ref.name': familiar}}]}
    li_raw = (json.dumps(layout_index, sort_keys=True, separators=(',', ':')) + '\n').encode()
    archive = out / ('image-' + name + '.tar')
    small = {'oci-layout': b'{"imageLayoutVersion":"1.0.0"}\n', 'index.json': li_raw,
             'blobs/sha256/' + index_digest: index_raw, 'blobs/sha256/' + mdig: man_raw}
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as t:
        for d in ('blobs', 'blobs/sha256'):
            ti = tarfile.TarInfo(d); ti.type = tarfile.DIRTYPE; ti.mode = 0o755; ti.mtime = 0
            t.addfile(ti)
        for n in sorted(small):
            t.addfile(info(n, len(small[n])), io.BytesIO(small[n]))
        for dig in sorted(blobs):
            with open(blobs[dig], 'rb') as f:
                t.addfile(info('blobs/sha256/' + dig, blobs[dig].stat().st_size), f)
    for p in blobs.values():
        p.unlink()
    h = hashlib.sha256()
    with open(archive, 'rb') as f:
        for raw in iter(lambda: f.read(1048576), b''):
            h.update(raw)
    archive.chmod(0o600)
    return {'name': name, 'ref': ref, 'registry': registry, 'repository': repo,
            'recorded_index_digest': 'sha256:' + index_digest, 'index_sha256_verified': True,
            'amd64_manifest_digest': desc['digest'], 'manifest_media_type': desc['mediaType'],
            'config_digest': cfg['digest'], 'layers': [{'digest': d['digest'], 'size': d['size']} for d in layers],
            'layout_index_sha256': sha(li_raw), 'archive': archive.name, 'archive_sha256': h.hexdigest(),
            'archive_bytes': archive.stat().st_size}


def main(out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    rows = {}
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        for name in IMAGES:
            rows[name] = build(name, out, Path(tmp))
    doc = {'schema': 'privsep-manager-start-images-v1',
           'source': 'health-capture export images section (heavy_fb99f9f1, f8fd700e stage)',
           'binding': 'recorded index digest -> single linux/amd64 manifest -> config/layers, all sha256-verified',
           'images': rows}
    (out / 'images.json').write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n'); (out / 'images.json').chmod(0o600)
    print(json.dumps({n: {k: r[k] for k in ('amd64_manifest_digest', 'archive_sha256', 'archive_bytes')}
                      for n, r in rows.items()}, sort_keys=True, indent=1))


if __name__ == '__main__':
    main(sys.argv[1])
