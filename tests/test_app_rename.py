"""App renames (wam/app_rename.py): id rewriting, rename discovery, the naming lint and the migrate/rollback flow."""
import json
from pathlib import Path

import pytest

from wam import app_rename as ar
from wam import app_standard


# ---------------------------------------------------------------- naming rule

@pytest.mark.parametrize("app_id,name,ok", [
    ("movies", "Movies", True), ("connected-accounts", "Connected accounts", True),
    ("wizard-calendar", "Calendar", True), ("wizard-files", "Wizard Files", True),
    ("cinequeue", "Movies", False), ("usuals", "Food", False), ("wizard-connectors", "Connected accounts", False),
])
def test_id_matches_name(app_id, name, ok):
    assert ar.id_matches_name(app_id, name) is ok


def _rule(doc):
    return {r["rule"]: r for r in app_standard.id_rows(doc)}


def test_new_app_with_made_up_id_is_refused():
    r = _rule({"id": "popcornbox", "name": "Movies"})["app id is the app's name"]
    assert r["ok"] is False and r["severity"] == "error" and "'movies'" in r["detail"]


@pytest.mark.parametrize("app_id,name", [
    ("movies", "Movies"), ("wizard-calendar", "Calendar"), ("movies-next", "Movies (test)"),
    ("search-preview", "Search (Oscar preview)"), ("agent-philosopher", "Philosopher (agent)"),
    ("connected-accounts", "Connected accounts"),
])
def test_names_and_staging_labels_pass(app_id, name):
    assert _rule({"id": app_id, "name": name})["app id is the app's name"]["ok"]


def test_legacy_ids_warn_but_do_not_fail():
    r = _rule({"id": "wizard-ux", "name": "Wizard Shell"})["app id is the app's name"]
    assert r["ok"] is True and r["severity"] == "warning"


@pytest.mark.parametrize('app_id', ['cinequeue', 'usuals', 'outline-streaming-demo', 'wizard-connectors', 'voice-outline'])
def test_migrated_app_exceptions_cannot_return(app_id):
    assert app_id not in app_standard.LEGACY_IDS


def test_renamed_from_must_list_other_ids():
    assert _rule({"id": "movies", "name": "Movies", "renamed_from": ["cinequeue"]})["renamed_from lists earlier app ids"]["ok"]
    assert not _rule({"id": "movies", "name": "Movies", "renamed_from": ["movies"]})["renamed_from lists earlier app ids"]["ok"]
    assert not _rule({"id": "movies", "name": "Movies", "renamed_from": ["Bad Id"]})["renamed_from lists earlier app ids"]["ok"]


def test_renamed_from_parsing():
    assert ar.renamed_from({"id": "movies", "renamed_from": "cinequeue"}) == ["cinequeue"]
    assert ar.renamed_from({"id": "movies", "renamed_from": ["cinequeue", "cinequeue", "movies", "Bad", 3]}) == ["cinequeue"]
    assert ar.renamed_from({"id": "movies"}) == []


# ---------------------------------------------------------------- id rewriting

def test_grants_carry_both_directions_and_bookkeeping():
    doc = {"grants": [{"kind": "app", "from": "wizard-calendar", "to": "wizard-connectors"},
                      {"kind": "app", "from": "wizard-connectors", "to": "slot:ai-connection"},
                      {"kind": "app", "from": "wisdom", "to": "wizard-claude"}],
           "denied": [{"from": "wizard-connectors", "to": "notes"}], "pending": [],
           "notices": [{"from": "wizard-connectors", "to": "x", "text": "t"}],
           "sensitive": ["wizard-connectors", "connected-accounts"], "seeded": ["wizard-connectors"],
           "layer2": {"managed": {"wizard-calendar": ["wizard-connectors"], "wizard-connectors": ["notes"]},
                      "adopted": ["wizard-connectors", "connected-accounts"],
                      "endpoints": {"wizard-calendar": {"wizard-connectors": "http://x"}}}}
    out = ar.rename_in_grants(doc, "wizard-connectors", "connected-accounts")
    assert out["grants"][0]["to"] == "connected-accounts" and out["grants"][1]["from"] == "connected-accounts"
    assert out["grants"][1]["to"] == "slot:ai-connection" and out["grants"][2] == doc["grants"][2]
    assert out["denied"][0]["from"] == "connected-accounts" and out["notices"][0]["from"] == "connected-accounts"
    assert out["sensitive"] == ["connected-accounts"] and out["seeded"] == ["connected-accounts"]
    assert out["layer2"]["managed"] == {"wizard-calendar": ["connected-accounts"], "connected-accounts": ["notes"]}
    assert out["layer2"]["adopted"] == ["connected-accounts"]
    assert out["layer2"]["endpoints"] == {"wizard-calendar": {"connected-accounts": "http://x"}}
    assert doc["grants"][0]["to"] == "wizard-connectors"   # input untouched


def test_routes_move_the_apps_own_path_and_carry_staging_label():
    doc = {"cinequeue": {"view": "/cinequeue-next/", "staged_as": "cinequeue-next"}, "notes": {"view": "/notes/"}}
    out = ar.rename_in_routes(doc, "cinequeue", "movies")
    assert out == {"movies": {"view": "/movies-next/", "staged_as": "movies-next"}, "notes": {"view": "/notes/"}}


