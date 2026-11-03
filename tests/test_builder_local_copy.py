"""Opt-in custom-copy migration; no personal fixture/source or live writes."""
import json
import os
from pathlib import Path

import pytest

from wam import builder_bundle as bb


def stable_stat(path):
    st = path.lstat()
    return st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns


@pytest.fixture
def local(tmp_path, monkeypatch):
    osh, hh = tmp_path / 'os', tmp_path / 'hermes'
    skill = osh / 'system-skills/wizard-builder'
    skill.mkdir(parents=True)
    skill.chmod(0o775)
    (skill / 'SKILL.md').write_bytes(b'locally customized builder\n')
    (skill / 'SKILL.md').chmod(0o664)
    (skill / 'empty').mkdir()
    (skill / 'empty').chmod(0o750)
    hh.mkdir()
    (hh / 'config.yaml').write_text('model: unchanged\n')
    (hh / 'skills').mkdir()
    (hh / 'skills/wizard-builder').symlink_to(skill)
    spec = {'variant': 'synthetic-local-copy', 'entries': bb._local_snapshot(skill)}
    original = bb._local_spec
    monkeypatch.setattr(bb, '_local_spec', lambda variant: spec if variant == spec['variant'] else original(variant))
    monkeypatch.setenv('WIZARD_OS_HOME', str(osh))
    return osh, hh, skill, spec


def test_explicit_only_preserves_bytes_modes_and_alias(local, monkeypatch):
    osh, hh, skill, spec = local
    before = bb._local_snapshot(skill)
    with pytest.raises(ValueError, match='Unmanaged'):
        bb.install(osh)
    with pytest.raises(ValueError, match='collision'):
        bb.check_collisions(hh, osh)
    bb.check_collisions(hh, osh, migrate_local_copy=spec['variant'])
    out = bb.install(osh, migrate_local_copy=spec['variant'])
    saved = Path(out['preserved_local_copy'])
    assert bb._local_snapshot(saved) == before
    assert json.loads(saved.with_suffix('.json').read_text()) == {
        'provenance': 'reviewed-custom-local-copy', 'variant': spec['variant'], 'entries': before}
    bb.register(hh, osh)
    assert skill.is_symlink() and (hh / 'skills/wizard-builder').is_symlink()
    assert 'Every tap registers before I/O' in (hh / 'skills/wizard-builder/SKILL.md').read_text()
    assert all(row[1] for row in bb.health(osh, hh))
    assert bb.install(osh)['sha256'] == out['sha256']
    # Restoration is separately tested against the shipped, exact spec.


