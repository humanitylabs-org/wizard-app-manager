"""Service coordination inodes stay local; owner stores stay fully compared."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam.backup import agents, restore
from wam.backup.box import box_digest, root_runtime_digest_path
from test_backup_flow import world, isolated, _export, _contents, _cli

BASES = ('.agent-pairs.service-lease.sqlite', '.threads.json.agent-pairs.lock.sqlite',
         '.agent-pairs.lock.sqlite')
RUNTIME = tuple(n + s for n in BASES for s in ('', '-journal', '-wal', '-shm'))
OWNER = ('.threads.json', '.agent-grants.json', 'owner.sqlite', 'owner.sqlite-journal',
         'owner.sqlite-wal-copy', 'prefix.agent-pairs.service-lease.sqlite',
         '.agent-pairs.service-lease.sqlite-journal-copy', '.agent-pairs.lock.sqlite-journal-journal')


def test_exact_coordination_names_only():
    assert all(agents.root_runtime_file(n) for n in RUNTIME)
    assert not any(agents.root_runtime_file(n) for n in OWNER)
    assert not agents.root_runtime_file('child/' + RUNTIME[0])


def test_export_import_undo_keeps_coordination_inodes_and_owner_journals(world):
    old, new = world['old'], world['new']
    roots = [Path(x.box().agents_root()) for x in (old, new)]
    for root, prefix in zip(roots, ('source', 'destination')):
        root.mkdir(parents=True, exist_ok=True)
        for name in RUNTIME + OWNER:
            (root / name).write_text(prefix + name)
    inodes = {n: (roots[1] / n).stat().st_ino for n in RUNTIME}
    before = box_digest(new.box())
    out, _ = _export(world)
    names = _contents(out, world['key'])
    assert all('agents-root/' + n not in names for n in RUNTIME)
    assert all('agents-root/' + n in names for n in OWNER)
    code, result = _cli(new.box(), 'import', str(out), '--yes', '--key-file', str(world['keyfile']))
    assert code == 0, result
    for n in RUNTIME:
        assert (roots[1] / n).stat().st_ino == inodes[n]
        (roots[1] / n).write_text('live Service wrote a new lease')
    assert all((roots[1] / n).read_text() == 'source' + n for n in OWNER)
    code, result = _cli(new.box(), 'undo')
    assert code == 0 and result['digest_match'], result
    assert box_digest(new.box()) == before
    assert all((roots[1] / n).stat().st_ino == inodes[n] for n in RUNTIME)
    assert all((roots[1] / n).read_text() == 'destination' + n for n in OWNER)
    (roots[1] / 'owner.sqlite-journal').write_text('owner transaction changed')
    assert box_digest(new.box()) != before


@pytest.mark.parametrize('layout', ['home', 'split', 'bundle', 'stock'])
def test_digest_scope_and_legacy_comparison(tmp_path, layout):
    home, rt = tmp_path / 'wizard', tmp_path / 'runtipi'
    ar = {'home': home / 'agents', 'split': rt / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/agents',
          'bundle': rt / 'app-data/wizardapps/wizard-shell/data/wizard-os/agents',
          'stock': rt / 'app-data/wizard-os/agents'}[layout]
    box = SimpleNamespace(home=home, runtipi_root=rt, agents_root=lambda: ar)
    tree = 'wizard-os' if layout == 'home' else 'runtipi/app-data'
    base = home if layout == 'home' else rt / 'app-data'
    prefix = ar.relative_to(base).as_posix() + '/'
    runtime = prefix + RUNTIME[1]
    owner = prefix + 'owner.sqlite-journal'
    assert root_runtime_digest_path(box, runtime, tree)
    for bad in (prefix + 'child/' + RUNTIME[1], 'elsewhere/' + RUNTIME[1], owner):
        assert not root_runtime_digest_path(box, bad, tree)
    fields = {'type': 'file', 'content': ['content'], 'mode': '', 'owner': '1:1'}
    before = {tree: {runtime: fields, owner: fields}}
    after = {tree: {owner: fields}}
    saved = copy.deepcopy(before)
    want, actual = {tree: 'old-hash'}, {tree: 'new-hash'}
    mismatch, diff = restore._undo_comparison(box, want, actual, before, after)
    assert mismatch == [] and diff['total'] == 0
    assert before == saved, 'original digest evidence is immutable'
    for field in ('content', 'owner', 'mode', 'type'):
        changed = copy.deepcopy(after)
        changed[tree][owner][field] = 'changed'
        mismatch, diff = restore._undo_comparison(box, want, actual, before, changed)
        assert mismatch == [tree] and diff['total'] == 1
    assert restore._undo_comparison(box, want, actual, None, after)[0] == [tree]
    assert restore._undo_comparison(box, want, actual, before, {})[0] == [tree]


@pytest.mark.parametrize('frozen', [False, True])
def test_old_journal_never_replaces_live_coordination_inode(tmp_path, frozen):
    ar = tmp_path / 'agents'; ar.mkdir()
    root = tmp_path / 'restore'; root.mkdir()
    held = root / 'before/agents-root'; held.mkdir(parents=True)
    name = RUNTIME[1]
    (ar / name).write_text('current live lease')
    (held / name).write_text('old saved lease')
    (ar / 'owner.sqlite-journal').write_text('imported')
    (held / 'owner.sqlite-journal').write_text('original')
    inode = (ar / name).stat().st_ino
    rows = [dict(kind='set_aside_entry', area='agents-root', src=str(ar / n), dst=str(held / n), state='done')
            for n in (name, 'owner.sqlite-journal')]
    j = {'steps': [dict(kind='set_aside', area='agents-root'), *rows], 'agents_root': str(ar)}
    if frozen:
        j['undo_moves'] = [dict(kind='undo_stash', area='agents-root', src=str(ar / n),
                                dst=str(root / 'undone/agents-root' / n)) for n in (name, 'owner.sqlite-journal')]
        j['undo_moves'] += [dict(kind='undo_restore', area='agents-root', src=r['dst'], dst=r['src']) for r in rows]
    box = SimpleNamespace()
    for _ in range(2):
        restore._undo_journaled_trees(box, root, j, {}, tmp_path / 'files', tmp_path / 'froot')
        assert (ar / name).stat().st_ino == inode and (ar / name).read_text() == 'current live lease'
        assert (ar / 'owner.sqlite-journal').read_text() == 'original'
