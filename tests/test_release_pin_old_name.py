"""A release pin naming a moved repo's OLD name still applies on a box that already follows the NEW name (#1047).

files, transcriber, secret-drop and wizard-claude moved from oscarhenrycollins to humanitylabs-org; `wam app-updates`
records the new name on every box, while releases kept pinning the old one, so the release-defaults loop skipped them
without a word."""
import json
from types import SimpleNamespace

import pytest

from wam import cli, release_pins, repo_moves

OLD, NEW = "oscarhenrycollins/files", "humanitylabs-org/files"
HAVE, WANT = "a" * 40, "b" * 40


def github(names):
    """GitHub's /repos/<name> answer: the name it serves the repo under now."""
    def get(url):
        repo = url.split("/repos/", 1)[1].replace("%2F", "/")
        return {"full_name": names.get(repo, repo)}
    return get


@pytest.fixture
def box(tmp_path, monkeypatch):
    live = tmp_path / "apps" / cli.STORE / "files"
    live.mkdir(parents=True)
    (live / ".wizard-commit").write_text(HAVE + "\n")
    (live / ".wizard-source").write_text(json.dumps({"repo": NEW, "git": f"https://github.com/{NEW}",
                                                     "private": True, "source": "standard"}) + "\n")
    (live / "wizard.md").write_text("---\nid: files\nversion: 0.1.0\n---\n")
    doc = {"default_apps": {"files": {"app": "files", "repo": OLD, "commit": WANT}}}
    monkeypatch.setattr(release_pins, "load", lambda: doc)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"files"})
    monkeypatch.setattr(repo_moves, "follow", lambda *a, **k: None)   # nothing to follow: the box has the new name
    monkeypatch.setattr(repo_moves, "_github_json", github({OLD: NEW}))
    seen = []

    def resolve_any(c, ref, **kw):
        seen.append((ref, kw))
        raise RuntimeError("stop after the recipe fetch")
    monkeypatch.setattr(cli, "resolve_any", resolve_any)
    return SimpleNamespace(c=SimpleNamespace(runtipi_root=tmp_path), live=live, doc=doc, seen=seen)


def test_a_pin_under_the_old_name_applies_and_fetches_from_the_name_the_box_follows(box):
    failed = {}
    cli.release_updates(box.c, None, failed)
    assert box.seen == [("files", {"pin_repo": NEW})]   # on main: never tried, nothing logged
    assert "files" in failed                             # the stub stopped it; the update was really attempted


def test_resolve_any_fetches_the_pinned_commit_from_the_current_name(monkeypatch):
    monkeypatch.setattr(release_pins, "resolve", lambda ref: {"id": "files", "repo": OLD, "commit": WANT, "image": None})
    calls = []
    monkeypatch.setattr(cli.private, "prepare", lambda root, store, repo, **kw: calls.append((repo, kw)) or {})
    cli.resolve_any(SimpleNamespace(runtipi_root=None), "files", pin_repo=NEW)
    cli.resolve_any(SimpleNamespace(runtipi_root=None), "files")
    assert calls[0][0] == NEW and calls[0][1]["source_pin"] == {"id": "files", "repo": NEW, "commit": WANT, "image": None}
    assert calls[1][0] == OLD and calls[1][1]["source_pin"]["repo"] == OLD


@pytest.mark.parametrize("why", ["not-moved", "elsewhere", "third-party", "owner-private", "github-down"])
def test_any_other_mismatch_is_skipped_with_a_line_naming_the_app(box, monkeypatch, capsys, why):
    src = json.loads((box.live / ".wizard-source").read_text())
    if why == "not-moved":
        monkeypatch.setattr(repo_moves, "_github_json", github({}))
    elif why == "elsewhere":
        monkeypatch.setattr(repo_moves, "_github_json", github({OLD: "humanitylabs-org/other-files"}))
    elif why == "third-party":
        box.doc["default_apps"]["files"]["repo"] = "someone-else/files"
        monkeypatch.setattr(repo_moves, "_github_json", github({"someone-else/files": NEW}))
    elif why == "owner-private":
        src["source"] = "owner-private"
    elif why == "github-down":
        def down(url):
            raise OSError("no network")
        monkeypatch.setattr(repo_moves, "_github_json", down)
    (box.live / ".wizard-source").write_text(json.dumps(src))
    assert cli.release_updates(box.c, None, {}) == [] and box.seen == []
    err = capsys.readouterr().err
    assert "note: files not moved to the release's source" in err and NEW in err


def test_an_unreadable_install_record_is_named_in_the_log(box, capsys):
    (box.live / ".wizard-source").write_text("not json")
    assert cli.release_updates(box.c, None, {}) == [] and box.seen == []
    assert "note: files not moved to the release's source: its install record can't be read" in capsys.readouterr().err


def test_matching_names_need_no_github_lookup(box, monkeypatch):
    box.doc["default_apps"]["files"]["repo"] = NEW

    def no_github(url):
        raise AssertionError("no GitHub call when the names match")
    monkeypatch.setattr(repo_moves, "_github_json", no_github)
    cli.release_updates(box.c, None, {})
    assert box.seen == [("files", {})]


def test_defaults_name_the_org_repos_of_moved_apps():
    slots = json.loads((cli.Path(cli.__file__).parent / "defaults.json").read_text())["slots"]
    repos = {k: v.get("repo") for k, v in slots.items()}
    assert repos["files"] == "humanitylabs-org/files"
    assert repos["claude-subscription"] == "humanitylabs-org/claude-connection"
    assert repos["cloudflare-connection"] == "humanitylabs-org/wizard-cloudflare"
