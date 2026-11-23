"""#971: Runtipi 4.12 runs an update as a job. A job that fails (seen live: its backup could not copy the Agent
Service's app data while a private agent kept writing there) puts the previous files back and the previous status,
so wam saw `running`, reported nothing, and only the release check later said "does not match the release pin
(update may have rolled back)". A job that waits for one of Runtipi's three job slots also leaves the old status in
place. wam now follows the job by its requestId and reports Runtipi's own reason.
"""
from wam import cli
from wam.runtipi import RuntipiError


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _rt(jobs, *, status="running", route=True, final=None):
    calls = {"start": 0, "job_paths": [], "waits": 0}
    seq = list(jobs)

    class RT:
        def app(self, urn):
            return {"app": {"status": status, "version": "1"}}

        def update(self, urn, perform_backup=True):
            return {"requestId": "req-1"}

        def req(self, method, path, body=None, timeout=60):
            assert method == "GET" and "/job?requestId=req-1" in path
            calls["job_paths"].append(path)
            if not route:
                raise RuntipiError(f"GET {path} -> 404: b'Cannot GET'")
            return {"job": seq.pop(0) if len(seq) > 1 else seq[0]}

        def wait_status(self, urn, want, timeout=900):
            calls["waits"] += 1
            return final or status

        def start(self, urn):
            calls["start"] += 1

    return RT(), calls


def _update(tmp_path, monkeypatch, rt):
    c = cli.Config()
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "home"
    c.home.mkdir(exist_ok=True)
    monkeypatch.setattr(c, "runtipi", lambda: rt, raising=False)
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli, "backfill_owner_login", lambda *a, **k: False)
    monkeypatch.setattr(cli, "keep_trusted_hosts", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.update_backups, "prune_app", lambda *a, **k: {"deleted": [], "errors": []})
    return cli._app_update(c, "wizard-agent-service", backup=True)


def test_a_failed_update_job_is_reported_with_runtipis_reason(tmp_path, monkeypatch):
    rt, calls = _rt([{"status": "queued", "phase": "queued"},
                     {"status": "running", "phase": "copying"},
                     {"status": "failed", "phase": "finished", "message": "Failed to copy app data for backup"}])
    out = _update(tmp_path, monkeypatch, rt)
    assert out["status"] == "running" and calls["start"] == 0
    assert out["error"] == ("Runtipi did not update wizard-agent-service: Failed to copy app data for backup; "
                            "the installed version was kept")
    assert out["job"] == {"status": "failed", "message": "Failed to copy app data for backup"}
    assert len(calls["job_paths"]) == 3   # followed the job to its end, not the first `running` status


def test_a_successful_job_is_a_plain_update(tmp_path, monkeypatch):
    rt, _ = _rt([{"status": "running", "phase": "updating"}, {"status": "succeeded", "phase": "finished",
                                                              "message": "App updated successfully"}])
    out = _update(tmp_path, monkeypatch, rt)
    assert out["status"] == "running" and not out.get("error") and "job" not in out


def test_a_runtipi_without_the_job_route_keeps_the_status_wait(tmp_path, monkeypatch):
    rt, calls = _rt([{}], route=False)
    out = _update(tmp_path, monkeypatch, rt)
    assert len(calls["job_paths"]) == 1 and calls["waits"] == 1
    assert out["status"] == "running" and not out.get("error")


def test_the_reason_comes_first_when_the_app_also_did_not_come_back(tmp_path, monkeypatch):
    rt, calls = _rt([{"status": "failed", "phase": "finished", "message": "pull access denied"}], final="stopped")
    out = _update(tmp_path, monkeypatch, rt)
    assert calls["start"] == 1
    assert out["error"].startswith("Runtipi did not update wizard-agent-service: pull access denied; "
                                   "the installed version was kept; the app did not come back")


def test_update_job_waits_through_the_queue_and_gives_up_at_its_timeout():
    clock = Clock()
    rt, _ = _rt([{"status": "queued", "phase": "queued"}])
    assert cli.update_job(rt, "x:wizardapps", {"requestId": "req-1"}, 30, sleep=clock.sleep, clock=clock) is None
    assert clock.t >= 30


def test_update_job_without_a_request_id_or_a_forgotten_job_is_unknown():
    rt, calls = _rt([None])
    assert cli.update_job(rt, "x:wizardapps", None, 30) is None
    assert cli.update_job(rt, "x:wizardapps", {}, 30) is None
    assert calls["job_paths"] == []
    assert cli.update_job(rt, "x:wizardapps", {"requestId": "req-1"}, 30) is None


def test_a_missed_answer_mid_job_is_asked_again():
    clock = Clock()
    answers = [{"job": {"status": "running", "phase": "copying"}}, OSError("connection reset"),
               {"job": {"status": "failed", "phase": "finished", "message": "boom"}}]

    class RT:
        def req(self, method, path, body=None, timeout=60):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

    job = cli.update_job(RT(), "x:wizardapps", {"requestId": "req-1"}, 60, sleep=clock.sleep, clock=clock)
    assert job["status"] == "failed" and job["message"] == "boom"


def test_release_updates_names_the_reason(tmp_path, monkeypatch):
    """The installer's release step now says why the Agent Service stayed on its old source."""
    import json
    from wam import release_pins
    doc = {"default_apps": {"agent-service": {"app": "wizard-agent-service", "repo": "example/as",
                                              "commit": "b" * 40, "index_version": "0.3.0"}}}
    monkeypatch.setattr(release_pins, "load", lambda: doc)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-agent-service"})
    for kind, commit in (("apps", "a" * 40), ("repos", "b" * 40)):
        p = tmp_path / kind / cli.STORE / ("apps" if kind == "repos" else "") / "wizard-agent-service"
        p.mkdir(parents=True)
        (p / ".wizard-commit").write_text(commit + "\n")
        (p / ".wizard-source").write_text(json.dumps({"repo": "example/as"}))
        (p / "wizard.md").write_text("---\nid: wizard-agent-service\nversion: 0.3.0\n---\n")
        (p / "config.json").write_text(json.dumps({"version": "0.3.0", "form_fields": []}))
    monkeypatch.setattr(cli, "resolve_any", lambda c, app: {"id": app, "uses": []})
    monkeypatch.setattr(cli, "app_update", lambda c, app, settings=None: {
        "status": "running", "error": "Runtipi did not update wizard-agent-service: Failed to copy app data for "
                                      "backup; the installed version was kept"})
    rt = type("RT", (), {"app": lambda self, urn: {"app": {"status": "running", "config": {}}}})()
    c = type("C", (), {"runtipi_root": tmp_path, "home": tmp_path / "home"})()
    failed = {}
    assert cli.release_updates(c, rt, failed) == []
    assert "Failed to copy app data for backup" in failed["wizard-agent-service"]