@pytest.mark.parametrize('change', ['bytes', 'file-mode', 'dir-mode', 'extra', 'empty-dir', 'symlink', 'fifo', 'cache', 'manifest'])
def test_exact_inventory_refuses_any_drift(local, change):
    osh, hh, skill, spec = local
    if change == 'bytes':
        (skill / 'SKILL.md').write_text('changed')
    elif change == 'file-mode':
        (skill / 'SKILL.md').chmod(0o600)
    elif change == 'dir-mode':
        skill.chmod(0o700)
    elif change == 'empty-dir':
        (skill / 'extra-dir').mkdir()
    elif change == 'symlink':
        (skill / 'link').symlink_to('SKILL.md')
    elif change == 'fifo':
        os.mkfifo(skill / 'pipe')
    else:
        (skill / {'cache': 'extra.pyc', 'manifest': 'bundle.json'}.get(change, 'extra')).write_text('extra')
    cfg = (hh / 'config.yaml').read_bytes()
    with pytest.raises(ValueError, match='preserving'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert not skill.is_symlink()
    assert not (osh / 'builder').exists()
    assert (hh / 'config.yaml').read_bytes() == cfg


def test_unknown_variant_and_other_discovery_collision(local):
    osh, hh, skill, spec = local
    with pytest.raises(ValueError, match='Unknown'):
        bb.install(osh, migrate_local_copy='invented')
    (hh / 'skills/nested').mkdir()
    (hh / 'skills/nested/wizard-builder.md').write_text('owner skill')
    with pytest.raises(ValueError, match='collision'):
        bb.check_collisions(hh, osh, migrate_local_copy=spec['variant'])
    assert not skill.is_symlink()


@pytest.mark.parametrize('point', ['receipt', 'current', 'skill'])
def test_failure_restores_original_local_copy(local, monkeypatch, point):
    osh, hh, skill, spec = local
    before = bb._local_snapshot(skill)
    switch, atomic = bb._switch, bb._atomic
    def fail_switch(link, target):
        if (point == 'skill' and link == skill) or (point == 'current' and link.name == 'current'):
            raise OSError('synthetic switch failure')
        return switch(link, target)
    def fail_atomic(path, text):
        if point == 'receipt' and path.name.startswith('local-copy-'):
            raise OSError('synthetic receipt failure')
        return atomic(path, text)
    monkeypatch.setattr(bb, '_switch', fail_switch)
    monkeypatch.setattr(bb, '_atomic', fail_atomic)
    with pytest.raises(OSError, match='synthetic'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert not skill.is_symlink()
    assert bb._local_snapshot(skill) == before
    assert not (osh / 'builder/current').exists()


def test_local_restore_requires_exact_receipt_and_managed_target(local, monkeypatch):
    osh, hh, skill, spec = local
    out = bb.install(osh, migrate_local_copy=spec['variant'])
    saved = Path(out['preserved_local_copy'])
    # The production variant is fixed in the shipped source; redirect only its
    # spec-file read to the synthetic fixture, never admit caller-supplied hashes.
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda p, *a, **kw: json.dumps(spec) if p.name == 'builder_local_copy.json' else original(p, *a, **kw))
    with pytest.raises(ValueError, match='Expected exact'):
        bb.restore_local_copy(osh, '../escape')
    receipt = saved.with_suffix('.json')
    old = receipt.read_bytes()
    receipt.write_text('{}')
    with pytest.raises(ValueError, match='receipt differs'):
        bb.restore_local_copy(osh, saved.name)
    receipt.write_bytes(old)
    assert bb.restore_local_copy(osh, saved.name)['state'] == 'restored_local_copy'
    assert bb._local_snapshot(skill) == spec['entries']
    assert (hh / 'skills/wizard-builder').is_symlink()
    assert (osh / 'builder/releases' / out['release']).is_dir()


@pytest.mark.parametrize('change', ['bytes', 'mode', 'target'])
def test_local_restore_refuses_changed_backup_or_discovery(local, monkeypatch, change):
    osh, hh, skill, spec = local
    out = bb.install(osh, migrate_local_copy=spec['variant'])
    saved = Path(out['preserved_local_copy'])
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda p, *a, **kw: json.dumps(spec) if p.name == 'builder_local_copy.json' else original(p, *a, **kw))
    if change == 'bytes':
        (saved / 'SKILL.md').write_text('changed backup')
    elif change == 'mode':
        saved.chmod(0o700)
    else:
        skill.unlink()
        skill.mkdir()
        (skill / 'SKILL.md').write_text('owner replacement')
    with pytest.raises(ValueError):
        bb.restore_local_copy(osh, saved.name)
    assert saved.is_dir()
    if change == 'target':
        assert (skill / 'SKILL.md').read_text() == 'owner replacement'
    else:
        assert skill.is_symlink()


