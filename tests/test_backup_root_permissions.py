"""Root-only app-data must travel, not disappear behind host PermissionError.

The Node helper below is the exact production helper, run without Docker solely
for unit-test transport. Host calls are denied even when CI happens to be root.
The separate disposable-worker probe also uses genuinely root-owned 0700 trees.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from wam.backup import agents, bundle, filesystem as fs, fmt, restore
from wam.backup.box import Box
from test_backup_flow import _export, isolated, world  # noqa: F401


@pytest.fixture
def denied_host(monkeypatch):
    roots = []
    real_popen = subprocess.Popen
    node = shutil.which('node')
    assert node, 'Node is needed to exercise the shipped Manager helper'
    launches = []

    def popen(cmd, **kw):
        if '--entrypoint' in cmd and cmd[cmd.index('--entrypoint') + 1] == 'node':
            launches.append(cmd)
            assert cmd[:8] == ['docker', 'run', '--rm', '-i', '--network', 'none', '-u', '0:0']
            assert cmd.count('-v') == 1  # cross-bind rename would be EXDEV on Linux
            assert not any('docker.sock' in arg for arg in cmd)
            assert 'WIZARD_BACKUP_KEY' not in kw['env']
            return real_popen([node, '-e', fs.NODE, cmd[-1]], **kw)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(subprocess, 'Popen', popen)

    def protect(root):
        roots.append(str(root))

    def guarded(fn):
        def wrapped(path, *a, **kw):
            # The in-process fake Runtipi represents the privileged daemon,
            # not the unprivileged WAM process under test.
            import sys
            frame = sys._getframe(1)
            while frame:
                if frame.f_code.co_filename.endswith('backup_fakes.py'):
                    return fn(path, *a, **kw)
                frame = frame.f_back
            paths = (path, a[0]) if fn.__name__ == 'rename' and a else (path,)
            if any(isinstance(p, (str, bytes, os.PathLike)) and any(
                    str(p) == r or str(p).startswith(r + '/') for r in roots) for p in paths):
                raise PermissionError(13, 'simulated root-only backup tree', str(path))
            return fn(path, *a, **kw)
        return wrapped

    for name in ('stat', 'lstat', 'listdir', 'mkdir', 'chmod', 'chown', 'unlink', 'rmdir', 'rename', 'open'):
        monkeypatch.setattr(os, name, guarded(getattr(os, name)))
    import builtins
    monkeypatch.setattr(builtins, 'open', guarded(builtins.open))
    yield protect, launches
    roots.clear()  # pytest cleanup is not part of the backup engine


def test_discovery_reads_private_service_and_bundle_roots(tmp_path, denied_host):
    protect, launches = denied_host
    home, rt = tmp_path / 'wizard', tmp_path / 'runtipi'
    home.mkdir(); rt.mkdir()
    main = rt / 'app-data/wizardapps/hermes-agent/data'
    main.mkdir(parents=True)
    root = rt / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/agents'
    agent = root / 'research'
    agent.mkdir(parents=True)
    (agent / 'agent.json').write_text(json.dumps({'schema':1, 'id':'research', 'label':'Research'}))
    (root / '.main-agent.json').write_text('{"apps":["files"]}')
    root.chmod(0o700)
    protect(rt / 'app-data')
    b = Box(home, rt, env={**os.environ, 'WAM_BACKUP_HELPER_IMAGE':'example/wam:test',
                          'WIZARD_BACKUP_KEY':'must-not-reach-helper'})
    assert b.main_agent().id == 'hermes'
    assert [a.id for a in b.agents()] == ['hermes', 'research']
    with b.operation():
        assert b.agents_root() == root
        assert agents.root_files(b.agents_root()) == ['.main-agent.json']
        assert (b.agents_root() / '.main-agent.json').read_text() == '{"apps":["files"]}'
        assert bundle.tree_size(b.agents_root())['count'] == 2
        assert b.agents_root().stat().st_mode & 0o777 == 0o700
    assert launches


def test_helper_refuses_escape_and_preserves_data(tmp_path, denied_host):
    protect, _ = denied_host
    home, rt = tmp_path / 'wizard', tmp_path / 'runtipi'
    home.mkdir(); rt.mkdir()
    root = rt / 'app-data'
    root.mkdir()
    secret = tmp_path / 'outside'
    secret.write_text('not captured')
    (root / 'escape').symlink_to(secret)
    protect(root)
    b = Box(home, rt, env={**os.environ, 'WAM_BACKUP_HELPER_IMAGE':'example/wam:test'})
    with b.operation():
        with pytest.raises(OSError, match='symbolic link'):
            (b.runtipi_root / 'app-data/escape').read_text()
        with pytest.raises(PermissionError, match='outside'):
            fs._current.get().call('rename', root / 'escape', dst=str(secret))
        outside_dir = tmp_path / 'private-directory'
        outside_dir.mkdir()
        linked_dir = root / 'linked-directory'
        linked_dir.symlink_to(outside_dir)
        with pytest.raises(OSError, match='symbolic link'):
            fs.listdir(linked_dir)
        dangling = root / 'dangling'
        missing = tmp_path / 'must-not-be-created'
        dangling.symlink_to(missing)
        with pytest.raises(OSError):
            with fs.private_open(dangling) as f:
                f.write('private')
        assert not missing.exists()


def test_private_temp_mode_before_writing(tmp_path):
    target = tmp_path / 'temporary-env'
    previous = os.umask(0o022)
    try:
        with fs.private_open(target) as f:
            assert target.stat().st_mode & 0o777 == 0o600
            f.write('SECRET=private')
        target.chmod(0o644)
        with fs.private_open(target):
            assert target.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous)


def test_private_fchmod_failure_uses_helper(tmp_path, denied_host, monkeypatch):
    _, launches = denied_host
    home, rt = tmp_path / 'wizard', tmp_path / 'runtipi'
    home.mkdir(); rt.mkdir()
    target = rt / 'app-data/env-temp'
    target.parent.mkdir()
    target.write_text('old')
    target.chmod(0o666)
    def denied(*a):
        raise PermissionError('different file owner')
    monkeypatch.setattr(os, 'fchmod', denied)
    b = Box(home, rt, env={**os.environ, 'WAM_BACKUP_HELPER_IMAGE':'example/wam:test'})
    with b.operation():
        with fs.private_open(target) as f:
            assert target.stat().st_mode & 0o777 == 0o600
            f.write('private')
    assert launches
    assert target.read_text() == 'private'


def test_replaced_ancestor_cannot_redirect_helper(tmp_path, denied_host, monkeypatch):
    home, rt = tmp_path / 'wizard', tmp_path / 'runtipi'
    home.mkdir(); rt.mkdir()
    parent = rt / 'app-data/agent'
    parent.mkdir(parents=True)
    held = parent.with_name('original-pinned-agent')
    outside = tmp_path / 'outside-root'
    outside.mkdir()
    marker = "const p = q.path === undefined ? undefined : checked(q.path);"
    attack = (marker + "\nif(q.op==='mkdir') { fs.renameSync(" + json.dumps(str(parent)) + "," +
              json.dumps(str(held)) + "); fs.symlinkSync(" + json.dumps(str(outside)) + "," +
              json.dumps(str(parent)) + "); }")
    monkeypatch.setattr(fs, 'NODE', fs.NODE.replace(marker, attack))
    b = Box(home, rt, env={**os.environ, 'WAM_BACKUP_HELPER_IMAGE':'example/wam:test'})
    with b.operation():
        fs._current.get().call('mkdir', parent / 'new-entry', mode=0o700)
    assert (held / 'new-entry').is_dir()
    assert not (outside / 'new-entry').exists()


def test_private_agent_metadata_export_and_undo(world, denied_host):  # noqa: F811
    """Use the real exporter/importer/journal with the existing fake app lifecycle."""
    old, new = world["old"], world["new"]
    b = old.box()
    # Move the fake extra agent into the modern Service-owned app-data layout.
    root = b.runtipi_root / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/agents'
    root.mkdir(parents=True)
    shutil.move(str(b.home / 'agents/research'), str(root / 'research'))
    (root / '.main-agent.json').write_text('{"fixture":"shared"}')
    # Fake stock Hermes runs separately and must see the new actual home path.
    old.rt.agent_homes['agent-research:wizardapps'] = root / 'research/home'
    state = json.loads(old.docker_state.read_text())
    state['agent-research_wizardapps']['home'] = str(root / 'research/home')
    old.docker_state.write_text(json.dumps(state))
    root.chmod(0o700)
    destination = new.runtipi / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/agents'
    destination.mkdir(parents=True)
    (destination / '.main-agent.json').write_text('{"fixture":"destination"}')
    new.rt.agent_homes['agent-research:wizardapps'] = destination / 'research/home'
    protect, _ = denied_host
    protect(root.parent)
    protect(destination.parent)
    out, _ = _export(world)
    keys = fmt.Keys(recovery=[world["key"]])
    report = restore.import_bundle(new.box(), out, keys, yes=True)
    assert report['ok']
    with new.box().operation():
        assert (fs.Path(destination) / '.main-agent.json').read_text() == '{"fixture":"shared"}'
        assert (fs.Path(destination) / 'research/runtime.key').read_text() == 'research-runtime-key'
    assert restore.undo(new.box())['digest_match']
    with new.box().operation():
        assert (fs.Path(destination) / '.main-agent.json').read_text() == '{"fixture":"destination"}'


def test_root_owned_owner_files_folder_is_read_through_helper(tmp_path, denied_host):
    """0.3.48 Thales gate: export died on root-owned media/Wisdom/launch-public."""
    protect, launches = denied_host
    home, rt = tmp_path / 'wizard', tmp_path / 'runtipi'
    home.mkdir(); rt.mkdir()
    (rt / 'app-data').mkdir()
    wisdom = rt / 'media/Wisdom/launch-public'
    wisdom.mkdir(parents=True)
    (wisdom / 'reading.md').write_text('public reading')
    (rt / 'media/notes.md').write_text('owner note')
    protect(wisdom)
    b = Box(home, rt, env={**os.environ, 'WAM_BACKUP_HELPER_IMAGE': 'example/wam:test'})
    with b.operation():
        assert fs.listdir(wisdom) == ['reading.md']
        assert bundle.tree_size(b.files_root())['count'] == 2
    assert launches


@pytest.mark.parametrize('external', [False, True])
def test_root_owned_media_export_verify_import_receipt_and_undo(world, denied_host, monkeypatch, external):
    """A denied Wisdom folder must survive every real backup engine phase."""
    from wam.backup.box import box_digest
    old, new = world['old'], world['new']
    if external:
        for fake in (old, new):
            media = world['tmp'] / (fake.home.parent.parent.name + '-external') / 'media'
            media.parent.mkdir(exist_ok=True)
            shutil.move(str(fake.box().files_root()), str(media))
            # Exercise production discovery, not only the test-only files override.
            (fake.runtipi / '.env').write_text('RUNTIPI_MEDIA_PATH=' + str(media.parent) + '\n')
    src, dst = old.box().files_root(), new.box().files_root()
    for root, text in ((src, 'carried reading'), (dst, 'original reading')):
        folder = root / 'Wisdom/launch-public'
        folder.mkdir(parents=True)
        (folder / 'reading.md').write_text(text)
        folder.chmod(0o700)
    protect, launches = denied_host
    protect(src / 'Wisdom')
    protect(dst / 'Wisdom')
    # A common mount + one external media bind (not per-tree binds).
    real_popen = subprocess.Popen
    if external:
        def popen(cmd, **kw):
            if '--entrypoint' in cmd and cmd[cmd.index('--entrypoint') + 1] == 'node':
                assert cmd.count('-v') == 2
                # Fixture checks the remaining confinement/secret properties.
                i = cmd.index('-v', cmd.index('-v') + 1)
                return real_popen(cmd[:i] + cmd[i+2:], **kw)
            return real_popen(cmd, **kw)
        monkeypatch.setattr(subprocess, 'Popen', popen)
    before = box_digest(new.box())
    out, exported = _export(world)
    keys = fmt.Keys(recovery=[world['key']])
    assert exported['ok'] and restore.verify(new.box(), out, keys)['ok']
    assert box_digest(new.box()) == before  # verify writes nothing
    real_apply = restore._apply
    def apply(box, r, p, agents):
        # After staging, renamed root-owned originals remain inaccessible. Also
        # deny future Undo stashes so cleanup must use the helper, not skip them.
        protect(r.froot / 'before')
        protect(r.froot / 'undone')
        return real_apply(box, r, p, agents)
    monkeypatch.setattr(restore, '_apply', apply)
    report = restore.import_bundle(new.box(), out, keys, yes=True)
    assert report['ok'] and not report['partial']
    with new.box().operation():
        assert (dst / 'Wisdom/launch-public/reading.md').read_text() == 'carried reading'
        root, journal = restore.last_restore(new.box())
        assert json.loads((root / 'receipt.json').read_text())['ok']
        held = dst / '.wizard-restore' / report['stamp']
        assert (held / 'before/Wisdom/launch-public/reading.md').read_text() == 'original reading'
    undone = restore.undo(new.box())
    assert undone['ok'] and undone['digest_match'] and not undone['mismatch']
    assert box_digest(new.box()) == before
    with new.box().operation():
        assert (dst / 'Wisdom/launch-public/reading.md').read_text() == 'original reading'
        assert not held.exists()
        assert restore.last_restore(new.box())[1]['status'] == 'undone'
    assert launches


@pytest.mark.parametrize('part', ['apps', 'user-config', 'repos'])
def test_other_root_owned_runtipi_trees_export_and_undo(world, denied_host, part):
    """Runtipi creates packages/overrides as root too; host reads cannot skip them."""
    from wam.backup.box import box_digest
    old, new = world['old'], world['new']
    protect, _ = denied_host
    for fake in (old, new):
        protect(fake.runtipi / part)
    before = box_digest(new.box())
    out, exported = _export(world)
    assert exported['ok']
    keys = fmt.Keys(recovery=[world['key']])
    assert restore.verify(new.box(), out, keys)['ok']
    assert restore.import_bundle(new.box(), out, keys, yes=True)['ok']
    assert restore.undo(new.box())['digest_match']
    assert box_digest(new.box()) == before
