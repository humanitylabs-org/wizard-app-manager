"""A failed app update tells the team (#524): file a bug in the app's repo, or comment on the open one.

Behind the off-by-default feature switch `oscar/update-bug-reports` (wam/update_reports.py)."""
import json
import urllib.error

from wam import app_updates as au
from wam import update_reports as ur

ROW = {"id": "wizard-messenger", "name": "Messenger", "from": 93, "to": 97, "to_version": "0.9.7",
       "reason": "the app did not answer after the update", "step": "check after update",
       "rollback": {"rolled_back": True, "status": "running", "version": 93}}


class FakeGitHub:
    def __init__(self, open_bugs=(), fail=None, private=False):
        self.open_bugs, self.fail, self.calls, self.private = list(open_bugs), fail, [], private

    def __call__(self, method, path, tok, body=None):
        self.calls.append((method, path, body))
        assert tok == "tok-123"
        if self.fail:
            raise self.fail
        if method == "GET" and "/issues" not in path:
            return {"private": self.private}
        if method == "GET":
            return self.open_bugs
        if path.endswith("/comments"):
            return {"html_url": "https://github.com/o/r/issues/7#issuecomment-1"}
        return {"html_url": "https://github.com/o/r/issues/8", "number": 8}


def recorded(tmp_path, app="wizard-messenger", repo="humanitylabs-org/wizard-messenger"):
    d = tmp_path / "apps" / "wizardapps" / app
    d.mkdir(parents=True)
    (d / ".wizard-source").write_text(json.dumps({"repo": repo}))

    class C:
        runtipi_root = tmp_path
    return C()


def test_draft_says_box_app_versions_step_and_error():
    d = ur.draft(ROW, "oscar-main")
    assert d["title"] == "App update failed: Messenger 93 to 97 (check after update)"
    for want in ("Box: oscar-main", "Messenger (`wizard-messenger`)", "93 -> 97 (0.9.7)", "Failing step: check after update",
                 "rolled back to the previous version", "Error: the app did not answer"):
        assert want in d["body"], want
    assert d["mark"] in d["body"] and d["key"] == "wizard-messenger:97:check after update"
    assert ur.step_of({**ROW, "rollback": {"rolled_back": False}}) == "rollback"


def test_error_text_is_scrubbed_of_private_paths_and_hosts():
    home = "/" + "home" + "/someone"
    host = ".".join(["box", "tail0000", "ts", "net"])
    ip = ".".join(["100", "64", "1", "2"])
    got = ur.scrub(f"open {home}/runtipi/x failed on {host} ({ip})")
    assert "someone" not in got and host not in got and ip not in got
    assert "<tailnet-host>" in got and "<tailnet-ip>" in got


def test_files_a_new_bug_labelled_bug():
    gh = FakeGitHub(open_bugs=[{"number": 3, "body": "something else", "html_url": "u3"}])
    got = ur.report(ROW, "o/r", "tok-123", box="oscar-main", gh=gh)
    assert got == {"filed": "https://github.com/o/r/issues/8", "issue": "https://github.com/o/r/issues/8"}
    method, path, body = gh.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues") and body["labels"] == ["bug"]
    assert "?state=open&labels=bug" in gh.calls[0][1]
    assert "waits for a human yes" in body["body"]


def test_box_name_goes_only_to_private_repos(monkeypatch):
    monkeypatch.setattr(ur, "box_name", lambda: "owners-laptop")
    public = FakeGitHub(private=False)
    ur.report(ROW, "o/r", "tok-123", gh=public)
    body = public.calls[-1][2]["body"]
    assert "owners-laptop" not in body and f"Box: {ur.box_id()}" in body and ur.box_id().startswith("box-")
    assert public.calls[0][1] == "/repos/o/r"
    team = FakeGitHub(private=True)
    ur.report(ROW, "o/r", "tok-123", gh=team)
    assert "Box: owners-laptop" in team.calls[-1][2]["body"]


