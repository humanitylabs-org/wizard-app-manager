"""Bounded retention of the backups `wam app update` and `wam hermes update` make."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import alongside, cli, update_backups
from wam.runtipi import RuntipiError

HOUR_MS = 3600 * 1000
URN = "notes:" + cli.STORE
OTHER = "music:" + cli.STORE


class FakeRuntipi:
    """Runtipi's app lifecycle + per-app backup API, in memory. Backups are per urn, like Runtipi's folders."""

    def __init__(self, status="running", make_backup=True):
        self.status = status
        self.make_backup = make_backup
        self.backups: dict[str, list[dict]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.list_error = None
        self.delete_error = None

    def seed(self, urn, *ages_hours):
        now = int(time.time() * 1000)
        for n, age in enumerate(ages_hours):
            date = now - int(age * HOUR_MS)
            self.backups.setdefault(urn, []).append({"id": f"{urn.split(':')[0]}-old-{n}.tar.gz", "size": 1, "date": date})

    def ids(self, urn):
        return sorted(b["id"] for b in self.backups.get(urn, []))

    def app(self, urn):
        return {"app": {"status": "running", "version": "1"}}

    def update(self, urn, perform_backup=True):
        if perform_backup and self.make_backup:
            self.backups.setdefault(urn, []).append(
                {"id": f"{urn.split(':')[0]}-new.tar.gz", "size": 1, "date": int(time.time() * 1000)})

    def wait_status(self, urn, wanted, timeout=0, **kw):
        return self.status

    def start(self, urn):
        pass

    def list_backups(self, urn):
        if self.list_error:
            raise self.list_error
        return sorted((dict(b) for b in self.backups.get(urn, [])), key=lambda b: -b["date"])

    def delete_backup(self, urn, filename):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append((urn, filename))
        self.backups[urn] = [b for b in self.backups.get(urn, []) if b["id"] != filename]
        return {}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {"ok": True, "errors": []})
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(alongside, "load", lambda *a, **k: None)
    rt = FakeRuntipi()
    c = SimpleNamespace(home=tmp_path / "wos", runtipi_root=tmp_path / "runtipi", runtipi=lambda: rt,
                        update_backups=update_backups.settings({}))
    return c, rt


def test_keeps_newest_and_removes_only_backups_older_than_12_hours(box):
    c, rt = box
    rt.seed(URN, 1, 11.5, 13, 72)          # two recent (kept), two past the age (removed)
    out = cli.app_update(c, "notes")
    assert out["status"] == "running" and "error" not in out
    assert rt.ids(URN) == ["notes-new.tar.gz", "notes-old-0.tar.gz", "notes-old-1.tar.gz"]
    assert sorted(out["backups_pruned"]) == ["notes-old-2.tar.gz", "notes-old-3.tar.gz"]


def test_newest_backup_always_survives_even_when_it_is_old(box):
    c, rt = box
    rt.seed(URN, 30, 50)
    out = update_backups.prune_app(rt, URN, started_ms=0, now_ms=int(time.time() * 1000) + 100 * HOUR_MS)
    assert out["deleted"] == ["notes-old-1.tar.gz"] and rt.ids(URN) == ["notes-old-0.tar.gz"]


def test_a_pair_made_minutes_apart_is_not_raced(box):
    c, rt = box
    rt.seed(URN, 0.05)                      # a backup made three minutes ago by a previous update
    cli.app_update(c, "notes")
    assert rt.ids(URN) == ["notes-new.tar.gz", "notes-old-0.tar.gz"] and rt.deleted == []


@pytest.mark.parametrize("status", ["update_error", "missing"])
def test_failed_update_prunes_nothing(box, status):
    c, rt = box
    rt.status = status
    rt.seed(URN, 13, 72)
    before = rt.ids(URN)
    out = cli.app_update(c, "notes")
    assert rt.deleted == [] and set(before) <= set(rt.ids(URN)) and "backups_pruned" not in out


def test_app_that_did_not_come_back_prunes_nothing(box):
    c, rt = box
    rt.status = "stopped"                   # it was running before; start() leaves it stopped
    rt.seed(URN, 13, 72)
    out = cli.app_update(c, "notes")
    assert out.get("error") and rt.deleted == []


def test_update_raising_prunes_nothing(box):
    c, rt = box
    rt.seed(URN, 13, 72)

    def boom(*a, **k):
        raise RuntipiError("queue refused")
    rt.update = boom
    with pytest.raises(RuntipiError):
        cli.app_update(c, "notes")
    assert rt.deleted == []


def test_no_backup_update_or_no_fresh_backup_prunes_nothing(box):
    c, rt = box
    rt.seed(URN, 13, 72)
    cli.app_update(c, "notes", backup=False)
    assert rt.deleted == []
    rt.make_backup = False                  # Runtipi accepted the request but made no new copy
    out = cli.app_update(c, "notes")
    assert rt.deleted == [] and "backups_pruned" not in out


def test_other_apps_backups_are_untouched(box):
    c, rt = box
    rt.seed(URN, 13)
    rt.seed(OTHER, 13, 72, 500)
    cli.app_update(c, "notes")
    assert all(urn == URN for urn, _ in rt.deleted)
    assert rt.ids(OTHER) == ["music-old-0.tar.gz", "music-old-1.tar.gz", "music-old-2.tar.gz"]


def test_backup_named_by_a_restore_journal_is_kept(box):
    c, rt = box
    rt.seed(URN, 13, 72)
    rt.seed(OTHER, 72)
    journal = c.home / "restores" / "20261001T000000Z" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"steps": [{"kind": "app_snapshot", "urn": URN, "backup": "notes-old-1.tar.gz"},
                                             {"kind": "app_snapshot", "urn": OTHER, "backup": "notes-old-0.tar.gz"}]}))
    out = cli.app_update(c, "notes")
    assert out["backups_pruned"] == ["notes-old-0.tar.gz"]
    assert rt.ids(URN) == ["notes-new.tar.gz", "notes-old-1.tar.gz"]


def test_unreadable_restore_journal_prunes_nothing(box):
    c, rt = box
    rt.seed(URN, 13, 72)
    journal = c.home / "restores" / "20261001T000000Z" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{")
    out = cli.app_update(c, "notes")
    assert rt.deleted == [] and "restore journals" in out["backup_prune_warning"] and "error" not in out


@pytest.mark.parametrize("where", ["list", "delete"])
def test_backup_api_errors_only_warn(box, capsys, where):
    c, rt = box
    rt.seed(URN, 13, 72)
    if where == "list":
        rt.list_error = RuntipiError("GET /backups -> 500")
    else:
        rt.delete_error = RuntipiError("DELETE /backups -> 500")
    out = cli.app_update(c, "notes")
    assert out["status"] == "running" and "error" not in out
    assert "500" in out["backup_prune_warning"]
    assert "old backups were not all removed" in capsys.readouterr().err
    assert len(rt.ids(URN)) == 3


def test_retention_comes_from_app_manager_json(tmp_path, monkeypatch):
    home = tmp_path / "wos"
    home.mkdir()
    (home / "app-manager.json").write_text(json.dumps({"update_backups": {"keep": 2, "app_max_age_hours": 48}}))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setattr(cli.release_pins, "load", lambda *a, **k: None)
    assert cli.Config().update_backups == {"keep": 2, "app_max_age_hours": 48, "hermes_max_age_days": 30}
    assert update_backups.settings({"update_backups": {"keep": 0, "app_max_age_hours": "x"}}) == \
        update_backups.settings({})


def test_keep_setting_keeps_more_than_one(box):
    c, rt = box
    c.update_backups = update_backups.settings({"update_backups": {"keep": 2}})
    rt.seed(URN, 13, 72)
    cli.app_update(c, "notes")
    assert rt.ids(URN) == ["notes-new.tar.gz", "notes-old-0.tar.gz"]


# ---------------------------------------------------------------- Hermes update snapshots

def _snap(d: Path, days_ago: float, suffix: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - days_ago * 86400))
    p = d / f"hermes-{stamp}-{suffix}"
    p.mkdir(parents=True)
    (p / "snapshot.json").write_text("{}")
    return p


def test_hermes_snapshots_keep_newest_and_expire_after_30_days(tmp_path):
    d = tmp_path / "backups"
    new = _snap(d, 0, "aaaaaaaa")
    recent = _snap(d, 20, "bbbbbbbb")
    old = _snap(d, 31, "cccccccc")
    older = _snap(d, 90, "dddddddd")
    foreign = d / "hermes-notes"; foreign.mkdir()                    # not a wam snapshot name
    other = d / "bundle-20200101"; other.mkdir()                     # wam backup's own files
    link = d / "hermes-20200101T000000Z-eeeeeeee"; link.symlink_to(other)
    out = update_backups.prune_hermes(d, protect=new)
    assert sorted(out["deleted"]) == sorted([old.name, older.name]) and out["errors"] == []
    assert new.is_dir() and recent.is_dir() and foreign.is_dir() and other.is_dir() and link.is_symlink()
    assert not old.exists() and not older.exists()


def test_hermes_only_snapshot_is_kept_even_when_old(tmp_path):
    only = _snap(tmp_path, 400, "aaaaaaaa")
    assert update_backups.prune_hermes(tmp_path)["deleted"] == [] and only.is_dir()


def test_hermes_prune_errors_are_reported_not_raised(tmp_path, monkeypatch):
    _snap(tmp_path, 0, "aaaaaaaa"); _snap(tmp_path, 60, "bbbbbbbb")
    monkeypatch.setattr(update_backups.shutil, "rmtree", lambda p: (_ for _ in ()).throw(PermissionError("denied")))
    out = update_backups.prune_hermes(tmp_path)
    assert out["deleted"] == [] and "denied" in out["errors"][0]
    assert update_backups.prune_hermes(tmp_path / "missing")["errors"]