def test_router_routes_and_services():
    doc = {"routes": {"usuals/suggest": {"app": "usuals", "feature": "suggest", "provider": "wizard-claude"},
                      "wisdom/ask": {"app": "wisdom", "provider": "wizard-claude"}},
           "services": {"usuals/stt": "wizard-tinfoil", "wizard-recorder/live": "usuals"}}
    out = ar.rename_in_router(doc, "usuals", "food")
    assert set(out["routes"]) == {"food/suggest", "wisdom/ask"} and out["routes"]["food/suggest"]["app"] == "food"
    assert out["services"] == {"food/stt": "wizard-tinfoil", "wizard-recorder/live": "food"}


def test_update_holds_follow_the_app():
    out = ar.rename_in_updates_state({"holds": ["cinequeue", "notes"], "failed": {"cinequeue": {"to": 9}}, "mode": "auto"},
                                     "cinequeue", "movies")
    assert out == {"holds": ["movies", "notes"], "failed": {}, "mode": "auto"}


def test_compose_override_renames_service_and_wam_record_only():
    text = ("# owner note\nservices:\n  cinequeue:\n    image: x\n    network_mode: host\n    networks: !reset []\n"
            "  cinequeue-worker:\n    image: y\n  ingress:\n    networks:\n      main:\n        aliases: [cinequeue-calendar]\n"
            "x-wizard-networks:\n  services: [cinequeue, ingress]\n  networks: [wizard-link-cinequeue-notes]\n")
    out = ar.rename_compose_override(text, "cinequeue", "movies", {"movies", "ingress"})
    assert "\n  movies:\n    image: x" in out and "  cinequeue-worker:" in out and "[cinequeue-calendar]" in out
    assert "services: [movies, ingress]" in out and "wizard-link-cinequeue-notes" in out
    assert "!reset []" in out and out.startswith("# owner note")
    # a recipe that kept its service name keeps the override as it is
    assert ar.rename_compose_override(text, "cinequeue", "movies", {"cinequeue"}) == text


# ---------------------------------------------------------------- discovery

class FakeRT:
    def __init__(self, apps):
        self.apps = apps   # id -> {"status", "port", "config", "version"}
        self.calls = []

    def installed(self):
        return [{"app": {"appName": k, "appStoreSlug": "wizardapps", **v}} for k, v in self.apps.items()]

    def app(self, urn):
        name = urn.split(":")[0]
        a = self.apps.get(name)
        return {"app": {"appName": name, **a} if a else None, "info": {"name": name.title()}}

    def req(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        urn = path.split("/")[2]
        self.apps[urn.split(":")[0]]["port"] = body.get("port")
        self.apps[urn.split(":")[0]]["config"] = body
        return {}

    def start(self, urn):
        self.calls.append(("start", urn))
        self.apps[urn.split(":")[0]]["status"] = "running"

    def uninstall(self, urn):
        self.calls.append(("uninstall", urn))
        self.apps.pop(urn.split(":")[0], None)

    def wait_status(self, urn, want, timeout=900):
        a = self.apps.get(urn.split(":")[0])
        return a["status"] if a else "missing"


def _box(tmp_path, apps):
    root = tmp_path / "runtipi"
    for app_id, src in apps.items():
        d = root / "apps" / "wizardapps" / app_id
        d.mkdir(parents=True)
        (d / ".wizard-source").write_text(json.dumps(src))
    home = tmp_path / "wos"
    home.mkdir()

    class C:
        runtipi_root = root
        index = "https://index.invalid"
        private_apps = {}
    C.home = home
    C.secrets = home / "secrets"
    return C()


def test_due_finds_index_renames_only(tmp_path):
    c = _box(tmp_path, {"cinequeue": {"repo": "humanitylabs-org/cinequeue", "index": "x"},
                        "notes": {"repo": "humanitylabs-org/notes"},
                        "usuals": {"repo": "humanitylabs-org/wizard-food-app", "private": True}})
    rt = FakeRT({"cinequeue": {"status": "running"}, "notes": {"status": "running"}, "usuals": {"status": "running"}})
    recs = {"humanitylabs-org/cinequeue": {"id": "movies", "name": "Movies", "repo": "humanitylabs-org/movies",
                                           "manifest": {"id": "movies", "renamed_from": ["cinequeue"]}},
            "humanitylabs-org/notes": {"id": "notes", "manifest": {"id": "notes"}}}
    got = ar.due(c, rt, fetch=lambda repo: recs[repo])
    assert got == [{"from": "cinequeue", "to": "movies", "repo": "humanitylabs-org/movies", "name": "Movies"}]
    # a new id that does not name the old one is never treated as a rename
    recs["humanitylabs-org/cinequeue"]["manifest"]["renamed_from"] = []
    assert ar.due(c, rt, fetch=lambda repo: recs[repo]) == []


# ---------------------------------------------------------------- migrate

class FakeFS:
    """The runtipi container's /data, in memory: path -> bytes (files) and a set of dirs."""

    def __init__(self, files):
        self.files = dict(files)

    def read(self, p):
        return self.files.get(p)

    def write(self, p, data, mode=None):
        self.files[p] = data

    def exists(self, p):
        return p in self.files or any(k.startswith(p + "/") for k in self.files)

    def nonempty(self, p):
        return any(k.startswith(p + "/") for k in self.files)

    def copy(self, src, dst):
        assert not self.exists(dst)
        for k in [k for k in self.files if k.startswith(src + "/")]:
            self.files[dst + k[len(src):]] = self.files[k]

    def move(self, src, dst):
        self.copy(src, dst)
        for k in [k for k in self.files if k.startswith(src + "/")]:
            del self.files[k]

    def summary(self, p):
        ks = [k for k in self.files if k.startswith(p + "/")]
        return {"files": len(ks), "bytes": sum(len(self.files[k]) for k in ks)}


@pytest.fixture
def box(tmp_path, monkeypatch):
    from wam import cli, network, app_updates
    c = _box(tmp_path, {"cinequeue": {"repo": "humanitylabs-org/cinequeue", "index": "x"}})
    (c.home / "routes.json").write_text(json.dumps({"cinequeue": {"view": "/cinequeue-next/", "staged_as": "cinequeue-next"}}))
    recipe = c.runtipi_root / "repos" / "wizardapps" / "apps" / "movies"
    recipe.mkdir(parents=True)
    (recipe / "wizard.md").write_text("---\nid: movies\nname: Movies\nrenamed_from: [cinequeue]\n---\n")
    (recipe / "docker-compose.yml").write_text("services:\n  movies:\n    image: x\n")
    fs = FakeFS({
        "/app-data/wizardapps/cinequeue/data/movies.json": b'{"movies": [1, 2, 3]}',
        "/app-data/wizardapps/cinequeue/wizard/grants.json": b"{}",
        "/data/user-config/wizardapps/cinequeue/docker-compose.yml": b"services:\n  cinequeue:\n    network_mode: host\n",
        "/data/state/wizard-grants.json": json.dumps({"grants": [{"from": "cinequeue", "to": "notes"}], "denied": []}).encode(),
    })
    for n, f in (("rt_read", fs.read), ("rt_write", fs.write), ("rt_exists", fs.exists),
                 ("rt_nonempty_dir", fs.nonempty), ("rt_copy_tree", fs.copy), ("rt_move", fs.move),
                 ("rt_tree_summary", fs.summary)):
        monkeypatch.setattr(ar, n, f)
    monkeypatch.setattr(ar, "_sh", lambda *a, **k: None)
    monkeypatch.setattr(ar, "app_data_dir", lambda: "/app-data")   # Runtipi 4 mounts app-data there (#869)
    rt = FakeRT({"cinequeue": {"status": "running", "port": 9234, "version": 8,
                               "config": {"CINEQUEUE_WATCH_REGION": "TH", "openPort": False}}})
    c.runtipi = lambda: rt
    installs = []

    def install(c_, app_id, given, _record=None, _form_base=None, **k):
        installs.append((app_id, given, _form_base))
        assert fs.exists("/app-data/wizardapps/movies/data/movies.json")   # data is there before first start
        rt.apps[app_id] = {"status": "running", "port": _form_base.get("port"), "config": _form_base}
        return {"status": "running"}

    monkeypatch.setattr(cli, "installed_ids", lambda rt_: set(rt_.apps))
    monkeypatch.setattr(cli, "resolve_any", lambda c_, ref: {"id": ref, "repo": "humanitylabs-org/movies"})
    monkeypatch.setattr(cli, "install", install)
    monkeypatch.setattr(cli, "free_port", lambda start, **k: 10234)
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli, "write_registry", lambda *a, **k: {"apps": []})
    monkeypatch.setattr(cli, "app_lifecycle", lambda c_, verb, app: (rt.apps[app].update(
        status="stopped" if verb == "stop" else "running"), {"status": rt.apps[app]["status"]})[1])
    monkeypatch.setattr(network, "host_access_allowed", lambda home: set())
    monkeypatch.setattr(app_updates, "_containers", lambda urn: ["running"])
    monkeypatch.setattr(app_updates, "_endpoint", lambda c_, rt_, app: ("http://127.0.0.1:9234/mcp", True))
    monkeypatch.setattr(app_updates, "_probe", lambda url, mcp, t: True)
    return c, rt, fs, installs


