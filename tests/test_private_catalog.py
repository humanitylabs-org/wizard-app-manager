import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from wam import test_catalog as cat, cli, private


def entry():
    return dict(id='test-app', repo='example/test-app', commit='a'*40, architectures=['arm64'],
                gateway={'path':'/test-app/','strip_prefix':True})


def test_catalog_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv('WAM_PRIVATE_CATALOG', raising=False)
    assert cat.load() == []
    p=tmp_path/'catalog.json'; p.write_text(json.dumps({'schema':1,'scope':'private-test','apps':[entry()]}))
    monkeypatch.setenv('WAM_PRIVATE_CATALOG',str(p))
    assert cat.resolve('test-app')['commit']=='a'*40
    assert cat.resolve('example/test-app')['id']=='test-app'
    assert cat.resolve('other') is None


@pytest.mark.parametrize('field,value',[('commit','main'),('repo','../invalid'),('id','../escape')])
def test_reject_malformed(tmp_path,field,value):
    a=entry(); a[field]=value
    p=tmp_path/'c'; p.write_text(json.dumps({'schema':1,'scope':'private-test','apps':[a]}))
    with pytest.raises(ValueError): cat.load(p)


def test_pin_never_hits_public_index(tmp_path):
    a=entry()
    with patch.object(cat,'resolve',return_value=a), patch.object(private,'prepare',return_value={'id':'test-app'}) as prep, patch.object(cli.index,'resolve',side_effect=AssertionError('private leak')):
        assert cli.resolve_any(SimpleNamespace(runtipi_root=tmp_path,index='unused'),'test-app')['id']=='test-app'
        assert prep.call_args.kwargs['release']==a


def test_gateway_has_no_listener():
    out=cat.gateway_fragment(entry(),19285)
    assert 'handle_path /test-app/*' in out
    assert '127.0.0.1:19285' in out
    assert 'flush_interval -1' in out
    with pytest.raises(ValueError): cat.gateway_fragment(entry(),80)


def test_architecture_fails_before_download():
    with patch.object(cat.subprocess,'run',return_value=SimpleNamespace(stdout='x86_64\n')) as run:
        with pytest.raises(private.PrivateError,match='does not support'): cat.preload(entry(),'not-a-token')
        assert run.call_count==1


def test_checksum_fails_before_docker_load(tmp_path):
    a=entry(); a.update(tag='v1',images={'arm64':{'asset':'image.tar.gz','sha256':'0'*64}})
    def run(args,**kwargs):
        if args[:2]==['docker','info']: return SimpleNamespace(stdout='aarch64\n')
        if args[:3]==['gh','release','download']:
            Path(args[-1],'image.tar.gz').write_bytes(b'corrupt')
            return SimpleNamespace(returncode=0)
        raise AssertionError('must not load a corrupt archive')
    with patch.object(cat.subprocess,'run',side_effect=run):
        with pytest.raises(private.PrivateError,match='checksum mismatch'): cat.preload(a,'not-a-token')


def test_archived_tags_need_not_match_compose_tag():
    import hashlib
    a=entry(); identity='sha256:'+'1'*64
    a.update(tag='v1',images={'arm64':{'asset':'image.tar.gz','sha256':hashlib.sha256(b'archive').hexdigest(),'image':'example/app:1','image_id':identity}})
    def run(args,**kwargs):
        if args[:2]==['docker','info']: return SimpleNamespace(stdout='aarch64\n')
        if args[:3]==['gh','release','download']:
            Path(args[-1],'image.tar.gz').write_bytes(b'archive'); return SimpleNamespace(returncode=0)
        if args[:2]==['docker','load']: return SimpleNamespace(returncode=0)
        assert args==['docker','image','inspect',identity]
        return SimpleNamespace(stdout=json.dumps([{'Id':identity,'Architecture':'arm64'}]))
    with patch.object(cat.subprocess,'run',side_effect=run):
        assert cat.preload(a,'not-a-token')['image_id']==identity

