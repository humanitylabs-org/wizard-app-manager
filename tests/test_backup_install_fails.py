"""wizard-app-manager#890: a blank-host restore stopped at the first agent app Runtipi could not install
('installing agent-philosopher did not finish (status missing)'), after waiting 30 minutes for it.

Runtipi creates an app's row when an install starts and deletes it when the install job fails, so `missing` after
the install call means it failed. The restore now reports that one app (with Runtipi's own error line when its log
has one), goes on with everything else, and still imports the agent's home, so no chat or memory is lost.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backup_fakes import FakeRuntipi, _w  # noqa: E402
from test_backup_flow import _cli, isolated  # noqa: E402,F401
from test_backup_realbox import SVC, realbox  # noqa: E402,F401
from wam.backup import capture, restore  # noqa: E402
from wam.backup.box import box_digest  # noqa: E402

AGENT = "agent-research:wizardapps"


def _fail_install(monkeypatch, urn, *, seen=False, leftovers=False):
    """Runtipi takes the install, then its job fails and the row goes (seen=True: one `installing` poll first;
    leftovers=True: the job got far enough to copy the package, write app-data and create a container)."""
    real = FakeRuntipi.install
    polls = {"n": 0}

    def install(self, u, form=None):
        if u != urn:
            return real(self, u, form)
        self.calls.append(("install", u))
        if leftovers:
            app_id, store = u.split(":")
            _w(self.root / "apps" / store / app_id / "config.json", "{}")
            _w(self.root / "app-data" / store / app_id / "app.env", "APP_ID=" + app_id + "\n")
            st = json.loads(self.docker_state.read_text())
            st[f"{app_id}_{store}"] = {"id": "c-left", "name": f"{app_id}_{store}-engine-1", "image": "x",
                                       "home": "/nowhere", "orphan": True}
            self.docker_state.write_text(json.dumps(st))
        if seen:
            self.apps[u] = {"status": "installing", "version": 1, "config": {}}
        return {}

    real_app = FakeRuntipi.app

    def app(self, u):
        if u == urn and u in self.apps and self.apps[u]["status"] == "installing":
            polls["n"] += 1
            if polls["n"] > 1:
                self.apps.pop(u)       # the job failed: Runtipi deleted the row
        return real_app(self, u)

    monkeypatch.setattr(FakeRuntipi, "install", install)
    monkeypatch.setattr(FakeRuntipi, "app", app)
    return polls


def _export(realbox):
    out = realbox["tmp"] / "b.wizardbackup"
    capture.export(realbox["old"].box(), out)
    return out


def test_an_agent_app_that_cannot_install_is_reported_and_the_rest_is_restored(realbox, monkeypatch):
    new = realbox["new"]
    out = _export(realbox)
    _fail_install(monkeypatch, AGENT)
    _w(new.runtipi / "logs" / "app.log", "2026-11-20 - ERROR > Error running docker compose for agent-research: "
                                         "pull access denied for ghcr.io/example/hermes-agent\n")
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 3, rec                                       # partly restored, not "stopped part way"
    assert rec["partial"] and not rec["ok"]
    [miss] = rec["not_installed"]
    assert miss["id"] == "agent-research" and miss["agent"]
    assert "installing agent-research did not finish (status missing)" in miss["message"]
    assert "pull access denied" in miss["message"]              # Runtipi's own reason travels with it
    assert any("its agent's home is restored" in x for x in rec["not_restored"])
    # the agent's home still came back through hermes import (chats, memory, paused jobs)
    rhome = new.runtipi / SVC / "wizard-os/agents/research/home"
    assert (rhome / "state.db").is_file()
    assert [j["state"] for j in json.loads((rhome / "cron/jobs.json").read_text())["jobs"]] == ["paused"]
    assert rec["agents"]["research"]["app_not_installed"] == miss["message"]
    # everything else is restored and started
    nd = new.runtipi / "app-data/wizardapps/notes-app/data"
    assert (nd / "notes.db").read_text() == "old notes database"
    assert rec["apps"]["notes-app"] == "running" and rec["apps"]["agent-research"] == "not installed"
    assert ("start", AGENT) not in new.rt.calls
    assert any("agent research" in n and "not installed" in n for n in rec["notes"])
    # one install try, not one more from the agent import
    assert [c for c in new.rt.calls if c == ("install", AGENT)] == [("install", AGENT)]


def test_a_failed_install_is_noticed_as_soon_as_runtipi_drops_the_app(realbox, monkeypatch):
    new = realbox["new"]
    out = _export(realbox)
    polls = _fail_install(monkeypatch, AGENT, seen=True)
    sleeps = []
    box = new.box()
    box.sleep = sleeps.append
    code, rec = _cli(box, "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 3 and rec["not_installed"][0]["id"] == "agent-research", rec
    assert polls["n"] == 2
    assert len(sleeps) < restore.INSTALL_MISSING_POLLS          # the old wait sat on `missing` for 30 minutes


def test_undo_after_a_partial_restore_puts_the_box_back(realbox, monkeypatch):
    new = realbox["new"]
    out = _export(realbox)
    before = box_digest(new.box())
    _fail_install(monkeypatch, AGENT)
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 3, rec
    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"], res
    assert box_digest(new.box()) == before


def test_agent_apps_install_after_the_other_apps(realbox, monkeypatch):
    """An agent's compose can use what another app brings (the Claude app's shared login volume)."""
    new = realbox["new"]
    out = _export(realbox)
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 0, rec
    order = [c[1] for c in new.rt.calls if c[0] == "install"]
    assert order.index(AGENT) > order.index("notes-app:wizardapps"), order


def test_a_runtipi_api_hiccup_is_not_read_as_a_failed_install(tmp_path):
    from wam.runtipi import RuntipiError

    class Rt:
        def __init__(self):
            self.n = 0

        def app(self, urn):
            self.n += 1
            if self.n < 4:
                raise RuntipiError("GET /apps/x -> 502: bad gateway")
            return {"app": {"status": "running"}}

    class B:
        rt = Rt()

        def sleep(self, s):
            pass

    assert restore._wait_installed(B(), "x:wizardapps") == "running"


def test_a_failed_install_leaves_no_containers_or_folders_behind(realbox, monkeypatch):
    """Codex P2 on PR #901: Runtipi's failed job only drops its row; the restore removes the rest."""
    new = realbox["new"]
    out = _export(realbox)
    _fail_install(monkeypatch, AGENT, leftovers=True)
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 3 and rec["not_installed"][0]["id"] == "agent-research", rec
    assert "agent-research_wizardapps" not in json.loads(new.docker_state.read_text())
    assert not (new.runtipi / "apps/wizardapps/agent-research").exists()
    assert not (new.runtipi / "app-data/wizardapps/agent-research").exists()
    # the agent's home (in the Agent Service's data) is untouched and imported
    assert (new.runtipi / SVC / "wizard-os/agents/research/home/state.db").is_file()


