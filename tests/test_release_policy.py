"""James's developer-only policy; real git, Docker mocked at the boundary."""
import json
from unittest.mock import patch

import pytest

from wam import release, release_policy as policy
from test_release import box, git, commit, _owner_change, _upstream_change


def context(b):
    return release.ctx(b.home, pr_lookup=b.lookup)


def proposal(b, number=10, path="web/proposed.js"):
    sha = commit(b.src, path, "proposed\n", "Proposed change")
    git(b.src, "push", "-q", "origin", "HEAD")
    b.prs[str(number)] = {"state": "open", "title": "Proposed change", "head": {"sha": sha}}
    return policy.register(context(b), "proposed", f"https://github.com/fixture/wizard-ux/pull/{number}")


def test_developer_mode_default_off_and_required(box):
    policy.set_developer(False, box.home)
    with pytest.raises(SystemExit, match="Developer mode"):
        box.init(channel="main")
    assert not (box.home / release.CONFIG).exists()
    policy.set_developer(True, box.home)
    box.init(channel="main")
    policy.set_developer(False, box.home)
    assert box.run("rebuild")["state"] == "failed"
    assert box.docker.calls == []


def test_adopt_register_pr_squash_safe_dropout_no_rewrite(box):
    box.init(channel="main")
    item = proposal(box)
    head = git(box.src, "rev-parse", "HEAD")
    assert policy.items(context(box))[0]["status"] == "proposed"
    assert git(box.src, "rev-parse", "HEAD") == head
    box.prs["10"].update(state="closed", merged_at="2026-10-27T10:00:00Z", merge_commit_sha="f" * 40)
    assert policy.items(context(box)) == []
    assert git(box.src, "rev-parse", "HEAD") == head


def test_agent_must_propose_owner_only_mine_cap_five(box):
    box.init(channel="main")
    sha = git(box.src, "rev-parse", "HEAD")
    with pytest.raises(release.GitError, match="open PR"):
        policy.register(context(box), "proposed", sha=sha)
    with pytest.raises(release.GitError, match="owner at an interactive terminal"):
        policy.register(context(box), "mine", sha=sha)
    with patch.object(policy, "owner_confirm"):
        for n in range(5):
            sha = commit(box.src, f"web/mine{n}.js", "mine", f"Mine {n}")
            policy.register(context(box), "mine", sha=sha)
        sha = commit(box.src, "web/six.js", "no", "Sixth mine")
        with pytest.raises(release.GitError, match="five"):
            policy.register(context(box), "mine", sha=sha)


def test_refuse_unpushed_dirty_and_unregistered(box):
    box.init(channel="main")
    commit(box.src, "web/private.js", "private", "private")
    assert "not pushed" in box.run("rebuild")["status"]
    git(box.src, "push", "-q", "origin", "HEAD")
    assert "unregistered" in box.run("rebuild")["status"]
    (box.src / "dirty").write_text("untracked")
    assert "untracked" in box.run("rebuild")["status"]
    assert box.docker.calls == []


def feature(b, path="web/feature.js", labels=("feature",)):
    sha = _upstream_change(b, path, "feature\n", "Feature")
    b.prs[sha] = {"merged_at": "2026-10-27T00:00:00Z", "merge_commit_sha": sha,
                  "title": path, "labels": [{"name": n} for n in labels]}
    return sha


def test_feature_before_fix_is_held_fix_applies_even_ask_mode_then_partial_go(box):
    box.init(channel="main")
    proposal(box)
    first = feature(box)
    second = feature(box, "web/second.js", labels=())  # unlabelled = feature
    fix = _upstream_change(box, "web/fix.js", "fixed\n", "Fix")
    result = box.run("auto")
    assert result["state"] == "ok", result
    assert (box.src / "web/fix.js").read_text() == "fixed\n"
    assert not (box.src / "web/feature.js").exists()
    assert not (box.src / "web/second.js").exists()
    assert git(box.src, "merge-base", "--is-ancestor", fix, "HEAD") == ""
    staged = release._read_json(release.paths(box.home).dir / "staging.json")
    assert staged["state"] == "staged" and set(staged["items"]) == {first, second}
    n = len(box.docker.builds())
    assert box.run("auto")["state"] == "waiting"
    assert len(box.docker.builds()) == n, "same batch built once"
    go = box.run("go", approve=[first])
    assert go["state"] == "ok", go
    assert (box.src / "web/feature.js").exists()
    assert not (box.src / "web/second.js").exists()
    assert [x["sha"] for x in go["held"]] == [second]
    go = box.run("go")
    assert go["state"] == "ok", go
    assert (box.src / "web/second.js").exists()
    label = next(x for x in box.docker.builds()[-1] if x.startswith("org.opencontainers.image.revision="))
    assert label.split("=", 1)[1] == git(box.src, "rev-parse", "HEAD")


def test_wam_itself_refused(box):
    box.init(channel="main")
    c = context(box)
    c.cfg["app"] = "wizard-app-manager"
    with pytest.raises(release.GitError, match="WAM"):
        policy.require(c)
    c.cfg["app"] = "wizard-ux"
    commit(box.src, "wam/cli.py", "# WAM", "not a shell")
    with pytest.raises(release.GitError, match="WAM"):
        policy.require(c)


def test_conflict_keeps_running_build_and_branch(box):
    box.init(channel="main")
    _owner_change(box, "web/app.js", "owner\n", "Owner app")
    assert box.run("rebuild")["state"] == "ok"
    before = release._read_json(release.paths(box.home).built)
    head = git(box.src, "rev-parse", "HEAD")
    _upstream_change(box, "web/app.js", "upstream\n", "Fix app")
    assert box.run("auto")["state"] == "conflict"
    assert release._read_json(release.paths(box.home).built) == before
    assert git(box.src, "rev-parse", "HEAD") == head


def test_core_resolver_stages_never_live_until_exact_go(box):
    box.init(channel="main")
    _owner_change(box, "web/app.js", "owner\n", "Owner app")
    assert box.run("rebuild")["state"] == "ok"
    before = release._read_json(release.paths(box.home).built)
    _upstream_change(box, "web/app.js", "upstream\n", "Fix app")
    def resolver(w, ours, theirs):
        (w / "web/app.js").write_text("resolved\n")
        git(w, "add", "-A"); git(w, "commit", "-q", "-m", "Resolve preserving both parents")
        return True
    got = box.run("auto", resolver=resolver)
    assert got["state"] == "staged", got
    assert release._read_json(release.paths(box.home).built) == before
    stage = release._read_json(release.paths(box.home).dir / "staging.json")
    assert stage["resolver"]["core"] is True
    assert release._read_json(release.paths(box.home).dir / "resolvers.json")["merges"]
    go = box.run("go", resolver=lambda *args: pytest.fail("must use reviewed resolver result"))
    assert go["state"] == "ok", go
    assert git(box.src, "rev-parse", "HEAD") == stage["source_revision"]
