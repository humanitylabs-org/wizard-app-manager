"""An app whose GitHub repo moved to a new name or owner keeps updating (wizard-app-manager#578)."""
import json
from types import SimpleNamespace

import pytest

from wam import app_updates, cli, private, repo_moves, store_selection as ss

OLD, NEW = "oscarhenrycollins/wizard-mail", "humanitylabs-org/wizard-mail"
PIN = "a" * 40
NEW_REC = {"id": "wizard-mail", "repo": NEW, "listed": True,
           "install": {"git": f"https://github.com/{NEW}", "commit": PIN}}
DELISTED = {"id": "wizard-mail", "repo": OLD, "listed": False, "install": {"git": f"https://github.com/{OLD}", "commit": PIN},
            "last_check": {"failures": [f"renamed to {NEW}"]}}


def lookups(old=DELISTED, current=NEW, new=NEW_REC):
    return {"index_get": lambda r: old if r == OLD else dict(new) if r == NEW else pytest.fail(f"index asked for {r}"),
            "github": lambda url: {"full_name": current} if url.endswith("/repos/" + OLD.replace("/", "%2F"))
            or url.endswith("/repos/" + OLD) else pytest.fail(url),
            "resolve": lambda r: dict(new) if r == NEW else pytest.fail(f"resolved {r}")}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.delenv("WAM_PUBLISHERS", raising=False)
    for d in ("apps/wizardapps/wizard-mail", "repos/wizardapps/apps/wizard-mail"):
        f = tmp_path / d
        f.mkdir(parents=True)
        (f / ".wizard-source").write_text(json.dumps({"repo": OLD, "git": f"https://github.com/{OLD}",
                                                      "private": True, "source": "standard"}) + "\n")
        (f / ".wizard-selection").write_text(json.dumps({"repo": OLD, "commit": PIN}) + "\n")
        (f / ".wizard-commit").write_text(PIN + "\n")
        (f / "config.json").write_text(json.dumps({"id": "wizard-mail", "tipi_version": 7,
                                                   "source": f"https://github.com/{OLD}"}, indent=2) + "\n")
    return SimpleNamespace(home=tmp_path / "home", runtipi_root=tmp_path, index="https://index.test",
                           runtipi=lambda: None)


def _files(box):
    out = {}
    for d in ("apps/wizardapps/wizard-mail", "repos/wizardapps/apps/wizard-mail"):
        out[d] = {n: json.loads((box.runtipi_root / d / n).read_text()) for n in (".wizard-source", ".wizard-selection", "config.json")}
        out[d][".wizard-commit"] = (box.runtipi_root / d / ".wizard-commit").read_text().strip()
    return out


def test_a_moved_repo_is_recorded_under_its_new_name_in_both_folders(box):
    res = repo_moves.follow(box, "wizard-mail", **lookups())
    assert res and res["from"] == OLD and res["to"] == NEW and len(res["changed"]) == 6
    for d, f in _files(box).items():
        assert f[".wizard-source"] == {"repo": NEW, "git": f"https://github.com/{NEW}", "private": True, "source": "standard"}, d
        assert f[".wizard-selection"] == {"repo": NEW, "commit": PIN}, d   # the pin keeps its commit
        assert f["config.json"] == {"id": "wizard-mail", "tipi_version": 7, "source": f"https://github.com/{NEW}"}, d
        assert f[".wizard-commit"] == PIN
    assert repo_moves.follow(box, "wizard-mail", **lookups()) is None   # done once; the new name is listed


@pytest.mark.parametrize("case", ["still listed", "no redirect", "other repo wanted", "other app id",
                                  "not first-party", "new name not listed", "index down", "github down"])
def test_never_follows_unless_github_and_the_index_agree_on_the_same_app(box, case):
    kw, want = lookups(), None
    if case == "still listed":
        kw = lookups(old={**DELISTED, "listed": True})
    elif case == "no redirect":
        kw = lookups(current=OLD)
    elif case == "other repo wanted":
        want = "humanitylabs-org/other"
    elif case == "other app id":
        kw = lookups(new={**NEW_REC, "id": "evil-mail"})
    elif case == "not first-party":
        kw = lookups(current="evil-org/wizard-mail")
        kw["resolve"] = lambda r: {**NEW_REC, "repo": r}
    elif case == "new name not listed":
        kw["resolve"] = lambda r: (_ for _ in ()).throw(RuntimeError("not listed"))
    elif case == "index down":
        kw["index_get"] = lambda r: (_ for _ in ()).throw(OSError("down"))
    elif case == "github down":
        kw["github"] = lambda url: (_ for _ in ()).throw(OSError("down"))
    before = _files(box)
    assert repo_moves.follow(box, "wizard-mail", want=want, **kw) is None
    assert _files(box) == before