def test_migrate_moves_everything_then_retires_the_old_app(box):
    c, rt, fs, installs = box
    c.secrets.mkdir()
    (c.secrets / "cinequeue.json").write_text('{"PW": "x"}')
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("renamed") is True, res
    assert (c.secrets / "movies.json").read_text() == '{"PW": "x"}' and (c.secrets / "cinequeue.json").is_file()
    # same port, same saved settings
    assert installs[0][2]["port"] == 9234 and installs[0][2]["CINEQUEUE_WATCH_REGION"] == "TH"
    assert installs[0][1] == {"CINEQUEUE_WATCH_REGION": "TH"}
    assert rt.apps["movies"]["status"] == "running" and "cinequeue" not in rt.apps
    assert fs.read("/app-data/wizardapps/movies/data/movies.json") == b'{"movies": [1, 2, 3]}'
    # the old data is kept, renamed, never deleted
    assert res["old_data"].startswith("/app-data/wizardapps/cinequeue.old-")
    assert fs.read(res["old_data"] + "/data/movies.json") == b'{"movies": [1, 2, 3]}'
    assert json.loads(fs.read("/data/state/wizard-grants.json"))["grants"][0]["from"] == "movies"
    assert b"\n  movies:\n" in fs.read("/data/user-config/wizardapps/movies/docker-compose.yml")
    assert json.loads((c.home / "routes.json").read_text()) == {"movies": {"view": "/movies-next/", "staged_as": "movies-next"}}
    assert (Path(res["backup"]) / "grants.bak").is_file() and (Path(res["backup"]) / "routes.bak").is_file()