def test_activation_failure_restores_previous_current(local, monkeypatch):
    osh, hh, skill, spec = local
    # Start with a retained managed release, then the review fixture as the
    # active discovery tree: rollback must not drop the prior current pointer.
    original = skill.rename(osh / 'original-local')
    bb.install(osh)
    previous = os.readlink(osh / 'builder/current')
    skill.unlink()
    original.rename(skill)
    switch = bb._switch
    def fail_skill(link, target):
        if link == skill:
            raise OSError('synthetic switch failure')
        return switch(link, target)
    monkeypatch.setattr(bb, '_switch', fail_skill)
    with pytest.raises(OSError, match='synthetic'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert bb._local_snapshot(skill) == spec['entries']
    assert os.readlink(osh / 'builder/current') == previous


def test_unmanaged_launcher_refuses_before_migration(local):
    osh, hh, skill, spec = local
    (osh / 'bin').mkdir()
    (osh / 'bin/wam').write_text('owner command')
    with pytest.raises(ValueError, match='Unmanaged command'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert bb._local_snapshot(skill) == spec['entries']
    assert not (osh / 'builder').exists()


def test_file_bin_parent_refuses_before_migration(local):
    osh, hh, skill, spec = local
    (osh / 'bin').write_bytes(b'owner bin file')
    with pytest.raises(ValueError, match='Unmanaged directory'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert bb._local_snapshot(skill) == spec['entries']
    assert not (osh / 'builder').exists()
    assert (osh / 'bin').read_bytes() == b'owner bin file'


@pytest.mark.parametrize('point', ['companion', 'launcher', 'chmod', 'register'])
@pytest.mark.parametrize('existing', [False, True])
def test_required_publication_failure_restores_all_discovery(local, monkeypatch, point, existing):
    osh, hh, skill, spec = local
    if existing:
        original = skill.rename(osh / 'original-local')
        bb.install(osh)
        skill.unlink()
        original.rename(skill)
        (osh / 'bin/wam').write_text('# ' + bb.MARKER + '\n# prior command\n')
        (osh / 'bin/wam').chmod(0o750)
    previous = os.readlink(osh / 'builder/current') if existing else None
    cmd = osh / 'bin/wam'
    before_cmd = (cmd.read_bytes(), cmd.stat().st_mode & 0o777) if existing else None
    cfg = hh / 'config.yaml'
    cfg.chmod(0o640)
    before_cfg = cfg.read_bytes(), cfg.stat().st_mode & 0o777
    companion = osh / 'system-skills/wizard-guide'
    before_companion = os.readlink(companion) if companion.is_symlink() else None
    switch, atomic, chmod, register = bb._switch, bb._atomic, Path.chmod, bb.register
    fired = False
    def fail_once(label):
        nonlocal fired
        if point == label and not fired:
            fired = True
            raise OSError('synthetic required publication failure')
    def fail_switch(link, target):
        result = switch(link, target)
        if link == companion:
            fail_once('companion')
        return result
    def fail_atomic(path, text):
        result = atomic(path, text)
        if path == cmd:
            fail_once('launcher')
        return result
    def fail_chmod(path, mode, *a, **kw):
        if path == cmd:
            fail_once('chmod')
        return chmod(path, mode, *a, **kw)
    def fail_register(*a, **kw):
        result = register(*a, **kw)
        fail_once('register')
        return result
    # Existing companions are deliberately retained. Force their publication
    # only in the companion fault case, leaving a separate retained-link path.
    if point == 'companion' and companion.is_symlink():
        companion.unlink()
        before_companion = None
    monkeypatch.setattr(bb, '_switch', fail_switch)
    monkeypatch.setattr(bb, '_atomic', fail_atomic)
    monkeypatch.setattr(Path, 'chmod', fail_chmod)
    monkeypatch.setattr(bb, 'register', fail_register)
    assert bb.main(['install', '--migrate-local-copy', spec['variant'], '--hermes-home', str(hh)]) == 1
    assert fired
    assert not skill.is_symlink() and bb._local_snapshot(skill) == spec['entries']
    current = osh / 'builder/current'
    assert (os.readlink(current) if current.is_symlink() else None) == previous
    assert ((cmd.read_bytes(), cmd.stat().st_mode & 0o777) if cmd.exists() else None) == before_cmd
    assert (cfg.read_bytes(), cfg.stat().st_mode & 0o777) == before_cfg
    assert (os.readlink(companion) if companion.is_symlink() else None) == before_companion


@pytest.mark.parametrize('target', ['backup', 'receipt'])
@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink', 'dangling'])
def test_deterministic_preservation_collision_never_clobbers(local, monkeypatch, target, kind):
    from types import SimpleNamespace
    osh, hh, skill, spec = local
    base = osh / 'builder'
    base.mkdir()
    (base / '.owner').write_text(bb.MARKER + '\n')
    saved = base / ('local-copy-' + spec['variant'] + '-12345678')
    collision = saved if target == 'backup' else saved.with_suffix('.json')
    owner = osh / 'owner-bytes'
    owner.write_bytes(b'owner bytes')
    if kind == 'file':
        collision.write_bytes(b'owner collision bytes')
    elif kind == 'directory':
        collision.mkdir()
    else:
        collision.symlink_to(owner if kind == 'symlink' else osh / 'missing')
    before = stable_stat(collision), os.readlink(collision) if collision.is_symlink() else None
    monkeypatch.setattr(bb.uuid, 'uuid4', lambda: SimpleNamespace(hex='12345678' + '0' * 24))
    with pytest.raises(ValueError, match='preservation destination'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert not skill.is_symlink() and bb._local_snapshot(skill) == spec['entries']
    assert stable_stat(collision) == before[0]
    assert (os.readlink(collision) if collision.is_symlink() else None) == before[1]
    assert owner.read_bytes() == b'owner bytes'
    if kind == 'file':
        assert collision.read_bytes() == b'owner collision bytes'
    assert not (base / 'current').is_symlink()


@pytest.mark.parametrize('kind', ['original', 'external', 'dangling', 'file'])
def test_release_parent_refuses_before_mutation(local, kind):
    osh, hh, skill, spec = local
    base = osh / 'builder'
    base.mkdir()
    (base / '.owner').write_text(bb.MARKER + '\n')
    releases = base / 'releases'
    external = osh.parent / 'external-owner'
    external.mkdir()
    (external / 'canary').write_bytes(b'owner bytes')
    if kind == 'file':
        releases.write_bytes(b'owner release file')
    else:
        target = skill if kind == 'original' else external if kind == 'external' else osh / 'missing'
        releases.symlink_to(target, target_is_directory=True)
    before_skill = bb._local_snapshot(skill)
    before_external = bb._local_snapshot(external)
    before_link = os.readlink(releases) if releases.is_symlink() else None
    with pytest.raises(ValueError, match='Unmanaged release directory'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert bb._local_snapshot(skill) == before_skill == spec['entries']
    assert bb._local_snapshot(external) == before_external
    assert (os.readlink(releases) if releases.is_symlink() else None) == before_link
    if kind == 'file':
        assert releases.read_bytes() == b'owner release file'
    assert sorted(p.name for p in base.iterdir()) == ['.owner', 'releases']
    assert not skill.is_symlink()


@pytest.mark.parametrize('parent', ['system-skills', 'bin'])
def test_install_static_ancestor_symlink_refuses_before_mutation(local, parent):
    osh, hh, skill, spec = local
    outside = osh.parent / ('outside-' + parent)
    if parent == 'system-skills':
        (osh / parent).rename(outside)
    else:
        outside.mkdir()
        (outside / 'canary').write_bytes(b'owner bytes')
    (osh / parent).symlink_to(outside, target_is_directory=True)
    before = bb._local_snapshot(outside)
    with pytest.raises(ValueError, match='Unmanaged directory'):
        bb.install(osh, migrate_local_copy=spec['variant'])
    assert bb._local_snapshot(outside) == before
    assert not (osh / 'builder').exists()
    assert (osh / parent).is_symlink()


@pytest.mark.parametrize('parent', ['system-skills', 'bin'])
def test_restore_static_ancestor_symlink_refuses_before_mutation(local, monkeypatch, parent):
    osh, hh, skill, spec = local
    out = bb.install(osh, migrate_local_copy=spec['variant'])
    saved = Path(out['preserved_local_copy'])
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda p, *a, **kw: json.dumps(spec) if p.name == 'builder_local_copy.json' else original(p, *a, **kw))
    outside = osh.parent / ('outside-' + parent)
    (osh / parent).rename(outside)
    (osh / parent).symlink_to(outside, target_is_directory=True)
    # Snapshot only exact external directory entries (managed child links are
    # deliberately not followed by this witness).
    before = [(p.name, stable_stat(p), os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in outside.iterdir()]
    with pytest.raises(ValueError, match='Unmanaged directory'):
        bb.restore_local_copy(osh, saved.name)
    assert [(p.name, stable_stat(p), os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in outside.iterdir()] == before
    assert bb._local_snapshot(saved) == spec['entries']


def test_second_io_failure_reports_exact_partial_recovery(local, monkeypatch, capsys):
    osh, hh, skill, spec = local
    cmd = osh / 'bin/wam'
    atomic, restore = bb._atomic, bb._restore_publication
    def fail_publication(path, text):
        atomic(path, text)
        if path == cmd:
            raise OSError('publication failed')
    def fail_recovery(path, before):
        if path == cmd:
            raise OSError('recovery storage failure')
        return restore(path, before)
    monkeypatch.setattr(bb, '_atomic', fail_publication)
    monkeypatch.setattr(bb, '_restore_publication', fail_recovery)
    assert bb.main(['install', '--migrate-local-copy', spec['variant'], '--hermes-home', str(hh)]) == 1
    err = capsys.readouterr().err
    receipt = json.loads(err.split('Incomplete builder recovery: ', 1)[1])
    assert receipt['state'] == 'recovery_required'
    assert receipt['failed_paths'] == [str(cmd)]
    assert receipt['original_location'] == str(skill)
    assert receipt['current_target'] is None
    assert receipt['preserved_copy'].startswith(str(osh / 'builder/local-copy-'))
    assert bb._local_snapshot(skill) == spec['entries']
    assert cmd.is_file()  # honest partial failure, not an atomicity claim


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'dangling'])
def test_config_publication_conflict_refuses_before_migration(local, kind):
    osh, hh, skill, spec = local
    cfg = hh / 'config.yaml'
    cfg.unlink()
    if kind == 'directory':
        cfg.mkdir()
    else:
        cfg.symlink_to(hh / ('missing' if kind == 'dangling' else 'owner-config'))
        if kind == 'symlink':
            (hh / 'owner-config').write_text('owner config')
    with pytest.raises(ValueError, match='Unmanaged publication file'):
        bb.install(osh, migrate_local_copy=spec['variant'], hermes_home=hh)
    assert bb._local_snapshot(skill) == spec['entries']
    assert not (osh / 'builder').exists()


def test_preserved_rule_coverage_in_canonical_source():
    text = (bb.SOURCE / 'system-skills/wizard-builder/SKILL.md').read_text()
    for rule in ['both pointerdown and click', 'long-press', '--wos-cutout', 'app-parity-gate',
                 'source serving live today', 'reuse-check-app.yml@main', 'WizardApp.capture',
                 'WizAppReadingPosition', 'inventory pasted', '?open=<app-id>', 'authenticated same-origin',
                 'Android-UA/touch', 'native bridge capability selection', 'Every tap registers before I/O',
                 'before awaiting the write', 'App changes ship in the app']:
        assert rule in text
    assert 'forum-ci-runner-add' not in text  # personal infrastructure, not app source
    views = (bb.SOURCE / 'system-skills/wizard-builder/references/views.md').read_text()
    assert 'Keep custom views only in `~/.wizard-os/views/`' in views


def test_custom_spec_never_claims_upstream_legacy_provenance():
    spec = json.loads((Path(bb.__file__).with_name('builder_local_copy.json')).read_text())
    assert spec['provenance'] == 'reviewed-custom-local-copy'
    assert spec['entries']['SKILL.md']['sha256'] == '49987bfade641b0e848694429635a61522a29aaa4306f62b6003bfa6e9846265'
    assert spec['entries']['references/system-functions.md']['sha256'] == '2a4e27d6993243765191c456d6e18c2e924b43f522e7c7a306287e747c647e89'
    assert spec['entries']['.']['mode'] == 0o775
    assert spec['entries']['SKILL.md']['mode'] == 0o664
