"""Undo diagnostics retain fingerprints, not file bodies, and report bounded relative paths."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace

from wam.backup.box import digest_differences, tree_digest


def test_diagnostic_kinds_and_limit(tmp_path):
    for name in ('missing', 'content', 'mode', 'owner'):
        (tmp_path / name).write_text('PRIVATE BODY')
    before = {}
    tree_digest(tmp_path, entries=before)
    (tmp_path / 'missing').unlink()
    (tmp_path / 'extra').write_text('new')
    (tmp_path / 'content').write_text('changed')
    (tmp_path / 'mode').chmod(0o700)
    after = {}
    tree_digest(tmp_path, entries=after)
    after['owner']['owner'] = '123:456'
    result = digest_differences({'tree': before}, {'tree': after})
    assert result['total'] == 5 and not result['truncated']
    assert {p['path']: p['kinds'] for p in result['paths']} == {
        'missing': ['missing'], 'extra': ['extra'], 'content': ['content'],
        'mode': ['mode'], 'owner': ['owner']}
    bounded = digest_differences({'tree': before}, {'tree': after}, limit=2)
    assert bounded['total'] == 5 and len(bounded['paths']) == 2 and bounded['truncated']
    assert 'PRIVATE BODY' not in str(before) + str(result)


def test_digest_owner_and_legacy_mode(tmp_path, monkeypatch):
    p = tmp_path / 'data'
    p.write_text('unchanged')
    before, _ = tree_digest(tmp_path)
    legacy, _ = tree_digest(tmp_path, owners=False)
    real = os.lstat

    def other_owner(path, *args, **kwargs):
        st = real(path, *args, **kwargs)
        if Path(path) == p:
            return SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size, st_mtime_ns=st.st_mtime_ns,
                                   st_uid=st.st_uid + 1, st_gid=st.st_gid)
        return st

    monkeypatch.setattr(os, 'lstat', other_owner)
    assert tree_digest(tmp_path)[0] != before
    assert tree_digest(tmp_path, owners=False)[0] == legacy