def test_migrate_rolls_back_when_the_new_app_does_not_answer(box, monkeypatch):
    from wam import app_updates
    c, rt, fs, installs = box
    monkeypatch.setattr(app_updates, "_probe", lambda url, mcp, t: False)
    c.secrets.mkdir()
    (c.secrets / "cinequeue.json").write_text("{}")
    grants_before = fs.read("/data/state/wizard-grants.json")
    routes_before = (c.home / "routes.json").read_text()
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("rolled_back") is True and "does not answer" in res["error"], res
    assert "movies" not in rt.apps and rt.apps["cinequeue"]["status"] == "running" and rt.apps["cinequeue"]["port"] == 9234
    assert fs.read("/data/state/wizard-grants.json") == grants_before
    assert (c.home / "routes.json").read_text() == routes_before
    assert fs.read("/app-data/wizardapps/cinequeue/data/movies.json") == b'{"movies": [1, 2, 3]}'
    assert not (c.secrets / "movies.json").exists() and (c.secrets / "cinequeue.json").is_file()


def test_migrate_refuses_without_renamed_from(box):
    c, rt, fs, installs = box
    (c.runtipi_root / "repos/wizardapps/apps/movies/wizard.md").write_text("---\nid: movies\nname: Movies\n---\n")
    res = ar.migrate(c, "cinequeue", "movies")
    assert "renamed_from" in res["error"] and not installs and rt.apps["cinequeue"]["status"] == "running"


def test_migrate_refuses_when_new_id_has_data(box):
    c, rt, fs, installs = box
    fs.write("/app-data/wizardapps/movies/data/x", b"1")
    res = ar.migrate(c, "cinequeue", "movies")
    assert "already has data" in res["error"] and not installs


def test_dry_run_changes_nothing(box):
    c, rt, fs, installs = box
    snap = dict(fs.files)
    res = ar.migrate(c, "cinequeue", "movies", dry_run=True)
    assert res["dry_run"] and res["port"] == 9234 and fs.files == snap and not installs and not rt.calls


def test_new_record_prefers_the_renamed_repo_with_the_same_commit(tmp_path, monkeypatch):
    from wam import index
    c = _box(tmp_path, {"cinequeue": {"repo": "humanitylabs-org/cinequeue", "index": "x"}})
    recs = {"humanitylabs-org/cinequeue": {"id": "movies", "repo": "humanitylabs-org/cinequeue", "install": {"commit": "a"}},
            "humanitylabs-org/movies": {"id": "movies", "repo": "humanitylabs-org/movies", "install": {"commit": "a"}},
            "someone/movies": {"id": "movies", "repo": "someone/movies", "install": {"commit": "b"}}}
    monkeypatch.setattr(index, "resolve", lambda idx, ref: recs[ref])
    monkeypatch.setattr(index, "search", lambda idx, id: [{"repo": "someone/movies"}, {"repo": "humanitylabs-org/cinequeue"},
                                                           {"repo": "humanitylabs-org/movies"}])
    assert ar._new_record(c, "cinequeue", "movies")["repo"] == "humanitylabs-org/movies"
    del recs["humanitylabs-org/movies"]
    monkeypatch.setattr(index, "search", lambda idx, id: [{"repo": "someone/movies"}])
    assert ar._new_record(c, "cinequeue", "movies")["repo"] == "humanitylabs-org/cinequeue"   # never a namesake


def test_new_record_from_an_explicit_repo(tmp_path, monkeypatch):
    """An app that moved from a folder of a bigger repo to its own repo: the new release comes from that repo."""
    from wam import index, private
    c = _box(tmp_path, {"outline-streaming-demo": {"repo": "humanitylabs-org/wizard-ux", "private": True}})
    monkeypatch.setattr(index, "resolve", lambda idx, ref: (_ for _ in ()).throw(index.IndexError_("not listed")))
    seen = []
    monkeypatch.setattr(private, "prepare", lambda root, store, repo, expected_id=None, **k:
                        seen.append((repo, expected_id)) or {"id": expected_id, "repo": repo, "private": True})
    rec = ar._new_record(c, "outline-streaming-demo", "outline", "humanitylabs-org/outline")
    assert rec["repo"] == "humanitylabs-org/outline" and seen == [("humanitylabs-org/outline", "outline")]


def test_migrate_refuses_a_bad_repo(box):
    c, rt, fs, installs = box
    assert "bad repo" in ar.migrate(c, "cinequeue", "movies", repo="../x")["error"] and not installs


def test_owner_ui_state_follows_the_rename():
    doc = {"desks": [{"wins": [{"appId": "usuals", "url": "/usuals/"}, {"appId": "other"}]}],
           "in_development": ["usuals", "food", "photos"], "name": "usuals"}
    out = ar.rename_in_owner_ui(doc, "usuals", "food")
    assert out["desks"][0]["wins"][0] == {"appId": "food", "url": "/usuals/"} and out["desks"][0]["wins"][1] == {"appId": "other"}
    assert out["in_development"] == ["food", "photos"] and out["name"] == "usuals"


# ---------------------------------------------------------------- agent access and the nightly run's limits (Oscar review)

def _agents(c, main=None, seen=None, agents=None, bindings=None):
    root = c.home / "agents"
    root.mkdir(exist_ok=True)
    if main is not None:
        (root / ".main-agent.json").write_text(json.dumps(main))
    if seen is not None:
        (root / ".apps-seen.json").write_text(json.dumps(seen))
    for aid, perms in (agents or {}).items():
        (root / aid).mkdir()
        (root / aid / "agent.json").write_text(json.dumps({"schema": 1, "id": aid, "permissions": perms}))
    if bindings is not None:
        (c.home / "app-bindings.json").write_text(json.dumps(bindings))
    return root


