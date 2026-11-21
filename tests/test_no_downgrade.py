"""Updates never install a lower app version than the one running (wizard-app-manager#914).

On 21 Nov the hourly updater replaced Photos 0.4.1 (humanitylabs-org/photos) with Photos 0.2.0 from the moved repo's
stale pre-transfer listing (oscarhenrycollins/photos): a different commit always gets a higher tipi_version, so the
older commit counted as an update. The GitHub lookup that should have followed the move had failed silently.
"""
import json
import urllib.error
from types import SimpleNamespace

import pytest

from wam import app_updates as au, cli, private, repo_moves, store_selection as ss

OLD, NEW = "oscarhenrycollins/photos", "humanitylabs-org/photos"
PIN = "b" * 40


def recipe(folder, version, tipi, repo=NEW, wizard_version=None):
    folder.mkdir(parents=True, exist_ok=True)
    cfg = {"id": "photos", "name": "Photos", "tipi_version": tipi, "source": f"https://github.com/{repo}"}
    if version is not None:
        cfg["version"] = version
    (folder / "config.json").write_text(json.dumps(cfg))
    if wizard_version is not None:
        (folder / "wizard.md").write_text(f"---\nid: photos\nversion: {wizard_version}\n---\n# Photos\n")
    (folder / ".wizard-source").write_text(json.dumps({"repo": repo, "source": "standard", "private": True}))
    (folder / ".wizard-commit").write_text(PIN + "\n")


def row(app_id, have, latest):
    return {"app": {"appName": app_id, "appStoreSlug": "wizardapps", "version": have, "status": "running"},
            "metadata": {"latestVersion": latest}}


@pytest.mark.parametrize("a,b", [("0.2.0", "0.4.1"), ("0.4.1", "0.4.2"), ("1.0.0-rc.1", "1.0.0"),
                                 ("1.0.0-alpha", "1.0.0-alpha.1"), ("1.0.0-alpha.2", "1.0.0-alpha.10"),
                                 ("1.0.0-alpha.beta", "1.0.0-beta"), ("v0.9", "0.10.0"), ("0.4.1+b7", "0.4.2"),
                                 ("2026.11.9", "2026.11.20")])
def test_version_names_compare_like_semver(a, b):
    assert au.version_key(a) < au.version_key(b)
    assert not au.version_key(b) < au.version_key(a)


def test_equal_and_unreadable_version_names():
    assert au.version_key("1.2") == au.version_key("1.2.0") == au.version_key("v1.2.0+build")
    for bad in (None, "", "latest", "1.x", 3):
        assert au.version_key(bad) is None


def test_the_hourly_run_skips_an_older_version_whatever_tipi_version_says(tmp_path):
    root = tmp_path / "runtipi"
    recipe(root / "apps/wizardapps/photos", "0.4.1", 8)
    recipe(root / "repos/wizardapps/apps/photos", "0.2.0", 9, repo=OLD)   # the stale listing: tipi 9 > 8
    recipe(root / "apps/wizardapps/money", "1.0.0", 3)
    recipe(root / "repos/wizardapps/apps/money", "1.1.0", 4)
    got = {r["id"]: r["skip"] for r in au.candidates([row("photos", 8, 9), row("money", 3, 4)], store="wizardapps",
                                                     runtipi_root=root, home=tmp_path / "home",
                                                     release_managed=set())}
    assert got == {"photos": "older", "money": None}


def test_wizard_md_version_counts_when_config_json_has_none(tmp_path):
    root = tmp_path / "runtipi"
    recipe(root / "apps/wizardapps/photos", None, 8, wizard_version="0.4.1")
    recipe(root / "repos/wizardapps/apps/photos", None, 9, wizard_version="0.2.0")
    assert au.older(root, "wizardapps", "photos") == ("0.2.0", "0.4.1")
    recipe(root / "repos/wizardapps/apps/photos", None, 9, wizard_version="0.4.2")
    assert au.older(root, "wizardapps", "photos") is None


