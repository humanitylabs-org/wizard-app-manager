"""Shared backend/CLI recipe cache publication regressions."""
from pathlib import Path
from unittest.mock import patch
import pytest
from wam import private


def trees(root):
    dst, tmp = root / 'files', root / 'files.tmp'
    for d, text in ((dst, 'old'), (tmp, 'new')):
        (d / 'metadata').mkdir(parents=True)
        (d / 'metadata/description.md').write_text(text)
    return tmp, dst


def test_nonempty_recipe_replaced(tmp_path):
    tmp, dst = trees(tmp_path)
    private.replace_recipe(tmp, dst)
    assert (dst / 'metadata/description.md').read_text() == 'new'
    assert not list(tmp_path.glob('.*.retired-*'))


def test_backend_owned_descendants_do_not_break_publication(tmp_path):
    tmp, dst = trees(tmp_path)
    with patch.object(private.shutil, 'rmtree', side_effect=PermissionError('backend-owned descendant')):
        with pytest.warns(RuntimeWarning, match='retired recipe cache retained'):
            private.replace_recipe(tmp, dst)
    assert (dst / 'metadata/description.md').read_text() == 'new'
    backup, = tmp_path.glob('.files.retired-*')
    assert (backup / 'metadata/description.md').read_text() == 'old'


def test_failed_publication_rolls_back_complete_original(tmp_path):
    tmp, dst = trees(tmp_path)
    rename = Path.rename
    def fail(source, target):
        if source == tmp:
            raise PermissionError('publish denied')
        return rename(source, target)
    with patch.object(Path, 'rename', fail), pytest.raises(PermissionError, match='publish denied'):
        private.replace_recipe(tmp, dst)
    assert (dst / 'metadata/description.md').read_text() == 'old'
    assert (tmp / 'metadata/description.md').read_text() == 'new'


def test_symlink_recipe_refused_without_touching_target(tmp_path):
    tmp, dst = trees(tmp_path)
    other = tmp_path / 'other'
    dst.rename(other)
    dst.symlink_to(other, target_is_directory=True)
    with pytest.raises(private.PrivateError, match='symlink'):
        private.replace_recipe(tmp, dst)
    assert (other / 'metadata/description.md').read_text() == 'old'