def test_migrate_keeps_each_agents_access_exactly(box):
    """The owner unticked the app for the main agent and ticked it for one helper. The new id must neither be ticked
    for the main agent (the Agent Service ticks an unseen app for it) nor lost by the helper."""
    c, rt, fs, installs = box
    root = _agents(c, main={"schema": 1, "wizard_apps": ["notes"], "agent_apps": []},
                   seen={"schema": 1, "apps": ["cinequeue", "notes"], "private": []},
                   agents={"helper": {"wizard_apps": ["cinequeue", "notes"], "agent_apps": []},
                           "other": {"wizard_apps": ["notes"], "agent_apps": []}},
                   bindings={"schema": 1, "apps": {"cinequeue": {"agent": "helper", "set_at": 1}}})
    (c.home / "grants").mkdir()
    (c.home / "grants" / "agent-service.json").write_text(json.dumps({"outgoing": {"cinequeue": "k1", "notes": "k2"}}))
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("renamed") is True, res
    assert json.loads((root / "helper" / "agent.json").read_text())["permissions"]["wizard_apps"] == ["movies", "notes"]
    assert json.loads((root / "other" / "agent.json").read_text())["permissions"]["wizard_apps"] == ["notes"]
    assert json.loads((root / ".main-agent.json").read_text())["wizard_apps"] == ["notes"]
    assert "movies" in json.loads((root / ".apps-seen.json").read_text())["apps"]
    assert json.loads((c.home / "app-bindings.json").read_text())["apps"] == {"movies": {"agent": "helper", "set_at": 1}}
    assert json.loads((c.home / "grants" / "agent-service.json").read_text())["outgoing"] == {"movies": "k1", "notes": "k2"}


def test_rollback_restores_agent_access(box, monkeypatch):
    from wam import app_updates
    c, rt, fs, installs = box
    monkeypatch.setattr(app_updates, "_probe", lambda url, mcp, t: False)
    root = _agents(c, main={"schema": 1, "wizard_apps": ["cinequeue"], "agent_apps": []},
                   seen={"schema": 1, "apps": ["cinequeue"], "private": []},
                   agents={"helper": {"wizard_apps": ["cinequeue"], "agent_apps": []}})
    before = {f: (root / f).read_text() for f in (".main-agent.json", ".apps-seen.json", "helper/agent.json")}
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("rolled_back") is True, res
    assert {f: (root / f).read_text() for f in before} == before


def test_nightly_leaves_held_system_and_pinned_apps_alone(tmp_path, monkeypatch):
    from wam import app_updates
    c = _box(tmp_path, {"cinequeue": {"repo": "humanitylabs-org/cinequeue"},
                        "wizard-ux": {"repo": "humanitylabs-org/wizard-ux"}})
    rt = FakeRT({"cinequeue": {"status": "running"}, "wizard-ux": {"status": "running"}})
    recs = {"humanitylabs-org/cinequeue": {"id": "movies", "manifest": {"id": "movies", "renamed_from": ["cinequeue"]}},
            "humanitylabs-org/wizard-ux": {"id": "wizard-shell-next",
                                           "manifest": {"id": "wizard-shell-next", "renamed_from": ["wizard-ux"]}}}
    fetch = lambda repo: recs[repo]  # noqa: E731
    monkeypatch.setattr(app_updates, "_release_managed", lambda: set())
    # on request (wam app-rename): both are offered
    assert {d["from"] for d in ar.due(c, rt, fetch=fetch)} == {"cinequeue", "wizard-ux"}
    # nightly: a system app moves with the WizardOS release, never on its own
    assert [d["from"] for d in ar.due(c, rt, fetch=fetch, nightly=True)] == ["cinequeue"]
    # an app the owner holds is not moved (a rename installs the new id's current release)
    app_updates.hold("cinequeue", True, home=c.home)
    assert ar.due(c, rt, fetch=fetch, nightly=True) == []
    (c.home / "app-updates.json").write_text(json.dumps({"holds": []}))
    monkeypatch.setattr(app_updates, "_selection_pinned", lambda root, store, app: app == "cinequeue")
    assert ar.due(c, rt, fetch=fetch, nightly=True) == []

def test_gateway_view_follows_the_apps_own_address_only():
    doc = {"usuals": {"view": "/usuals/"}, "outline-streaming-demo": {"view": "/voice-outline-streaming/"},
           "cinequeue": {"view": "/cinequeue-next/", "staged_as": "cinequeue-next"}, "x": {"view": "/mine/"}}
    assert ar.rename_in_routes(doc, "usuals", "food", "/usuals/", "/food/")["food"] == {"view": "/food/"}
    assert ar.rename_in_routes(doc, "outline-streaming-demo", "outline", "/voice-outline-streaming/", "/outline/")["outline"] == {"view": "/outline/"}
    assert ar.rename_in_routes(doc, "cinequeue", "movies", "/cinequeue/", "/movies/")["movies"] == {"view": "/movies-next/", "staged_as": "movies-next"}
    assert ar.rename_in_routes(doc, "x", "y", "/x/", "/y/")["y"] == {"view": "/mine/"}


def test_uses_of_an_old_id_follow_the_renamed_provider():
    from wam import network
    apps = [{"id": "wizard-calendar", "uses": ["wizard-connectors", "agent-service"]},
            {"id": "connected-accounts", "uses": [], "renamed_from": ["wizard-connectors"]}]
    assert network.follow_renames(apps)[0]["uses"] == ["connected-accounts", "agent-service"]
    both = [{"id": "a", "uses": ["old"]}, {"id": "old", "uses": []}, {"id": "new", "uses": [], "renamed_from": ["old"]}]
    assert network.follow_renames(both)[0]["uses"] == ["old"]


# ---------------------------------------------------------------- staging copies (wizard-os#628)

