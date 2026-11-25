"""#1053: one app's unreadable recipe folder (root-owned 0700 after a Runtipi restore) never fails another app's
rename or update, a rename's rollback says whether the old app is back, and a rename keeps the gateway path."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import app_rename as ar
from wam import app_updates, network, registry, repo_moves

from test_app_rename import box  # noqa: F401 - the rename fixture (fake Runtipi, fake /data)


def _recipe(d: Path, app_id: str, view: str = "/"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "wizard.md").write_text(f"---\nid: {app_id}\nname: {app_id.title()}\nversion: 1\nview:\n  path: {view}\n"
                                 "mcp:\n  port: 80\n---\n")
    (d / "config.json").write_text('{"port": 8800}')
    (d / "docker-compose.yml").write_text(f"services:\n  {app_id}:\n    image: x\n")
    (d / ".wizard-source").write_text(json.dumps({"repo": f"humanitylabs-org/{app_id}", "source": "standard"}))


@pytest.fixture
def locked_root(tmp_path, monkeypatch):
    """A Runtipi root with Finances and a Calendar whose installed folder this user can't enter."""
    root = tmp_path / "runtipi"
    for a in ("finances", "wizard-calendar"):
        _recipe(root / "apps" / "wizardapps" / a, a)
        _recipe(root / "repos" / "wizardapps" / "apps" / a, a)
    locked = root / "apps" / "wizardapps" / "wizard-calendar"
    real_stat, real_listdir = os.stat, os.listdir

    def guarded(p):   # what a root-owned 0700 folder does to uid 1000, also when the tests run as root
        if str(p).startswith(str(locked) + os.sep):
            raise PermissionError(13, "Permission denied", str(p))

    def stat(p, *a, **k):
        guarded(p)
        return real_stat(p, *a, **k)

    def listdir(p="."):
        guarded(os.path.join(str(p), "x"))
        return real_listdir(p)
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (guarded(self), open(self, *a, **k).read())[1])
    monkeypatch.setattr(Path, "read_bytes", lambda self: (guarded(self), open(self, "rb").read())[1])
    return root


ROWS = [{"app": {"status": "running", "port": p}, "info": {"urn": f"{a}:wizardapps"}}
        for a, p in (("finances", 8809), ("wizard-calendar", 8810))]


def test_registry_lists_every_app_and_warns_about_the_unreadable_one(locked_root):
    reg = registry.build(ROWS, "wizardapps", "127.0.0.1", locked_root)
    assert [a["id"] for a in reg["apps"]] == ["finances", "wizard-calendar"]
    assert len(reg["warnings"]) == 1 and reg["warnings"][0].startswith("wizard-calendar: ")
    assert "Permission denied" in reg["warnings"][0] and "store copy" in reg["warnings"][0]


def test_registry_leaves_out_an_app_with_no_readable_copy_and_says_so(locked_root):
    import shutil
    shutil.rmtree(locked_root / "repos" / "wizardapps" / "apps" / "wizard-calendar")
    reg = registry.build(ROWS, "wizardapps", "127.0.0.1", locked_root)
    assert [a["id"] for a in reg["apps"]] == ["finances"]
    assert "left out of apps.json" in reg["warnings"][0]


def test_network_plan_holds_the_unreadable_app_and_plans_the_rest(locked_root):
    apps = network.read_apps(locked_root, "wizardapps", ["finances", "wizard-calendar"])
    by = {a["id"]: a for a in apps}
    assert by["finances"].get("unreadable") is None and by["finances"]["services"]
    assert "can't be read" in by["wizard-calendar"]["unreadable"]
    p = network.plan(apps, [], set())
    assert network.warnings(p) == [f"wizard-calendar: {by['wizard-calendar']['unreadable']}; its networks were left as they are"]