@pytest.mark.parametrize("src", [{"repo": OLD, "source": "owner-private", "private": True},
                                 {"repo": OLD, "source": "link", "private": True},
                                 {"repo": OLD, "local": True}])
def test_private_local_and_link_installs_keep_their_repo(box, src):
    (box.runtipi_root / "apps/wizardapps/wizard-mail/.wizard-source").write_text(json.dumps(src))
    assert repo_moves.follow(box, "wizard-mail", **lookups()) is None


def test_current_name_follows_githubs_redirect_and_fails_soft():
    assert repo_moves.current_name(OLD, lambda url: {"full_name": NEW}) == NEW
    assert repo_moves.current_name(OLD, lambda url: (_ for _ in ()).throw(OSError("x"))) is None
    assert repo_moves.current_name("../etc", lambda url: pytest.fail("bad name fetched")) is None
    assert repo_moves.current_name(OLD, lambda url: {"full_name": "../x"}) is None


def test_store_selection_of_the_new_repo_is_no_longer_a_conflict(box, monkeypatch):
    """Was: 'installed repository conflict; nothing changed' (store_selection.execute)."""
    monkeypatch.setattr(ss.index, "resolve", lambda index, ref: dict(NEW_REC) if ref == NEW else pytest.fail(ref))
    monkeypatch.setattr(ss.index, "_get", lambda url: DELISTED if url.endswith("/v1/apps/" + OLD) else pytest.fail(url))
    monkeypatch.setattr(repo_moves, "_github_json", lambda url: {"full_name": NEW})
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-mail"})
    calls = []

    def prepare(root, store, repo, **kw):
        calls.append(repo)
        folder = root / "repos" / store / "apps" / "wizard-mail"
        (folder / ".wizard-source").write_text(json.dumps({"repo": repo, "source": kw["source"]}))
        return dict(NEW_REC)

    def update(c, app_id):
        live = c.runtipi_root / "apps/wizardapps" / app_id
        (live / ".wizard-source").write_text(json.dumps({"repo": NEW, "source": "standard"}))
        return {"status": "running"}

    monkeypatch.setattr(private, "prepare", prepare)
    monkeypatch.setattr(cli, "app_update", update)
    monkeypatch.setattr(cli, "write_registry", lambda c: None)
    sel = ss.resolve(box, "wizard-mail", {"source": "standard", "repo": NEW, "commit": PIN, "operation": "update"})
    assert ss.execute(box, "wizard-mail", sel)["status"] == "running"
    assert calls == [NEW]


def test_store_selection_of_an_unrelated_repo_stays_refused(box, monkeypatch):
    monkeypatch.setattr(ss.index, "resolve", lambda index, ref: {**NEW_REC, "repo": ref})
    monkeypatch.setattr(ss.index, "_get", lambda url: DELISTED)
    monkeypatch.setattr(repo_moves, "_github_json", lambda url: {"full_name": NEW})
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-mail"})
    monkeypatch.setattr(private, "prepare", lambda *a, **k: pytest.fail("must not prepare"))
    sel = ss.resolve(box, "wizard-mail", {"source": "standard", "repo": "humanitylabs-org/mail-fork",
                                          "commit": PIN, "operation": "switch"})
    with pytest.raises(ValueError, match="repository conflict"):
        ss.execute(box, "wizard-mail", sel)


def test_app_updates_follow_moves_before_choosing_what_is_due(box, monkeypatch):
    order = []
    rt = SimpleNamespace(installed=lambda: order.append("installed") or [])
    box.runtipi = lambda: rt
    monkeypatch.setattr(app_updates, "_release_managed", lambda: set())
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: None)
    res = app_updates.apply(box, notify=False, renames=lambda c, rt: [],
                            moves=lambda c, installed, only=None: order.append("moves") or
                            [{"id": "wizard-mail", "from": OLD, "to": NEW, "changed": []}])
    assert order[:3] == ["installed", "moves", "installed"]
    assert res["repo_moved"][0]["to"] == NEW
    dry = app_updates.apply(box, dry_run=True, moves=lambda *a, **k: pytest.fail("dry run must not change files"))
    assert dry["dry_run"]


def test_follow_all_checks_only_installed_wizard_store_apps(box, monkeypatch):
    seen = []
    monkeypatch.setattr(repo_moves, "follow", lambda c, app_id, **kw: seen.append(app_id) or None)
    rows = [{"app": {"appName": "wizard-mail", "appStoreSlug": "wizardapps"}},
            {"app": {"appName": "nextcloud", "appStoreSlug": "migrated"}},
            {"app": {"appName": "files", "appStoreSlug": "wizardapps"}}]
    assert repo_moves.follow_all(box, rows) == [] and seen == ["wizard-mail", "files"]
    seen.clear()
    repo_moves.follow_all(box, rows, only=["files"])
    assert seen == ["files"]
