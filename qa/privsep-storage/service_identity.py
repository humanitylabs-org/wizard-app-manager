#!/usr/bin/python3
"""QA-only config/content identity across classic and containerd image stores.

An engine's image handle is not the portable config digest. Never choose an
unbound loaded tag: attest the saved config and every uncompressed layer first.
No extraction, image builds, runtime privilege changes, or product policy edits.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess
import tarfile
import zlib

MAX_ARCHIVE = 2147483648
MAX_METADATA = 1048576


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def archive_identity(stream):
    """Inspect Docker-save streaming metadata + ALL layer bytes; bounded memory.

    Docker compatibility manifests can reference raw or gzip OCI layer blobs.
    Their uncompressed digests must match the exact config's diff_ids.
    """
    hashes = {}; small = {}; total = 0; small_total = 0
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        seen = set()
        for member in archive:
            name = member.name
            assert (name not in seen and not name.startswith('/') and
                    '..' not in PurePosixPath(name).parts), 'unsafe image archive membership'
            seen.add(name)
            if member.isdir(): continue
            assert member.isfile() and not member.sparse, 'unsupported image member'
            total += member.size
            assert total <= MAX_ARCHIVE and len(seen) <= 4096, 'image export bound'
            f = archive.extractfile(member); assert f is not None
            h = hashlib.sha256(); parts = []; decoder = None; expanded = 0; first = True
            while True:
                raw = f.read(1048576)
                if not raw: break
                if member.size <= MAX_METADATA: parts.append(raw)
                if first:
                    decoder = zlib.decompressobj(31) if raw.startswith(b'\x1f\x8b') else None
                    first = False
                pending = raw
                while pending:
                    plain = decoder.decompress(pending, 1048576) if decoder else pending
                    h.update(plain); expanded += len(plain)
                    assert expanded <= MAX_ARCHIVE, 'expanded layer bound'
                    pending = decoder.unconsumed_tail if decoder else b''
            if decoder:
                assert decoder.eof and not decoder.unused_data, 'invalid compressed layer'
            hashes[name] = h.hexdigest()
            if member.size <= MAX_METADATA:
                small_total += member.size
                assert small_total <= 16777216, 'image metadata bound'
                small[name] = b''.join(parts)
    manifest = json.loads(small['manifest.json'])
    assert type(manifest) is list and len(manifest) == 1, 'ambiguous image manifest'
    row = manifest[0]
    config = small[row['Config']]; doc = json.loads(config)
    layers = row['Layers']
    assert type(layers) is list and layers and len(layers) == len(set(layers))
    diff_ids = ['sha256:' + hashes[name] for name in layers]
    assert doc['rootfs'] == {'type':'layers', 'diff_ids':diff_ids}, 'image layer/config disagreement'
    return {'config_sha256':sha(config), 'platform':{k:doc.get(k) for k in ('os','architecture','variant')},
            'labels':doc['config'].get('Labels') or {}, 'rootfs':doc['rootfs'],
            'repo_tags':row.get('RepoTags') or []}


def bind_archive(actual, accepted, *, tag):
    for key in ('config_sha256','platform','labels','rootfs'):
        assert actual[key] == accepted[key], 'saved image identity differs: ' + key
    assert tag in actual['repo_tags'], 'saved image tag not bound to accepted config'
    assert actual['platform'] == {'os':'linux','architecture':'amd64','variant':None}


def bind_inspect(info, accepted):
    assert re.fullmatch('sha256:[0-9a-f]{64}', info['Id']), 'invalid engine image handle'
    assert info['Os'] == accepted['platform']['os'] and info['Architecture'] == accepted['platform']['architecture']
    assert (info.get('Variant') or None) == accepted['platform']['variant']
    assert (info['Config'].get('Labels') or {}) == accepted['labels']
    assert info['RootFS'] == {'Type':'layers','Layers':accepted['rootfs']['diff_ids']}
    # Id is used only AFTER exact config + complete layer export attestation.
    # A descriptor-derived engine Id is recorded separately, never relabelled as config.
    return {'engine_image_id':info['Id'], 'config_digest':'sha256:'+accepted['config_sha256'],
            'descriptor':info.get('Descriptor'), 'platform':accepted['platform'],
            'labels':accepted['labels'], 'rootfs':accepted['rootfs']}


def attest_loaded(accepted, tag):
    """Tag is a locator only: re-export it and prove the accepted config/layers."""
    info = json.loads(subprocess.check_output(['docker','image','inspect',tag], timeout=30))
    assert len(info) == 1
    process = subprocess.Popen(['timeout','--signal=KILL','180','docker','image','save',tag], stdout=subprocess.PIPE)
    assert process.stdout is not None
    try:
        actual = archive_identity(process.stdout)
        assert process.wait(timeout=60) == 0, 'image export failed'
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=30)
        process.stdout.close()
    bind_archive(actual, accepted, tag=tag)
    result = bind_inspect(info[0], accepted)
    # The locator must not change between export and use.
    again = json.loads(subprocess.check_output(['docker','image','inspect',tag], timeout=30))
    assert len(again) == 1 and again[0]['Id'] == result['engine_image_id']
    bind_inspect(again[0], accepted)
    return result


if __name__ == '__main__':
    import sys
    from pathlib import Path
    accepted = json.loads(Path(sys.argv[1]).read_text())
    tag = accepted['manifest'][0]['RepoTags'][0]
    value = attest_loaded(accepted, tag)
    subprocess.run(['docker','tag',value['engine_image_id'],'privsep-wam:595523fb'], check=True, timeout=30)
    tagged = json.loads(subprocess.check_output(['docker','image','inspect','privsep-wam:595523fb'],timeout=30))
    assert len(tagged) == 1 and bind_inspect(tagged[0],accepted)['engine_image_id'] == value['engine_image_id']
    Path(sys.argv[2]).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