def test_drift_and_cleanup_never_touch_networks_of_a_held_app():
    held = {"id": "wizard-calendar", "unreadable": "x", "custom": "x", "services": {}, "uses": []}
    mine = {"id": "finances", "uses": [], "services": {"finances": {"docker_networks": [], "on_shared": False,
                                                                     "network_mode": "", "networks": {}}}}
    p = {"apps": [held, {**mine, "state": "no-network", "want": {"finances": {}}}], "networks": {}}
    link = network.link_name("finances", "wizard-calendar")
    live = {"networks": {link: {"labels": {network.LABEL: "link", "org.wizard-os.consumer": "finances",
                                           "org.wizard-os.provider": "wizard-calendar"}, "containers": []}},
            "containers": [{"urn": "finances:wizardapps", "running": True, "service": "finances", "name": "f",
                            "networks": {link: []}, "network_mode": ""},
                           {"urn": "wizard-calendar:wizardapps", "running": True, "service": "wizard-calendar",
                            "name": "cal", "networks": {link: []}, "network_mode": ""}]}
    assert network.held_networks(p, live) == {link}
    assert network.drift(p, live) == []   # no "extra" link to drop, no row for the held app's containers


def test_repo_move_check_reads_a_root_only_source_through_the_runtipi_container(tmp_path, monkeypatch):
    rel = "apps/wizardapps/wizard-calendar/.wizard-source"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("{}")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    seen = []
    monkeypatch.setattr(ar, "rt_read", lambda p: (seen.append(p), b'{"repo": "x/y"}')[1])
    assert repo_moves._read(tmp_path, rel) == b'{"repo": "x/y"}' and seen == ["/data/" + rel]


# ---------------------------------------------------------------- rename rollback (ask 2) and gateway path (ask 4)

def test_rollback_is_true_when_the_old_app_is_back_even_if_the_registry_refresh_fails(box, monkeypatch):
    from wam import cli
    c, rt, fs, installs = box
    monkeypatch.setattr(app_updates, "_probe", lambda url, mcp, t: url.endswith("/old"))   # new app never answers
    monkeypatch.setattr(app_updates, "_endpoint", lambda c_, rt_, app: ("http://127.0.0.1:9234/old", True)
                        if app == "cinequeue" else ("http://127.0.0.1:9234/new", True))

    def registry_fails(*a, **k):   # every registry refresh hits another app's unreadable folder (old managers)
        raise PermissionError(13, "Permission denied: '/runtipi/apps/wizardapps/wizard-calendar/wizard.md'")
    monkeypatch.setattr(cli, "write_registry", registry_fails)
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {"errors": ["wizard-calendar: Permission denied"]})
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res["rolled_back"] is True, res
    assert "rollback_errors" not in res and "old_app" not in res
    assert any(w.startswith("registry: PermissionError") for w in res["warnings"])
    assert any(w.startswith("networks: ") for w in res["warnings"])
    assert rt.apps["cinequeue"]["status"] == "running" and rt.apps["cinequeue"]["port"] == 9234
    saved = json.loads((Path(res["backup"]) / "result.json").read_text())
    assert saved["rolled_back"] is True and saved["old_app"] == "back" and saved["warnings"] == res["warnings"]


def test_rollback_is_false_when_the_old_app_is_not_on_its_port(box, monkeypatch):
    c, rt, fs, installs = box
    monkeypatch.setattr(app_updates, "_probe", lambda url, mcp, t: False)
    real_req = rt.req

    def req(method, path, body=None, timeout=60):   # the "old port back" step silently does nothing
        if body and body.get("port") == 9234:
            return {}
        return real_req(method, path, body, timeout)
    rt.req = req
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res["rolled_back"] is False and "port 10234 (it was on 9234)" in res["old_app"], res


def test_rollback_is_false_when_the_old_app_answered_before_and_not_after(box, monkeypatch):
    from wam import cli
    c, rt, fs, installs = box
    monkeypatch.setattr(cli, "write_registry", lambda *a, **k: {"apps": [
        {"id": "cinequeue", "mcp": {"url": "http://127.0.0.1:9234/mcp"}}]})
    answers = iter([True])   # the baseline answers, nothing after it does
    monkeypatch.setattr(app_updates, "_probe", lambda url, mcp, t: next(answers, False))
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res["rolled_back"] is False and "does not answer" in res["old_app"], res