def test_an_explicit_update_by_id_never_goes_lower_either(tmp_path, monkeypatch):
    root = tmp_path / "runtipi"
    recipe(root / "apps/wizardapps/photos", "0.4.1", 8)
    recipe(root / "repos/wizardapps/apps/photos", "0.2.0", 9, repo=OLD)
    rt = SimpleNamespace(installed=lambda: [row("photos", 8, 9)])
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=root, index="https://index.test", runtipi=lambda: rt)
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: None)
    monkeypatch.setattr(cli, "app_update", lambda *a, **k: pytest.fail("an older version was installed"))
    out = au.apply(c, only=["photos"], notify=False, renames=lambda c, rt: [], moves=lambda *a, **k: [],
                   reporter=lambda *a, **k: {})
    assert out["updated"] == [] and [r["skip"] for r in out["skipped"]] == ["older"]


def test_store_update_to_an_older_version_needs_force(tmp_path):
    root = tmp_path / "runtipi"
    recipe(root / "apps/wizardapps/photos", "0.4.1", 8)
    recipe(root / "repos/wizardapps/apps/photos", "0.2.0", 9, repo=OLD)
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=root)
    why = au.store_refusal(c, "photos")
    assert why and "0.2.0 is older than the 0.4.1" in why
    assert au.store_refusal(c, "photos", force=True) is None
    recipe(root / "repos/wizardapps/apps/photos", "0.4.2", 9)
    assert au.store_refusal(c, "photos") is None


def test_wam_app_update_refuses_an_older_version_unless_asked(tmp_path, monkeypatch, capsys):
    root = tmp_path / "runtipi"
    recipe(root / "apps/wizardapps/photos", "0.4.1", 8)
    recipe(root / "repos/wizardapps/apps/photos", "0.2.0", 9, repo=OLD)
    monkeypatch.setattr(cli, "Config", lambda: SimpleNamespace(runtipi_root=root, home=tmp_path / "home"))
    monkeypatch.setattr(cli, "rename_instead_of_update", lambda c, t: None)
    calls = []
    monkeypatch.setattr(cli, "app_update", lambda c, app, **k: calls.append(app) or {"status": "running"})
    assert cli.main(["app", "update", "photos"]) == 1 and calls == []
    assert "older than the installed 0.4.1" in capsys.readouterr().err
    assert cli.main(["app", "update", "photos", "--allow-older"]) == 0 and calls == ["photos"]


# ---------------------------------------------------------------- the move check (#914 points 3 and 4)

STALE = {"id": "photos", "repo": OLD, "listed": True, "install": {"git": f"https://github.com/{OLD}", "commit": "d" * 40}}
NEW_REC = {"id": "photos", "repo": NEW, "listed": True, "install": {"git": f"https://github.com/{NEW}", "commit": PIN}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.delenv("WAM_PUBLISHERS", raising=False)
    for d in ("apps/wizardapps/photos", "repos/wizardapps/apps/photos"):
        recipe(tmp_path / d, "0.4.1", 8, repo=OLD)
    return SimpleNamespace(home=tmp_path / "home", runtipi_root=tmp_path, index="https://index.test",
                           runtipi=lambda: None)


def _down(url):
    raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)


def test_a_failed_github_lookup_is_logged_and_marks_the_app_unchecked(box, capsys):
    unchecked = []
    res = repo_moves.follow(box, "photos", unchecked=unchecked, github=_down,
                            index_get=lambda r: dict(STALE), search=lambda i: [dict(STALE), dict(NEW_REC)],
                            resolve=lambda r: pytest.fail("no lookup, no follow"))
    assert res is None and unchecked == ["photos"]
    assert "WARNING: GitHub lookup of oscarhenrycollins/photos failed (HTTP 403)" in capsys.readouterr().err


def test_an_app_listed_nowhere_else_is_never_marked_unchecked(box):
    unchecked = []
    assert repo_moves.follow(box, "photos", unchecked=unchecked, github=lambda url: pytest.fail("no GitHub call"),
                             index_get=lambda r: dict(STALE), search=lambda i: [dict(STALE)]) is None
    assert unchecked == []