@pytest.mark.parametrize("app_id,want", [
    ("search-preview", ["search", "wizard-search"]), ("movies-next", ["movies", "wizard-movies"]),
    ("agent-philosopher", ["philosopher", "wizard-philosopher"]), ("wizard-notes-test", ["wizard-notes"]),
    ("wizard-search", []), ("cinequeue", []), ("", []),
])
def test_staging_targets(app_id, want):
    assert ar.staging_targets(app_id) == want


SEARCH = {"id": "wizard-search", "name": "Search", "repo": "humanitylabs-org/wizard-search",
          "manifest": {"id": "wizard-search", "renamed_from": ["search-preview"]}}


def test_a_staging_copy_the_released_app_claims_is_due(tmp_path):
    """search-preview was installed from a branch build (local source): it has no update of its own. The released
    wizard-search lists it in renamed_from, so the next update moves it there."""
    c = _box(tmp_path, {"search-preview": {"repo": "local/search-preview", "private": False},
                        "movies-next": {"repo": "humanitylabs-org/movies", "local": True}})
    rt = FakeRT({"search-preview": {"status": "running"}, "movies-next": {"status": "running"}})
    searched = []
    hits = {"wizard-search": [{"id": "wizard-search", "repo": "humanitylabs-org/wizard-search"}],
            "movies": [{"id": "movies", "repo": "humanitylabs-org/movies"}]}
    recs = {"humanitylabs-org/wizard-search": SEARCH,
            "humanitylabs-org/movies": {"id": "movies", "manifest": {"id": "movies"}}}

    def fetch(repo):
        if repo not in recs:
            raise OSError("404")
        return recs[repo]

    def search(app_id):
        searched.append(app_id)
        return hits.get(app_id, [])

    got = ar.due(c, rt, fetch=fetch, search=search)
    # the released Movies does not claim movies-next, so that copy stays as it is
    assert got == [{"from": "search-preview", "to": "wizard-search", "repo": "humanitylabs-org/wizard-search",
                    "name": "Search", "staging": True}]
    assert "wizard-search" in searched
    # no claim, no move; and never onto an id that is already installed
    recs["humanitylabs-org/wizard-search"] = {**SEARCH, "manifest": {"id": "wizard-search"}}
    assert ar.due(c, rt, fetch=fetch, search=search) == []
    recs["humanitylabs-org/wizard-search"] = SEARCH
    rt.apps["wizard-search"] = {"status": "running"}
    assert ar.due(c, rt, fetch=fetch, search=search) == []


def test_an_app_its_own_source_still_publishes_is_never_claimed(tmp_path):
    c = _box(tmp_path, {"search-preview": {"repo": "someone/search-preview"}})
    rt = FakeRT({"search-preview": {"status": "running"}})
    recs = {"someone/search-preview": {"id": "search-preview", "manifest": {"id": "search-preview"}},
            "humanitylabs-org/wizard-search": SEARCH}
    assert ar.due(c, rt, fetch=lambda r: recs[r],
                  search=lambda i: [{"id": "wizard-search", "repo": "humanitylabs-org/wizard-search"}]) == []


def test_rename_due_moves_a_staging_copy_from_the_released_repo(tmp_path, monkeypatch):
    c = _box(tmp_path, {})
    rt = FakeRT({"search-preview": {"status": "running"}, "notes-next": {"status": "running"}})
    rows = [{"from": "search-preview", "to": "wizard-search", "repo": "humanitylabs-org/wizard-search",
             "name": "Search", "staging": True},
            {"from": "notes-next", "to": "notes", "repo": "humanitylabs-org/notes", "name": "Notes"}]
    seen, moved = [], []
    monkeypatch.setattr(ar, "due", lambda c_, rt_, nightly=False, **k: seen.append(nightly) or rows)
    monkeypatch.setattr(ar, "migrate", lambda c_, old, new, repo=None, staging=False:
                        moved.append((old, new, repo, staging)) or {"renamed": True})
    ar.rename_due(c, rt)
    assert seen == [True] and moved == [("search-preview", "wizard-search", "humanitylabs-org/wizard-search", True),
                                        ("notes-next", "notes", None, False)]
    # asked for by id (wam app-updates --apply search-preview): only that app, the nightly holds do not apply
    seen.clear(), moved.clear()
    ar.rename_due(c, rt, only=["search-preview"])
    assert seen == [False] and moved == [("search-preview", "wizard-search", "humanitylabs-org/wizard-search", True)]


def test_migrate_without_a_new_id_takes_the_staging_claims_repo(box, monkeypatch):
    c, rt, fs, installs = box
    monkeypatch.setattr(ar, "due", lambda c_, rt_, **k: [{"from": "cinequeue", "to": "movies", "repo": "humanitylabs-org/movies",
                                                     "name": "Movies", "staging": True}])
    asked = []
    real = ar._new_record
    monkeypatch.setattr(ar, "_new_record", lambda c_, old, new, repo=None: asked.append(repo) or real(c_, old, new))
    fs.files["/data/user-config/wizardapps/cinequeue/docker-compose.yml"] = (
        b"services:\n  cinequeue:\n    image: local/search:d267a685\n    build:\n      context: .\n"
        b"    network_mode: host\n")
    res = ar.migrate(c, "cinequeue", settle=0, probe_seconds=0)
    assert res.get("renamed") is True, res
    assert asked == ["humanitylabs-org/movies"]
    # the branch build pin is dropped so the released image runs; the owner's other values stay
    assert fs.read("/data/user-config/wizardapps/movies/docker-compose.yml") == \
        b"services:\n  movies:\n    network_mode: host\n"


