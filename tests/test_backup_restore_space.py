"""Restore capacity must be refused before any stop, pointer or owner mutation."""
import zipfile

from test_backup_flow import world, isolated, _export, _cli
from wam.backup import restore
from wam.backup.box import box_digest


def test_restore_counts_installed_package_and_config_before_staging(world):
    out, _ = _export(world)
    new = world['new']
    # Existing preflight counted only app-data, not the app package Undo copy.
    pkg = new.runtipi / 'apps/wizardapps/files-app/large-package'
    pkg.parent.mkdir(parents=True, exist_ok=True)
    with pkg.open('wb') as f:
        f.truncate(600 << 20)
    new.free = 500 << 20
    before = box_digest(new.box())
    calls = list(new.rt.calls)
    code, res = _cli(new.box(), 'import', str(out), '--yes', '--key-file', str(world['keyfile']))
    assert code == 1 and 'not enough free space' in res['error'], res
    assert 'undo' in res['error'].lower()
    assert 'nothing was changed' in res['error'].lower()
    assert new.rt.calls == calls
    assert box_digest(new.box()) == before
    assert not (new.home / 'restores').exists()


def test_expanded_agent_zip_refused_after_staging_without_replacing_last(world, monkeypatch):
    out, _ = _export(world)
    new = world['new']
    # Keep early estimate sufficient; inject a highly compressed large agent ZIP
    # only into verified staging to exercise the exact post-stage budget.
    original = restore.stage
    def staged(*args, **kwargs):
        r, res = original(*args, **kwargs)
        z = r.root / 'stage/agents/hermes/hermes-backup.zip'
        with zipfile.ZipFile(z, 'a', compression=zipfile.ZIP_DEFLATED) as f:
            f.writestr('large.bin', b'0' * (8 << 20))
        return r, res
    monkeypatch.setattr(restore, 'stage', staged)
    monkeypatch.setattr(restore, 'MARGIN', 1 << 20)
    new.free = 5 << 20
    previous = new.home / 'restores/previous'
    previous.mkdir(parents=True)
    (previous / 'journal.json').write_text('{"stamp":"previous","status":"undone"}')
    pointer = new.home / 'restores/last.json'
    pointer.write_text('{"stamp":"previous"}')
    saved = pointer.read_bytes(), (previous / 'journal.json').read_bytes()
    before = box_digest(new.box())
    calls = list(new.rt.calls)
    code, res = _cli(new.box(), 'import', str(out), '--yes', '--key-file', str(world['keyfile']))
    assert code == 1 and 'not enough free space' in res['error'], res
    assert new.rt.calls == calls
    assert box_digest(new.box()) == before
    assert (pointer.read_bytes(), (previous / 'journal.json').read_bytes()) == saved
    assert sorted(p.name for p in (new.home / 'restores').iterdir()) == ['last.json', 'previous']


def test_restore_budget_adds_writes_on_same_disk(world, monkeypatch):
    box = world['new'].box()
    manifest = {'estimate_bytes': 100, 'files': {'included': True, 'bytes': 30}, 'apps': [], 'agents': []}
    wants = []
    monkeypatch.setattr(restore, 'need_spaces', lambda b, rows: wants.extend(rows))
    monkeypatch.setattr(restore, 'MARGIN', 10)
    restore._space_check(box, restore._Restore(box), manifest)
    # 100 outer staging + 70 expanded app/agent data + margins; same-disk
    # aggregation is delegated to the existing export helper.
    assert sum(n for _, n, _ in wants) >= 190


def test_undo_budget_includes_all_app_folders_and_named_volumes(world, monkeypatch):
    new = world['new']
    box = new.box()
    app = {'id': 'files-app', 'store': 'wizardapps', 'urn': 'files-app:wizardapps', 'data': 'archive'}
    monkeypatch.setattr(box.appdata, 'volume_bytes', lambda names: 12345 * len(names))
    monkeypatch.setattr(box.appdata, 'volumes', lambda *args: ['shared'])
    p = {'install': [], 'replace': ['files-app'], 'kept_here': []}
    n = restore._undo_bytes(box, {'apps': [app]}, p)
    assert n >= 12345
    extra = new.runtipi / 'user-config/wizardapps/files-app/extra'
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b'x' * 5000)
    assert restore._undo_bytes(box, {'apps': [app]}, p) >= n + 5000


def test_volume_measurement_is_read_only_and_counts_sparse_logical_size(world):
    from pathlib import Path
    box = world['new'].box()
    volume = Path(box.env['FAKE_VOLUMES']) / 'sparse-volume'
    volume.mkdir(parents=True)
    with (volume / 'large').open('wb') as f:
        f.truncate(32 << 20)
    (volume / 'outside').symlink_to(world['old'].home, target_is_directory=True)
    rows = box.appdata.volume_usage(['sparse-volume'])
    assert len(rows) == 1 and rows[0]['bytes'] >= 32 << 20
    assert rows[0]['bytes'] < 33 << 20  # symlink was not followed
    assert rows[0]['free'] > 0


def test_named_volume_space_refused_before_stopping_or_pointer(world, monkeypatch):
    from wam.backup.appdata import AppData
    out, _ = _export(world)
    new = world['new']
    original = restore.stage
    def staged(*args, **kwargs):
        r, result = original(*args, **kwargs)
        for a in r.journal['manifest']['apps']:
            if a['id'] == 'files-app':
                a['volumes'] = ['full-volume']
        return r, result
    monkeypatch.setattr(restore, 'stage', staged)
    monkeypatch.setattr(AppData, 'volume_exists', lambda self, name: True)
    monkeypatch.setattr(AppData, 'volume_usage', lambda self, names: [
        {'name': n, 'bytes': 10, 'device': 99999, 'free': 0} for n in names])
    before = box_digest(new.box())
    calls = list(new.rt.calls)
    code, res = _cli(new.box(), 'import', str(out), '--yes', '--key-file', str(world['keyfile']))
    assert code == 1 and 'named volumes' in res['error'], res
    assert 'Nothing was changed' in res['error']
    assert new.rt.calls == calls and box_digest(new.box()) == before
    assert not (new.home / 'restores').exists()