def test_comments_on_the_open_bug_with_the_same_failure():
    mark = ur.draft(ROW, "x")["mark"]
    gh = FakeGitHub(open_bugs=[{"number": 5, "body": "a PR", "pull_request": {}, "html_url": "pr"},
                               {"number": 7, "body": f"earlier report\n{mark}", "html_url": "https://github.com/o/r/issues/7"}])
    got = ur.report(ROW, "o/r", "tok-123", box="other-box", gh=gh)
    assert got["commented"].endswith("#issuecomment-1") and got["issue"] == "https://github.com/o/r/issues/7"
    method, path, body = gh.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues/7/comments") and "Box: other-box" in body["body"]
    assert len(gh.calls) == 2   # box given: no visibility lookup


def test_github_refusal_is_an_error_without_the_token():
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    got = ur.report(ROW, "o/r", "tok-123", box="b", gh=FakeGitHub(fail=err))
    assert got == {"error": "GitHub said 403"} and "tok" not in json.dumps(got)


def test_switched_off_reports_nothing_and_never_reads_the_login(tmp_path):
    c = recorded(tmp_path)

    def no_token():
        raise AssertionError("token read while switched off")
    gh = FakeGitHub()
    assert ur.report_all(c, [ROW], store="wizardapps", token=no_token, gh=gh, on=lambda: False) == {}
    assert gh.calls == []


def test_switched_on_reports_each_app_it_can(tmp_path):
    c = recorded(tmp_path)
    gh = FakeGitHub()
    rows = [ROW, {**ROW, "id": "no-source"}]
    got = ur.report_all(c, rows, store="wizardapps", token=lambda: "tok-123", gh=gh, on=lambda: True)
    assert got["wizard-messenger"]["filed"] and got["no-source"] == {"error": "no recorded repo for this app"}
    assert all(p.startswith("/repos/humanitylabs-org/wizard-messenger") for _, p, _ in gh.calls)
    nolog = ur.report_all(c, [ROW], store="wizardapps", token=lambda: "", gh=gh, on=lambda: True)
    assert nolog == {"wizard-messenger": {"error": "this Wizard has no GitHub login"}}


def test_owner_notice_says_the_team_was_told_only_when_it_was():
    failed = [{**ROW, "rollback": None}]
    _, before, _ = au._summary([], [], failed, [])
    assert "Ask your Wizard to look at it." in before and "team" not in before
    _, after, _ = au._summary([], [], failed, [], told={"wizard-messenger"})
    assert "The team was told, so you don't need to report it." in after and "Ask your Wizard" not in after
    _, rolled, _ = au._summary([], [ROW], [], [], told={"wizard-messenger"})
    assert "Kept the previous version of Messenger" in rolled and "The team was told about it." in rolled
    _, untold, _ = au._summary([], [ROW], [], [])
    assert "team" not in untold


def test_apply_hands_failures_to_the_reporter(tmp_path, monkeypatch):
    from wam import cli, updates
    monkeypatch.setattr(updates, "installing", lambda home: False)
    monkeypatch.setattr(au, "candidates", lambda *a, **k: [{"id": "wizard-messenger", "name": "Messenger", "from": 93,
                                                            "to": 97, "to_version": "0.9.7", "status": "running",
                                                            "skip": None}])
    monkeypatch.setattr(au, "_containers", lambda urn: ["running"])
    monkeypatch.setattr(au, "baseline", lambda *a, **k: True)
    monkeypatch.setattr(au, "reconcile_settings", lambda *a, **k: [])
    monkeypatch.setattr(cli, "app_update", lambda c, app_id: {"version": 97, "status": "running"})
    monkeypatch.setattr(au, "verify", lambda *a, **k: "the app did not answer after the update")
    monkeypatch.setattr(au, "rollback", lambda *a, **k: {"rolled_back": True})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt: None)
    sent = []
    monkeypatch.setattr(au, "_notify", lambda c, *msg: sent.append(msg))

    class C:
        home = tmp_path
        runtipi_root = tmp_path

        def runtipi(self):
            return type("Rt", (), {"installed": lambda self: []})()
    seen = []

    def reporter(c, rows, store):
        seen.append([(r["id"], r["step"]) for r in rows])
        return {"wizard-messenger": {"filed": "u", "issue": "u"}}
    out = au.apply(C(), renames=lambda c, rt: [], reporter=reporter)
    assert seen == [[("wizard-messenger", "check after update")]]
    assert out["reported"] == {"wizard-messenger": {"filed": "u", "issue": "u"}}
    assert "The team was told about it." in sent[0][1]
