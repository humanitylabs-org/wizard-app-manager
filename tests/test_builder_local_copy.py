"""Opt-in custom-copy migration; no personal fixture/source or live writes."""
import json
import os
from pathlib import Path

import pytest

from wam import builder_bundle as bb


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
