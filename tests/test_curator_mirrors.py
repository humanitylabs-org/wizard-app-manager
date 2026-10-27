import json
import subprocess
from pathlib import Path

import pytest

from wam import mirrors, packaging, private
from tools import mirror_defaults


def git(cwd, *args):
    return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    source = tmp_path / 'builder'
    source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.org')
    (source / 'server.py').write_text('print("pinned code")')
    (source / 'wizard.md').write_text('---\nid: demo\nruntime:\n  compose: compose.yml\n---\n')
    (source / 'compose.yml').write_text('services:\n  app:\n    image: python@sha256:' + 'a'*64 + '\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'pinned source')
    sha = git(source, 'rev-parse', 'HEAD')
    mirror = tmp_path / 'mirror.git'
    git(tmp_path, 'clone', '-q', '--bare', str(source), str(mirror))
    git(mirror, 'update-ref', mirrors.source_ref('gone/builder', sha), sha)
    assets = tmp_path / 'assets'
    assets.mkdir()
    git(mirror, 'bundle', 'create', str(assets / mirrors.source_name('gone/builder', sha)), mirrors.source_ref('gone/builder', sha))
    config = tmp_path / 'gitconfig'
    config.write_text(f'[url "file://{tmp_path}/unreachable"]\n    insteadOf = https://github.com/gone/builder\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setattr(mirrors, 'SOURCE_MIRROR', assets.as_uri())
    return source, mirror, sha


@pytest.mark.parametrize('package', ['source', 'build'])
def test_unreachable_origin_uses_mirror_sha_verified_then_packages(tmp_path, fixture, package):
    source, mirror, sha = fixture
    dest = tmp_path / 'checkout'
    dest.mkdir()
    (dest / 'stale').write_text('must disappear')
    private.fetch('gone/builder', sha, dest, '')
    assert git(dest, 'rev-parse', 'HEAD') == sha
    assert (dest / 'server.py').read_text() == 'print("pinned code")'
    assert not (dest / 'stale').exists()
    compose = {'services': {'app': {'build': '.'} if package == 'build' else {'image': 'python@sha256:' + '1'*64}}}
    target = packaging.source_dir(tmp_path / 'app-data', 'store', 'demo', sha)
    packaging.prepare_package(package, compose, dest, target, 'demo', sha)
    assert (target / 'server.py').read_bytes() == (source / 'server.py').read_bytes()
    assert not (target / '.git').exists()


def test_wrong_mirror_commit_fails_closed(tmp_path, fixture):
    source, mirror, sha = fixture
    wrong = 'b' * 40
    git(mirror, 'update-ref', mirrors.source_ref('gone/builder', wrong), sha)
    git(mirror, 'bundle', 'create', str(tmp_path / 'assets' / mirrors.source_name('gone/builder', wrong)), mirrors.source_ref('gone/builder', wrong))
    dest = tmp_path / 'checkout'
    with pytest.raises(private.PrivateError, match='expected'):
        private.fetch('gone/builder', wrong, dest, '')
    assert not dest.exists()


def test_origin_success_never_contacts_mirror(tmp_path, fixture, monkeypatch):
    source, mirror, sha = fixture
    config = tmp_path / 'gitconfig'
    config.write_text(f'[url "file://{source}"]\n    insteadOf = https://github.com/gone/builder\n')
    monkeypatch.setattr(mirrors, 'SOURCE_MIRROR', str(tmp_path / 'no-mirror'))
    private.fetch('gone/builder', sha, tmp_path / 'checkout', '')
    assert git(tmp_path / 'checkout', 'rev-parse', 'HEAD') == sha


def test_missing_mirror_and_invalid_pin_fail(tmp_path, fixture, monkeypatch):
    monkeypatch.setattr(mirrors, 'SOURCE_MIRROR', str(tmp_path / 'missing'))
    with pytest.raises(private.PrivateError):
        private.fetch('gone/builder', fixture[2], tmp_path / 'checkout', '')
    with pytest.raises(ValueError, match='40-hex'):
        private.fetch('gone/builder', 'main', tmp_path / 'checkout', '')


def test_image_fallback_preserves_digest_and_never_floats():
    original = 'docker.io/library/python:3@sha256:' + 'a'*64
    mirror = mirrors.image_ref(original)
    seen = []
    def probe(image):
        seen.append(image)
        return image == mirror
    compose = {'services': {'app': {'image': original}, 'tag': {'image': 'python:latest'},
                            'org': {'image': 'ghcr.io/humanitylabs-org/app@sha256:' + 'a'*64}}}
    mirrors.resolve_images(compose, probe)
    assert compose['services']['app']['image'] == mirror
    assert mirror.endswith('@sha256:' + 'a'*64)
    assert seen == [original, mirror]
    seen.clear()
    compose['services']['app']['image'] = original
    mirrors.resolve_images(compose, lambda image: seen.append(image) or True)
    assert seen == [original]
    assert compose['services']['app']['image'] == original
    compose['services']['app']['image'] = '${OWNER_IMAGE:-' + original + '}'
    mirrors.resolve_images(compose, lambda image: image == mirror)
    assert compose['services']['app']['image'] == '${OWNER_IMAGE:-' + mirror + '}'


def test_collect_keeps_channel_and_catalog_exact_pins(tmp_path):
    (tmp_path / 'releases').mkdir()
    (tmp_path / 'wam').mkdir()
    row = {'repo': 'builder/app', 'commit': 'a'*40}
    for channel in ['stable', 'latest']:
        (tmp_path / 'releases' / channel).write_text('1.0')
    (tmp_path / 'releases/1.0.json').write_text(json.dumps({'default_apps': {'slot': row}}))
    (tmp_path / 'wam/defaults.json').write_text(json.dumps({'slots': {
        'slot': {'app': 'demo', 'repo': 'builder/app'},
        'future': {'app': 'not-published', 'install': 'on-demand'}}}))
    catalog = [{'repo': 'builder/app', 'id': 'demo', 'install': {'commit': 'b'*40}}]
    pins, images, unpublished = mirror_defaults.collect(tmp_path, catalog)
    assert pins == [('builder/app', 'a'*40), ('builder/app', 'b'*40)]
    assert unpublished == ['not-published']


def test_collect_skips_slots_a_release_leaves_empty(tmp_path):
    """Releases from 0.3.60 on carry `"agent-history": null`; custody must not crash on it."""
    (tmp_path / 'releases').mkdir()
    (tmp_path / 'wam').mkdir()
    row = {'repo': 'builder/app', 'commit': 'a'*40}
    for channel in ['stable', 'latest']:
        (tmp_path / 'releases' / channel).write_text('1.0')
    (tmp_path / 'releases/1.0.json').write_text(json.dumps({'default_apps': {'slot': row, 'agent-history': None}}))
    (tmp_path / 'wam/defaults.json').write_text(json.dumps({'slots': {'slot': {'app': 'demo', 'repo': 'builder/app'}}}))
    catalog = [{'repo': 'builder/app', 'id': 'demo', 'install': {'commit': 'a'*40}}]
    pins, images, unpublished = mirror_defaults.collect(tmp_path, catalog)
    assert pins == [('builder/app', 'a'*40)]
    assert unpublished == []


def test_collect_copies_release_pinned_host_component_defaults(tmp_path):
    """The Troubleshooter default is host tooling pinned in release components, not an index app."""
    (tmp_path / 'releases').mkdir()
    (tmp_path / 'wam').mkdir()
    for channel in ['stable', 'latest']:
        (tmp_path / 'releases' / channel).write_text('1.0')
    (tmp_path / 'releases/1.0.json').write_text(json.dumps({
        'components': {'tool': {'repo': 'org/host-tool', 'commit': 'c'*40, 'branch': 'main'}},
        'default_apps': {}}))
    (tmp_path / 'wam/defaults.json').write_text(json.dumps({'slots': {
        'tool': {'app': 'host-tool', 'repo': 'org/host-tool', 'required': False}}}))
    pins, images, unpublished = mirror_defaults.collect(tmp_path, [])
    assert pins == [('org/host-tool', 'c'*40)]
    assert unpublished == []


def test_collect_still_refuses_an_unpinned_default(tmp_path):
    (tmp_path / 'releases').mkdir()
    (tmp_path / 'wam').mkdir()
    for channel in ['stable', 'latest']:
        (tmp_path / 'releases' / channel).write_text('1.0')
    (tmp_path / 'releases/1.0.json').write_text(json.dumps({
        'components': {'other': {'repo': 'org/other', 'commit': 'c'*40}}, 'default_apps': {}}))
    (tmp_path / 'wam/defaults.json').write_text(json.dumps({'slots': {'tool': {'app': 'host-tool', 'repo': 'org/host-tool'}}}))
    with pytest.raises(ValueError, match='no pinned publication'):
        mirror_defaults.collect(tmp_path, [])


def test_denied_registry_does_not_mean_missing(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args, stderr='DENIED: permission denied')
    monkeypatch.setattr(mirror_defaults, 'run', denied)
    with pytest.raises(subprocess.CalledProcessError):
        mirror_defaults.ensure_image('python@sha256:' + 'a'*64)
    assert len(calls) == 1  # no copy/overwrite after denied lookup


def test_bundle_limit_and_invalid_bundle_fail_closed(tmp_path, fixture):
    source, mirror, sha = fixture
    with pytest.raises(ValueError, match='size limit'):
        mirrors.download_bundle('gone/builder', sha, tmp_path / 'too-large', limit=1)
    (tmp_path / 'assets' / mirrors.source_name('gone/builder', sha)).write_bytes(b'not a Git bundle')
    with pytest.raises(private.PrivateError):
        private.fetch('gone/builder', sha, tmp_path / 'checkout', '')


def test_maintainer_reads_existing_bundle_without_builder(tmp_path, fixture, monkeypatch):
    source, mirror, sha = fixture
    def no_api(*a, **k):
        raise AssertionError('existing mirror must not need builder metadata or an upload')
    monkeypatch.setattr(private, '_api', no_api)
    monkeypatch.setattr(mirror_defaults, 'api_json', no_api)
    work = tmp_path / 'work'
    work.mkdir()
    images, record = mirror_defaults.ensure_source('gone/builder', sha, work, private._git_env(''),
                                                  {'id': 1}, {mirrors.source_name('gone/builder', sha): {}})
    assert images == ['python@sha256:' + 'a'*64]
    assert record['commit'] == sha
    assert git(work / (mirrors.source_name('gone/builder', sha) + '.git'), 'rev-parse', 'FETCH_HEAD') == sha


def test_new_bundle_upload_is_read_back_into_fresh_git(tmp_path, fixture, monkeypatch):
    source, mirror, sha = fixture
    (tmp_path / 'gitconfig').write_text(f'[url "file://{source}"]\n insteadOf = https://github.com/gone/builder.git\n')
    monkeypatch.setattr(private, '_api', lambda *a: {'private': False})
    uploads = []
    def upload(url, data, binary=False):
        assert binary and '/assets?name=' in url
        uploads.append(url)
        (tmp_path / 'assets' / mirrors.source_name('gone/builder', sha)).write_bytes(data)
        return {'name': mirrors.source_name('gone/builder', sha)}
    monkeypatch.setattr(mirror_defaults, 'api_json', upload)
    work = tmp_path / 'work'
    work.mkdir()
    images, record = mirror_defaults.ensure_source('gone/builder', sha, work, private._git_env(''), {'id': 1}, {})
    assert len(uploads) == 1
    assert record['commit'] == sha
    assert len(record['bundle_sha256']) == 64


def test_nonpublic_builder_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(private, '_api', lambda *a: {'private': True})
    with pytest.raises(ValueError, match='nonpublic'):
        mirror_defaults.ensure_source('gone/builder', 'a'*40, tmp_path, {}, {'id': 1}, {})


def test_release_asset_listing_is_paginated(monkeypatch):
    calls = []
    def api(url, data=None, binary=False):
        calls.append(url)
        if '/tags/' in url:
            return {'id': 1, 'draft': False, 'prerelease': True}
        if url.endswith('&page=1'):
            return [{'name': str(i)} for i in range(100)]
        return [{'name': 'last'}]
    monkeypatch.setattr(mirror_defaults, 'api_json', api)
    release, assets = mirror_defaults.source_release()
    assert len(assets) == 101
    assert len(calls) == 3
    assert 'page=2' in calls[-1]