def test_rename_keeps_the_owners_gateway_path_and_never_writes_root(box):
    c, rt, fs, installs = box
    (c.home / "routes.json").write_text(json.dumps({"cinequeue": {"view": "/cinequeue/"}, "notes": {"view": "/notes/"}}))
    recipe = c.runtipi_root / "repos" / "wizardapps" / "apps" / "movies"
    (recipe / "wizard.md").write_text("---\nid: movies\nname: Movies\nrenamed_from: [cinequeue]\nview:\n  path: /\n---\n")
    res = ar.migrate(c, "cinequeue", "movies", settle=0, probe_seconds=0)
    assert res.get("renamed") is True, res
    routes = json.loads((c.home / "routes.json").read_text())
    assert routes == {"movies": {"view": "/cinequeue/"}, "notes": {"view": "/notes/"}}


def test_routes_keep_every_view_and_carry_the_staging_label():
    doc = {"money": {"view": "/money/"}, "cinequeue": {"view": "/cinequeue-next/", "staged_as": "cinequeue-next"},
           "x": {"view": "/mine/"}}
    assert ar.rename_in_routes(doc, "money", "finances")["finances"] == {"view": "/money/"}
    assert ar.rename_in_routes(doc, "cinequeue", "movies")["movies"] == {"view": "/cinequeue-next/", "staged_as": "movies-next"}
    assert ar.rename_in_routes(doc, "x", "y")["y"] == {"view": "/mine/"}


# ---------------------------------------------------------------- update rollback restores the folder mode (ask 3)

def test_update_rollback_opens_a_root_only_recipe_folder(tmp_path, monkeypatch):
    root = tmp_path / "runtipi"
    for rel in ("apps/wizardapps/wizard-calendar", "user-config/wizardapps/wizard-calendar", "apps/wizardapps/notes"):
        (root / rel).mkdir(parents=True)
    (root / "apps/wizardapps/wizard-calendar").chmod(0o700)
    (root / "user-config/wizardapps/wizard-calendar").chmod(0o700)
    ran = []

    def sh(script, *args, **k):   # stands in for `docker exec runtipi sh -c ...` as root
        ran.append(args[0])
        (root / args[0].removeprefix("/data/")).chmod(0o755)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(ar, "_sh", sh)
    fixed = app_updates.open_restored_folders(SimpleNamespace(runtipi_root=root), "wizard-calendar:wizardapps")
    assert fixed == ["apps/wizardapps/wizard-calendar", "user-config/wizardapps/wizard-calendar"]
    assert oct(os.stat(root / "apps/wizardapps/wizard-calendar").st_mode & 0o777) == "0o755"
    # a folder that is not 0700 is never touched
    assert app_updates.open_restored_folders(SimpleNamespace(runtipi_root=root), "notes:wizardapps") == []
    assert ran == ["/data/apps/wizardapps/wizard-calendar", "/data/user-config/wizardapps/wizard-calendar"]


def test_update_rollback_runs_the_folder_fix_after_the_restore(tmp_path, monkeypatch):
    order = []

    class RT:
        def list_backups(self, urn):
            return [{"id": "b1", "date": 10**13}]

        def restore_backup(self, urn, bid):
            order.append("restore")

        def wait_status(self, urn, want, t):
            return "running"

        def app(self, urn):
            return {"app": {"version": 3}}
    monkeypatch.setattr(app_updates.time, "sleep", lambda s: None)
    monkeypatch.setattr(app_updates, "open_restored_folders", lambda c, urn: order.append(("open", urn)))
    from wam import cli
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: order.append("networks"))
    res = app_updates.rollback(SimpleNamespace(), RT(), "wizard-calendar:wizardapps", 0, {"status": "running", "from": 3})
    assert res["rolled_back"] is True
    assert order == ["restore", ("open", "wizard-calendar:wizardapps"), "networks"]
