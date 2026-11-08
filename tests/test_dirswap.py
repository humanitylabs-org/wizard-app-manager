"""Updating an app whose old store folder holds files this user can't delete (Runtipi, running as root, rewrites
repos/wizardapps/apps/<id>) must still land the new recipe. Before: rmtree(ignore_errors) left the files and the
rename failed with `[Errno 39] Directory not empty: .../apps/notifications.tmp-<pid> -> .../apps/notifications`."""
import errno
import json
import os
import shutil

import pytest

from wam import dirswap, private

PIN = 'b' * 40


def locked(d):
    """A subfolder whose entries can't be deleted (as with root-owned files, without needing root)."""
    sub = d / 'metadata'
    sub.mkdir(parents=True, exist_ok=True)
    (sub / 'description.md').write_text('old\n')
    sub.chmod(0o555)
    return sub


def unlock(root):
    for p in [root, *root.rglob('*')]:
        if p.is_dir() and not p.is_symlink():
            p.chmod(0o755)


@pytest.fixture
def undeletable(tmp_path):
    probe = tmp_path / 'probe'
    sub = locked(probe)
    shutil.rmtree(probe, ignore_errors=True)
    if not (sub / 'description.md').exists():
        unlock(tmp_path)
        pytest.skip('running as root: chmod cannot make a file undeletable')
    unlock(tmp_path)
    shutil.rmtree(probe)
    yield
    unlock(tmp_path)


def test_update_over_undeletable_old_store_folder(tmp_path, monkeypatch, undeletable):
    def fetch(repo, commit, dst, tok):
        dst.mkdir(parents=True)
        (dst / 'wizard.md').write_text('---\nid: sample\nname: Sample\nversion: 2\nruntime:\n  config: config.json\n'
                                       '  compose: docker-compose.yml\n---\nSample\n')
        (dst / 'config.json').write_text(json.dumps({'id': 'sample', 'port': 9000}))
        (dst / 'docker-compose.yml').write_text('services:\n  sample:\n    image: example/sample:2\n')

    monkeypatch.setattr(private, 'fetch', fetch)
    apps = tmp_path / 'repos/wizardapps/apps'
    old = apps / 'sample'
    old.mkdir(parents=True)
    (old / '.wizard-commit').write_text('a' * 40 + '\n')
    (old / 'config.json').write_text(json.dumps({'id': 'sample', 'tipi_version': 3}))
    locked(old)

    private.prepare(tmp_path, 'wizardapps', 'org/sample', tok='test',
                    source_pin={'id': 'sample', 'repo': 'org/sample', 'commit': PIN})

    assert (old / '.wizard-commit').read_text().strip() == PIN
    assert json.loads((old / 'config.json').read_text())['tipi_version'] == 4
    assert (old / 'metadata/description.md').read_text().startswith('# Sample')
    assert not list(apps.glob('sample.tmp-*'))
    left = [p.name for p in apps.iterdir() if p.name != 'sample']
    assert all(n.startswith('.sample.old-') for n in left)   # only a hidden, never-read leftover


def test_replace_dir_rolls_back_when_new_cannot_land(tmp_path, monkeypatch):
    dst, new = tmp_path / 'app', tmp_path / 'app.tmp-1'
    dst.mkdir()
    (dst / 'v').write_text('old')
    new.mkdir()
    (new / 'v').write_text('new')
    real = os.rename

    def rename(a, b):
        if str(a) == str(new):
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        return real(a, b)

    monkeypatch.setattr(dirswap.os, 'rename', rename)
    with pytest.raises(OSError):
        dirswap.replace_dir(new, dst)
    assert (dst / 'v').read_text() == 'old'
    assert (new / 'v').read_text() == 'new'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['app', 'app.tmp-1']


def test_replace_dir_names_path_and_owner_when_old_cannot_move(tmp_path, monkeypatch):
    dst, new = tmp_path / 'app', tmp_path / 'app.tmp-1'
    dst.mkdir()
    new.mkdir()
    real = os.rename

    def rename(a, b):
        if str(a) == str(dst):
            raise PermissionError(errno.EACCES, 'Permission denied')
        return real(a, b)

    monkeypatch.setattr(dirswap.os, 'rename', rename)
    with pytest.raises(dirswap.ReplaceError) as e:
        dirswap.replace_dir(new, dst)
    msg = str(e.value)
    assert str(dst) in msg and 'moving it aside failed' in msg and 'uid' in msg
    assert isinstance(e.value, OSError)   # callers' existing OSError handling reports it
    assert dst.is_dir() and new.is_dir()


def test_replace_dir_fresh_target_and_stale_aside_sweep(tmp_path, monkeypatch):
    new = tmp_path / 'app.tmp-1'
    new.mkdir()
    dirswap.replace_dir(new, tmp_path / 'app')
    assert (tmp_path / 'app').is_dir() and not new.exists()
    stale = tmp_path / '.app.old-999'
    stale.mkdir()
    new.mkdir()
    monkeypatch.setattr(dirswap, 'STALE_SECONDS', -1)
    dirswap.replace_dir(new, tmp_path / 'app')
    assert sorted(p.name for p in tmp_path.iterdir()) == ['app']
