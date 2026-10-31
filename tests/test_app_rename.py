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
    r = _rule({"id": "cinequeue", "name": "Movies"})["app id is the app's name"]
    assert r["ok"] is True and r["severity"] == "warning"


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
                      "adopted": ["wizard-connectors"],
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


def test_routes_keep_gateway_path_and_carry_staging_label():
    doc = {"cinequeue": {"view": "/cinequeue-next/", "staged_as": "cinequeue-next"}, "notes": {"view": "/notes/"}}
    out = ar.rename_in_routes(doc, "cinequeue", "movies")
    assert out == {"movies": {"view": "/cinequeue-next/", "staged_as": "movies-next"}, "notes": {"view": "/notes/"}}


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
        "/data/app-data/wizardapps/cinequeue/data/movies.json": b'{"movies": [1, 2, 3]}',
        "/data/app-data/wizardapps/cinequeue/wizard/grants.json": b"{}",
        "/data/user-config/wizardapps/cinequeue/docker-compose.yml": b"services:\n  cinequeue:\n    network_mode: host\n",
        "/data/state/wizard-grants.json": json.dumps({"grants": [{"from": "cinequeue", "to": "notes"}], "denied": []}).encode(),
    })
    for n, f in (("rt_read", fs.read), ("rt_write", fs.write), ("rt_exists", fs.exists),
                 ("rt_nonempty_dir", fs.nonempty), ("rt_copy_tree", fs.copy), ("rt_move", fs.move),
                 ("rt_tree_summary", fs.summary)):
        monkeypatch.setattr(ar, n, f)
    monkeypatch.setattr(ar, "_sh", lambda *a, **k: None)
    rt = FakeRT({"cinequeue": {"status": "running", "port": 9234, "version": 8,
                               "config": {"CINEQUEUE_WATCH_REGION": "TH", "openPort": False}}})
    c.runtipi = lambda: rt
    installs = []

    def install(c_, app_id, given, _record=None, _form_base=None, **k):
        installs.append((app_id, given, _form_base))
        assert fs.exists("/data/app-data/wizardapps/movies/data/movies.json")   # data is there before first start
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
    assert fs.read("/data/app-data/wizardapps/movies/data/movies.json") == b'{"movies": [1, 2, 3]}'
    # the old data is kept, renamed, never deleted
    assert res["old_data"].startswith("/data/app-data/wizardapps/cinequeue.old-")
    assert fs.read(res["old_data"] + "/data/movies.json") == b'{"movies": [1, 2, 3]}'
    assert json.loads(fs.read("/data/state/wizard-grants.json"))["grants"][0]["from"] == "movies"
    assert b"\n  movies:\n" in fs.read("/data/user-config/wizardapps/movies/docker-compose.yml")
    assert json.loads((c.home / "routes.json").read_text()) == {"movies": {"view": "/cinequeue-next/", "staged_as": "movies-next"}}
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
    assert fs.read("/data/app-data/wizardapps/cinequeue/data/movies.json") == b'{"movies": [1, 2, 3]}'
    assert not (c.secrets / "movies.json").exists() and (c.secrets / "cinequeue.json").is_file()


def test_migrate_refuses_without_renamed_from(box):
    c, rt, fs, installs = box
    (c.runtipi_root / "repos/wizardapps/apps/movies/wizard.md").write_text("---\nid: movies\nname: Movies\n---\n")
    res = ar.migrate(c, "cinequeue", "movies")
    assert "renamed_from" in res["error"] and not installs and rt.apps["cinequeue"]["status"] == "running"


def test_migrate_refuses_when_new_id_has_data(box):
    c, rt, fs, installs = box
    fs.write("/data/app-data/wizardapps/movies/data/x", b"1")
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