def test_an_agent_whose_hermes_cannot_run_keeps_its_archive_and_the_restore_finishes(realbox, monkeypatch):
    """Codex P1 on PR #901: a failed pull means the agent's Hermes image is not here either, so `hermes import`
    in its container fails too. The restore keeps the checked archive and goes on instead of stopping part way."""
    from wam.backup.box import HermesError, HermesRunner

    new = realbox["new"]
    out = _export(realbox)
    _fail_install(monkeypatch, AGENT)
    real = HermesRunner.import_zip

    def import_zip(self, agent, zip_path):
        if agent.urn == AGENT:
            raise HermesError("docker run: pull access denied for ghcr.io/example/hermes-agent")
        return real(self, agent, zip_path)

    monkeypatch.setattr(HermesRunner, "import_zip", import_zip)
    code, rec = _cli(new.box(), "import", str(out), "--yes", "--key-file", str(realbox["keyfile"]))
    assert code == 3, rec
    [kept] = rec["not_imported"]
    assert kept["agent"] == "research" and "pull access denied" in kept["message"]
    assert Path(kept["archive"]).is_file()                       # nothing of the agent is lost
    assert rec["agents"]["research"]["archive_kept"] == kept["archive"]
    assert any("kept, not imported" in x for x in rec["not_restored"])
    nd = new.runtipi / "app-data/wizardapps/notes-app/data"      # the rest is restored and started
    assert (nd / "notes.db").read_text() == "old notes database"
    assert rec["apps"]["notes-app"] == "running"
    code, res = _cli(new.box(), "undo")
    assert code == 0 and res["digest_match"], res