def test_drop_image_pins_keeps_everything_else():
    text = ("services:\n  wizard-search:\n    image: x:1 # pinned\n    environment:\n      A: '1'\n"
            "  other:\n    build:\n      context: .\n      dockerfile: D\n    ports: ['1:1']\n"
            "x-wizard-networks:\n  image: keep\n")
    assert ar.drop_image_pins(text) == ("services:\n  wizard-search:\n    environment:\n      A: '1'\n"
                                        "  other:\n    ports: ['1:1']\nx-wizard-networks:\n  image: keep\n")


# ---------------------------------------------------------------- a move left half done (#832, telegram -> tg-style)

def _leave_old_installed(box, monkeypatch):
    """A move whose uninstall of the old app never went through: the new app runs, the old one stays stopped."""
    c, rt, fs, installs = box
    monkeypatch.setattr(ar, "UNINSTALL_WAITS", (0, 0))
    real = rt.uninstall
    rt.uninstall = lambda urn: (_ for _ in ()).throw(RuntimeError("runtipi queue busy"))
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("renamed") is True and "was not removed" in res["warning"], res
    assert rt.apps["cinequeue"]["status"] == "stopped" and rt.apps["movies"]["status"] == "running"
    assert fs.read("/app-data/wizardapps/cinequeue/data/movies.json")   # its data is back in place
    installed = c.runtipi_root / "apps" / "wizardapps" / "movies"
    installed.mkdir(parents=True)
    (installed / "wizard.md").write_text("---\nid: movies\nname: Movies\nrenamed_from: [cinequeue]\n---\n")
    rt.uninstall = real
    return c, rt, fs, installs


def test_the_old_app_is_asked_to_go_twice(box, monkeypatch):
    c, rt, fs, installs = box
    monkeypatch.setattr(ar, "UNINSTALL_WAITS", (0, 0))
    tries = []
    real = rt.uninstall

    def flaky(urn):
        tries.append(urn)
        if len(tries) == 1:
            raise RuntimeError("runtipi queue busy")
        real(urn)
    rt.uninstall = flaky
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("renamed") is True and "warning" not in res, res
    assert len(tries) == 2 and "cinequeue" not in rt.apps


def test_a_half_done_move_is_due_and_finished(box, monkeypatch):
    c, rt, fs, installs = _leave_old_installed(box, monkeypatch)
    recs = {"humanitylabs-org/cinequeue": {"id": "movies", "manifest": {"id": "movies", "renamed_from": ["cinequeue"]}}}
    got = ar.due(c, rt, fetch=lambda repo: recs[repo])
    assert got == [{"from": "cinequeue", "to": "movies", "repo": None, "name": "Movies", "finish": True}]
    assert ar.due(c, rt, fetch=lambda repo: recs[repo], only=["notes"]) == []
    snap = dict(fs.files)
    assert ar.migrate(c, "cinequeue", dry_run=True)["finish"] is True and fs.files == snap and "cinequeue" in rt.apps
    n = len(installs)
    res = ar.migrate(c, "cinequeue", settle=0, probe_seconds=0)
    assert res.get("renamed") is True and res.get("finished") is True and "warning" not in res, res
    assert "cinequeue" not in rt.apps and rt.apps["movies"]["status"] == "running" and len(installs) == n
    # the old data is kept under a dated name, the new app's copy is untouched
    assert fs.read(res["old_data"] + "/data/movies.json") == b'{"movies": [1, 2, 3]}'
    assert fs.read("/app-data/wizardapps/movies/data/movies.json") == b'{"movies": [1, 2, 3]}'
    assert ar.due(c, rt, fetch=lambda repo: recs[repo]) == []


def test_a_half_done_move_is_finished_when_asked_with_both_ids(box, monkeypatch):
    c, rt, fs, installs = _leave_old_installed(box, monkeypatch)
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("finished") is True and "cinequeue" not in rt.apps, res


def test_two_apps_are_left_alone_without_this_boxs_move_record(box, monkeypatch):
    c, rt, fs, installs = _leave_old_installed(box, monkeypatch)
    fetch = lambda repo: {"id": "movies", "manifest": {"id": "movies", "renamed_from": ["cinequeue"]}}  # noqa: E731
    # the owner started the old app again: never retired behind their back
    rt.apps["cinequeue"]["status"] = "running"
    assert ar.due(c, rt, fetch=fetch) == []
    rt.apps["cinequeue"]["status"] = "stopped"
    # a move that was rolled back, or none at all (both installed by hand): nothing proves the data was copied
    for d in (c.home / "renames").iterdir():
        (d / "result.json").write_text(json.dumps({"rolled_back": True}))
    assert ar.due(c, rt, fetch=fetch) == []
    assert ar.migrate(c, "cinequeue", "movies")["error"] == "movies is already installed"
    assert "cinequeue" in rt.apps