def test_a_deleted_repo_is_an_answer_not_a_failure():
    def gone(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    assert repo_moves.github_name(OLD, gone) == (None, True)
    assert repo_moves.github_name(OLD, lambda url: {"full_name": NEW}) == (NEW, True)
    assert repo_moves.github_name(OLD, _down) == (None, False)


def test_the_github_lookup_uses_the_boxs_login_and_only_on_github(monkeypatch):
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"full_name": NEW}).encode()

    monkeypatch.setattr(repo_moves, "_token", lambda: "tok-123")
    monkeypatch.setattr(repo_moves.urllib.request, "urlopen",
                        lambda req, timeout: sent.append((req.full_url, req.get_header("Authorization"))) or Resp())
    monkeypatch.delenv("WAM_GITHUB_API", raising=False)
    assert repo_moves.current_name(OLD) == NEW
    monkeypatch.setenv("WAM_GITHUB_API", "https://mirror.example")
    repo_moves.current_name(OLD)
    assert sent == [(f"https://api.github.com/repos/{OLD}", "Bearer tok-123"),
                    (f"https://mirror.example/repos/{OLD}", None)]


def test_the_hourly_run_skips_an_app_whose_move_could_not_be_checked(box, monkeypatch):
    """The 21 Nov case: GitHub's anonymous limit was spent, `follow` did nothing, the update ran from the old name."""
    recipe(box.runtipi_root / "repos/wizardapps/apps/photos", "0.4.2", 9, repo=OLD)   # even a newer version waits
    rt = SimpleNamespace(installed=lambda: [row("photos", 8, 9)])
    box.runtipi = lambda: rt
    monkeypatch.setattr(au, "_release_managed", lambda: set())
    monkeypatch.setattr(au, "_installed_unpinned", lambda c, rt: rt.installed())
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: None)
    monkeypatch.setattr(cli, "app_update", lambda *a, **k: pytest.fail("updated from the unchecked old name"))
    monkeypatch.setattr(repo_moves, "_github_json", _down)
    from wam import index
    monkeypatch.setattr(index, "_get", lambda url: dict(STALE) if url.endswith("/v1/apps/" + OLD) else pytest.fail(url))
    monkeypatch.setattr(index, "search", lambda i, **f: [dict(STALE), dict(NEW_REC)])
    out = au.apply(box, notify=False, renames=lambda c, rt: [], reporter=lambda *a, **k: {})
    assert out["updated"] == [] and [(r["id"], r["skip"]) for r in out["skipped"]] == [("photos", "move-unchecked")]


def test_a_store_update_from_a_stale_listing_records_the_repo_github_serves_now(box, monkeypatch):
    """#914 point 2: the selection carried the pre-transfer name; after the update the record must name the new repo."""
    from wam import index
    monkeypatch.setattr(ss.index, "resolve", lambda idx, ref: {**STALE, "install": {**STALE["install"], "commit": PIN}}
                        if ref == OLD else dict(NEW_REC))
    monkeypatch.setattr(index, "_get", lambda url: dict(STALE))
    monkeypatch.setattr(index, "search", lambda i, **f: [dict(STALE), dict(NEW_REC)])
    monkeypatch.setattr(repo_moves, "_github_json", lambda url: {"full_name": NEW})
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"photos"})
    monkeypatch.setattr(au, "_release_managed_strict", lambda: set())

    def prepare(root, store, repo, **kw):
        folder = root / "repos" / store / "apps" / "photos"
        (folder / ".wizard-source").write_text(json.dumps({"repo": repo, "source": kw["source"], "private": True}))
        return {}

    def update(c, app_id, **k):   # Runtipi copies the prepared recipe into apps/
        live = c.runtipi_root / "apps/wizardapps" / app_id
        repo = c.runtipi_root / "repos/wizardapps/apps" / app_id
        for n in (".wizard-source", ".wizard-commit", "config.json"):
            (live / n).write_bytes((repo / n).read_bytes())
        return {"status": "running"}

    monkeypatch.setattr(private, "prepare", prepare)
    monkeypatch.setattr(cli, "write_registry", lambda c: None)
    monkeypatch.setattr(au, "store_update", lambda c, app_id, fn: fn())
    monkeypatch.setattr(cli, "app_update", update)
    sel = ss.resolve(box, "photos", {"source": "standard", "repo": OLD, "commit": PIN, "operation": "update"})
    ss.execute(box, "photos", sel)
    live = json.loads((box.runtipi_root / "apps/wizardapps/photos/.wizard-source").read_text())
    assert live["repo"] == NEW
    cfg = json.loads((box.runtipi_root / "apps/wizardapps/photos/config.json").read_text())
    assert cfg["source"] == f"https://github.com/{NEW}"
