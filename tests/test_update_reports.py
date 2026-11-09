"""A failed app update tells the team (#524): file a bug in the app's repo, or comment on the open one.

Behind the off-by-default feature switch `wizard/update-bug-reports` (wam/update_reports.py)."""
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


# ---- WizardOS update failures (wam/updates.py) -------------------------------------------------------------------

def os_box(tmp_path, state="failed", receipt=None, step="pulling the shell image", reason="docker pull failed"):
    (tmp_path / "install-status.json").write_text(json.dumps({
        "state": state, "step": step, "failure_reason": reason, "exit_code": 1,
        "started_at": "2026-11-09T03:31:00Z"}))
    (tmp_path / "release-pins.json").write_text(json.dumps({"version": "0.4.2"}))
    if receipt:
        (tmp_path / "release.json").write_text(json.dumps(receipt))
    return tmp_path


def test_os_failure_reads_the_installer_run(tmp_path):
    row = ur.os_failure(os_box(tmp_path))
    assert (row["kind"], row["to"], row["step"], row["reason"]) == ("os", "0.4.2", "pulling the shell image",
                                                                     "docker pull failed")
    d = ur.draft({**row, "from": "0.4.1"}, "oscar-main")
    assert d["title"] == "WizardOS update failed: 0.4.1 to 0.4.2 (pulling the shell image)"
    assert d["key"] == "wizard-os:0.4.2:pulling the shell image"
    for want in ("A WizardOS update failed", "Box: oscar-main", "0.4.1 -> 0.4.2", "Error: docker pull failed",
                 "retry it with Update now"):
        assert want in d["body"], want


def test_os_failure_read_back_mismatch_and_healthy(tmp_path):
    receipt = {"release": "0.4.3", "status": "mismatch", "checks": [
        {"name": "shell image", "core": True, "ok": False}, {"name": "default app x", "core": False, "ok": False}]}
    row = ur.os_failure(os_box(tmp_path, state="done", receipt=receipt))
    assert (row["to"], row["step"]) == ("0.4.3", "read-back") and "shell image" in row["reason"]
    assert "default app x" not in row["reason"]
    os_box(tmp_path, state="done", receipt={"release": "0.4.3", "status": "pinned"})
    assert ur.os_failure(tmp_path) is None
    os_box(tmp_path, state="failed", receipt={"status": "unpinned"})
    assert ur.os_failure(tmp_path) is None   # a developer box updated by hand is not a release failure


def test_report_os_switched_off_does_nothing(tmp_path):
    os_box(tmp_path)

    def no_token():
        raise AssertionError("token read while switched off")
    gh = FakeGitHub()
    assert ur.report_os(tmp_path, token=no_token, gh=gh, on=lambda: False, notify=lambda *m: 1 / 0) == {}
    assert gh.calls == [] and not (tmp_path / ur.OS_STATE).exists()


def test_report_os_files_once_per_failed_run_and_tells_the_owner(tmp_path):
    (tmp_path / ur.OS_STATE).write_text(json.dumps({"good": "0.4.1"}))
    os_box(tmp_path)
    gh, sent = FakeGitHub(private=True), []
    got = ur.report_os(tmp_path, token=lambda: "tok-123", gh=gh, on=lambda: True, notify=lambda *m: sent.append(m))
    assert got["filed"] and gh.calls[0][1] == "/repos/humanitylabs-org/wizard-os"
    method, path, body = gh.calls[-1]
    assert (method, path) == ("POST", "/repos/humanitylabs-org/wizard-os/issues") and body["labels"] == ["bug"]
    assert "0.4.1 -> 0.4.2" in body["body"]
    title, msg, prio = sent[0]
    assert prio == "high" and "did not finish (step: pulling the shell image): docker pull failed" in msg
    assert "The team was told, so you don't need to report it." in msg
    # the same failed run is not reported again
    assert ur.report_os(tmp_path, token=lambda: "tok-123", gh=gh, on=lambda: True, notify=sent.append) == {}
    assert len(sent) == 1 and len(gh.calls) == 3
    # the next failed run of the same release comments on the open bug
    mark = ur.draft({**ur.os_failure(tmp_path)}, "x")["mark"]
    (tmp_path / "install-status.json").write_text(json.dumps({
        "state": "failed", "step": "pulling the shell image", "failure_reason": "docker pull failed",
        "started_at": "2026-11-10T03:31:00Z"}))
    gh2 = FakeGitHub(open_bugs=[{"number": 7, "body": mark, "html_url": "https://github.com/o/r/issues/7"}],
                     private=True)
    got = ur.report_os(tmp_path, token=lambda: "tok-123", gh=gh2, on=lambda: True, notify=lambda *m: sent.append(m))
    assert got["commented"] and gh2.calls[-1][1].endswith("/issues/7/comments") and len(sent) == 2


def test_report_os_remembers_the_last_clean_release(tmp_path):
    (tmp_path / "release.json").write_text(json.dumps({"release": "0.4.1", "status": "pinned"}))
    gh = FakeGitHub()
    assert ur.report_os(tmp_path, token=lambda: "tok-123", gh=gh, on=lambda: True) == {}
    assert json.loads((tmp_path / ur.OS_STATE).read_text())["good"] == "0.4.1" and gh.calls == []


def test_report_os_without_login_or_on_github_refusal_tells_nobody(tmp_path):
    os_box(tmp_path)
    sent = []
    got = ur.report_os(tmp_path, token=lambda: "", gh=FakeGitHub(), on=lambda: True, notify=sent.append)
    assert got == {"error": "this Wizard has no GitHub login"}
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    got = ur.report_os(tmp_path, token=lambda: "tok-123", gh=FakeGitHub(fail=err), on=lambda: True,
                       notify=sent.append)
    assert got == {"error": "GitHub said 403"} and sent == []
    assert "reported" not in json.loads((tmp_path / ur.OS_STATE).read_text() if (tmp_path / ur.OS_STATE).exists()
                                        else "{}")


def test_automatic_check_reports_before_retrying(tmp_path, monkeypatch):
    from wam import updates
    calls = []
    monkeypatch.setattr(updates, "mode", lambda h=None: "auto")
    monkeypatch.setattr(ur, "report_os", lambda home=None: calls.append(("report", home)) or {})
    monkeypatch.setattr(updates, "status", lambda *a, **k: calls.append(("status",)) or (_ for _ in ()).throw(
        RuntimeError("stop here")))
    try:
        updates.apply_if_auto(tmp_path)
    except RuntimeError:
        pass
    assert calls == [("report", tmp_path), ("status",)]


def test_report_failure_never_raises(tmp_path, monkeypatch):
    from wam import updates
    monkeypatch.setattr(ur, "report_os", lambda home=None: 1 / 0)
    assert updates.report_failure(tmp_path) == {}


def test_ask_mode_nightly_run_still_reports(tmp_path, monkeypatch):
    from wam import maintain, updates
    seen = []
    monkeypatch.setattr(updates, "mode", lambda h=None: "ask")
    monkeypatch.setattr(updates, "report_failure", lambda home=None: seen.append(home) or {})
    assert maintain.updates_step(tmp_path, True)["state"] == "ask" and seen == [tmp_path]