def test_wam_app_update_moves_a_renamed_app(tmp_path, monkeypatch):
    from wam import cli
    asked, moved = [], []
    monkeypatch.setattr(ar, "due", lambda c_, rt=None, only=None, **k: asked.append(only) or (
        [{"from": "telegram", "to": "tg-style", "repo": "humanitylabs-org/tg-style", "name": "TG style"}]
        if only == ["telegram"] else []))
    monkeypatch.setattr(ar, "migrate", lambda c_, old, new, **k: moved.append((old, new)) or
                        {"renamed": True, "from": old, "to": new})
    updated = []
    monkeypatch.setattr(cli, "app_update", lambda c_, app, **k: updated.append(app) or {"status": "running"})
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    assert cli.main(["app", "update", "telegram"]) == 0
    assert moved == [("telegram", "tg-style")] and updated == []
    # an app with no rename updates as before; system apps never move on their own
    assert cli.main(["app", "update", "notes"]) == 0 and updated == ["notes"]
    assert cli.rename_instead_of_update(cli.Config(), "wizard-ux") is None and asked == [["telegram"], ["notes"]]
    # the flag-first form (docs/graceful-restart.md) moves it too, and hands any other app on to Runtipi
    passed = []
    monkeypatch.setattr(cli, "runtipi_cli", lambda root, argv: passed.append(argv) or 0)
    moved.clear()
    assert cli.main(["app", "update", "--no-backup", "telegram"]) == 0 and moved == [("telegram", "tg-style")]
    assert passed == []
    assert cli.main(["app", "update", "--no-backup", "telegram", "notes"]) == 0
    assert passed == [["app", "update", "--no-backup", "notes"]]


# ---------------------------------------------------------------- where Runtipi keeps app-data (#869)

def test_app_data_dir_comes_from_the_runtipi_container_mounts(monkeypatch):
    import subprocess
    from wam import agent_link
    monkeypatch.setattr(ar, "_APP_DATA", [])
    monkeypatch.setattr(agent_link, "_docker", lambda: ["docker"])
    mounts = [{"Source": "/home/oscar/runtipi/state", "Destination": "/data/state"},
              {"Source": "/home/oscar/runtipi/app-data", "Destination": "/app-data"}]
    seen = []

    def run(argv, **k):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(mounts).encode(), b"")
    monkeypatch.setattr(ar.subprocess, "run", run)
    assert ar.app_data_dir() == "/app-data"
    assert ar._paths("wizardapps", "money")["data"] == "/app-data/wizardapps/money"
    assert seen[0][:3] == ["docker", "inspect", "runtipi"] and len(seen) == 1   # asked once, then remembered


def test_app_data_dir_falls_back_to_the_folder_it_finds_and_refuses_when_none(monkeypatch):
    import subprocess
    monkeypatch.setattr(ar, "_APP_DATA", [])
    monkeypatch.setattr(ar, "_mount_destination", lambda: None)
    monkeypatch.setattr(ar, "_sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"/app-data\n", b""))
    assert ar.app_data_dir() == "/app-data"
    monkeypatch.setattr(ar, "_APP_DATA", [])
    monkeypatch.setattr(ar, "_sh", lambda *a, **k: subprocess.CompletedProcess(a, 3, b"", b""))
    with pytest.raises(ar.RenameError):
        ar.app_data_dir()


def _host_data(c, app_id="cinequeue"):
    d = c.runtipi_root / "app-data" / "wizardapps" / app_id / "data"
    d.mkdir(parents=True)
    (d / "finances.db").write_bytes(b"rows")


def test_a_move_that_can_not_see_the_old_data_changes_nothing(box, monkeypatch):
    """money -> finances on oscar-main: the data sat at /app-data, the move looked in /data/app-data, copied 0 files,
    called it done and uninstalled Money, and Runtipi's uninstall deleted the real data."""
    c, rt, fs, installs = box
    _host_data(c)
    monkeypatch.setattr(ar, "app_data_dir", lambda: "/data/app-data")   # a folder the container does not have
    calls = list(rt.calls)
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert "refusing to move it" in res["error"] and not res.get("renamed"), res
    assert not installs and rt.apps["cinequeue"]["status"] == "running" and rt.calls == calls
    assert not (c.home / "renames").exists()


def test_the_old_app_is_never_uninstalled_when_its_data_was_not_kept(box, monkeypatch):
    c, rt, fs, installs = box
    _host_data(c)
    rt.apps["movies"] = {"status": "running"}
    rt.apps["cinequeue"]["status"] = "stopped"
    for k in [k for k in fs.files if k.startswith("/app-data/wizardapps/cinequeue/")]:
        del fs.files[k]   # the container shows no data folder, the host has one
    bdir = c.home / "renames" / "x"
    bdir.mkdir(parents=True)
    res = ar._retire(c, rt, "cinequeue", "movies", {"renamed": True}, lambda s: None, bdir)
    assert "was not removed" in res["warning"] and "not kept" in res["warning"], res
    assert "cinequeue" in rt.apps and ("uninstall", "cinequeue:wizardapps") not in rt.calls


def test_a_half_done_move_without_the_data_is_not_finished(box, monkeypatch):
    c, rt, fs, installs = _leave_old_installed(box, monkeypatch)
    for k in [k for k in fs.files if k.startswith("/app-data/wizardapps/movies/")]:
        del fs.files[k]   # an earlier move installed the new app without any of the old data
    res = ar.migrate(c, "cinequeue", settle=0, probe_seconds=0)
    assert "none of cinequeue's data" in res["error"] and not res.get("renamed"), res
    assert rt.apps["cinequeue"]["status"] == "stopped" and fs.read("/app-data/wizardapps/cinequeue/data/movies.json")


def test_agent_files_under_app_data_go_through_the_app_data_mount(box):
    c, rt, fs, installs = box
    f = c.runtipi_root / "app-data" / "wizardapps" / "wizard-agent-service" / "data" / "wizard-os" / "agents" / "a.json"
    assert ar._under_runtipi(c, f) == "/app-data/wizardapps/wizard-agent-service/data/wizard-os/agents/a.json"
    assert ar._under_runtipi(c, c.runtipi_root / "state" / "x.json") == "/data/state/x.json"
    assert ar._under_runtipi(c, c.home / "routes.json") is None
