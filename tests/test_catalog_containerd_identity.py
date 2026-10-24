"""Docker 29/containerd identity normalization must not turn tags into trust anchors."""
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from wam import test_catalog as cat, private


def fixture(tmp_path, mode='valid'):
    cfg = json.dumps({'architecture':'amd64' if mode == 'wrong-config-arch' else 'arm64',
                      'os':'windows' if mode == 'wrong-os' else 'linux'}).encode()
    config_id = 'sha256:' + hashlib.sha256(cfg).hexdigest()
    manifest = json.dumps({'schemaVersion':2,'config':{'digest':'sha256:'+'0'*64 if mode == 'wrong-link' else config_id}}).encode()
    manifest_id = 'sha256:' + hashlib.sha256(manifest).hexdigest()
    archive = tmp_path/'image.tar.gz'
    def add(tar, key, data, link=False):
        item = tarfile.TarInfo('blobs/sha256/' + key.removeprefix('sha256:'))
        if link:
            item.type = tarfile.SYMTYPE; item.linkname = '/untrusted-path'
        else:
            item.size = len(data)
        tar.addfile(item, None if link else io.BytesIO(data))
    with tarfile.open(archive, 'w:gz') as tar:
        add(tar, manifest_id, manifest)
        if mode != 'missing-config':
            add(tar, config_id, cfg+b' ' if mode == 'corrupt-config' else
                b' '*(4*1024*1024+1) if mode == 'oversize' else cfg, mode == 'symlink')
        if mode == 'duplicate': add(tar, config_id, cfg)
    asset = {'image_id':config_id,'image':'example/app:v1','sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'asset':archive.name}
    image = {'Id': 'sha256:'+'2'*64 if mode == 'wrong-runtime-id' else manifest_id,
             'Architecture':'amd64' if mode == 'wrong-runtime-arch' else 'arm64',
             'Descriptor': {'digest':manifest_id}}
    return archive, asset, image


def inspect(asset, image):
    def run(args, **kwargs):
        if args == ['docker','image','inspect',asset['image_id']]:
            raise subprocess.CalledProcessError(1,args)
        assert args == ['docker','image','inspect',asset['image']]
        return SimpleNamespace(stdout=json.dumps([image]))
    return run


def test_containerd_manifest_is_bound_to_approved_config(tmp_path):
    archive, asset, image = fixture(tmp_path)
    with patch.object(cat.subprocess,'run',side_effect=inspect(asset,image)):
        loaded = cat._loaded_identity(asset,archive,'arm64')
    assert loaded['image_id'] == image['Id']
    assert loaded['config_id'] == asset['image_id']
    assert asset['image_id'] != loaded['image_id']  # Catalog stays engine-neutral, immutable.


@pytest.mark.parametrize('mode', ['wrong-link','wrong-config-arch','wrong-os','missing-config',
    'corrupt-config','duplicate','symlink','oversize','wrong-runtime-id','wrong-runtime-arch'])
def test_containerd_rejects_unbound_or_malformed_content(tmp_path, mode):
    archive, asset, image = fixture(tmp_path,mode)
    with patch.object(cat.subprocess,'run',side_effect=inspect(asset,image)):
        with pytest.raises(private.PrivateError): cat._loaded_identity(asset,archive,'arm64')


def test_preload_keeps_archive_until_identity_is_verified(tmp_path):
    archive, asset, image = fixture(tmp_path)
    entry = {'id':'app','repo':'example/app','tag':'v1','architectures':['arm64'],'images':{'arm64':asset}}
    def run(args,**kwargs):
        if args[:2] == ['docker','info']: return SimpleNamespace(stdout='aarch64\n')
        if args[:3] == ['gh','release','download']:
            Path(args[-1],asset['asset']).write_bytes(archive.read_bytes())
            return SimpleNamespace(returncode=0)
        if args[:2] == ['docker','load']: return SimpleNamespace(returncode=0)
        return inspect(asset,image)(args,**kwargs)
    with patch.object(cat.subprocess,'run',side_effect=run):
        assert cat.preload(entry,'test-no-secret')['image_id'] == image['Id']


def test_classic_mismatch_does_not_fall_back_to_tag(tmp_path):
    archive, asset, image = fixture(tmp_path)
    with patch.object(cat.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps([image]))) as run:
        with pytest.raises(private.PrivateError): cat._loaded_identity(asset,archive,'arm64')
        assert run.call_count == 1
