"""#387: the App Manager writes the gateway's grant keys (grants/agent-service.json) for every app with tools, behind
the off-by-default feature switch wizard/gateway-keys. Providers first, hand-written keys and rows adopted unchanged,
removal on uninstall, 0600 owned by the Agent Service's user."""
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import cli, gateway_keys  # noqa: E402

RUNTIPI_FILES = gateway_keys.RuntipiFiles

HAND = "ab" * 32
HOST = "host:agent-service"


class LocalFiles:
    """RuntipiFiles on the test's own disk: records each write (path, mode, uid, gid) instead of chowning."""

    def __init__(self, owners=None):
        self.writes = []
        self.owners = owners or {}

    def read(self, p):
        return Path(p).read_bytes() if Path(p).is_file() else None

    def stat(self, p):
        p = Path(p)
        if not p.exists() or p.is_symlink():
            return None
        st = p.stat()
        uid, gid = self.owners.get(str(p), (st.st_uid, st.st_gid))
        return stat.S_IMODE(st.st_mode), uid, gid

    def write(self, p, data, mode, uid, gid):
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.writes.append((p.name if p.name != "grants.json" else p.parent.parent.name, mode, uid, gid))
        tmp = p.with_name(p.name + ".t")
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        tmp.replace(p)
        self.owners[str(p)] = (uid, gid)


def test_runtipi_files_uses_the_app_manager_app_data_mount():
    """Runtipi mounts app-data at /app-data, separately from the rest of its root at /data."""
    files = RUNTIPI_FILES(Path("/srv/runtipi"))
    assert files._inner(Path("/srv/runtipi/app-data")) == "/app-data"
    assert files._inner(Path("/srv/runtipi/app-data/wizardapps/messenger/wizard/grants.json")) == (
        "/app-data/wizardapps/messenger/wizard/grants.json"
    )
    assert files._inner(Path("/srv/runtipi/media/example.txt")) == "/data/media/example.txt"


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "wizard-os"
    home.mkdir()
    root = tmp_path / "runtipi"
    os_home = root / "app-data" / "wizardapps" / "wizard-agent-service" / "data" / "wizard-os"
    os_home.mkdir(parents=True)
    files = LocalFiles({str(os_home): (1000, 1000)})   # the service runs as uid 1000 here
    return SimpleNamespace(home=home, runtipi_root=root, os_home=os_home, files=files,
                           gw=os_home / "grants" / "agent-service.json")


def app(i, mcp=True, store="wizardapps"):
    return {"id": i, "urn": f"{i}:{store}", **({"mcp": {"url": "http://127.0.0.1:1/mcp"}} if mcp else {})}


def reg(*apps):
    return {"apps": [app("wizard-agent-service"), app("wizard-ux", mcp=False), *apps]}


def app_file(box, i, incoming=(), enforce=True):
    f = box.runtipi_root / "app-data" / "wizardapps" / i / "wizard" / "grants.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"outgoing": {}, "incoming": list(incoming), "enforce": enforce}))
    return f


def rows(f):
    return json.loads(f.read_text())["incoming"]


def gateway(box):
    return json.loads(box.gw.read_text())["outgoing"]


def test_mints_one_key_per_app_with_tools_and_publishes_rows_first(box):
    m, n = app_file(box, "messenger"), app_file(box, "notes")
    keys = iter(["11" * 32, "22" * 32])
    res = gateway_keys.sync(reg(app("messenger"), app("notes")), box.home, box.runtipi_root, files=box.files,
                            mint=lambda: next(keys))
    assert gateway(box) == {"messenger": "11" * 32, "notes": "22" * 32}
    assert rows(m) == [{"from": HOST, "sha256": gateway_keys.digest("11" * 32)}]
    assert rows(n) == [{"from": HOST, "sha256": gateway_keys.digest("22" * 32)}]
    # providers before the gateway file, which is 0600 and owned by the Agent Service's user
    assert [w[0] for w in box.files.writes] == ["messenger", "notes", "agent-service.json"]
    assert box.files.writes[-1][1:] == (0o600, 1000, 1000)
    assert res["minted"] == ["messenger", "notes"]
    assert "11" * 32 not in json.dumps(res)
    assert json.loads((box.home / gateway_keys.STATE).read_text()) == {"managed": ["messenger", "notes"]}


def test_the_doctor_row_passes_after_a_sync(box):
    app_file(box, "messenger")
    r = reg(app("messenger"))
    gateway_keys.sync(r, box.home, box.runtipi_root, files=box.files)
    name, ok, _ = gateway_keys.check(r, box.home, box.runtipi_root)
    assert name == gateway_keys.NAME and ok


def test_hand_written_key_and_row_are_adopted_unchanged(box):
    """Miguel's hand fix: key in the gateway file and its row in Messenger's file stay exactly as they are."""
    box.gw.parent.mkdir()
    box.gw.write_text(json.dumps({"outgoing": {"messenger": HAND}}))
    os.chmod(box.gw, 0o600)
    box.files.owners[str(box.gw)] = (1000, 1000)
    hand_row = {"from": HOST, "sha256": gateway_keys.digest(HAND)}
    m = app_file(box, "messenger", [{"from": "calendar", "sha256": "cc" * 32}, hand_row])
    before = m.read_bytes()
    res = gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files,
                            mint=lambda: pytest.fail("must not mint over a hand-written key"))
    assert gateway(box) == {"messenger": HAND}
    assert m.read_bytes() == before and box.files.writes == []   # nothing needed rewriting
    assert res["keys"] == ["messenger"] and res["minted"] == [] and res["rows_added"] == []


def test_a_hand_written_key_for_an_app_it_never_managed_is_kept(box):
    box.gw.parent.mkdir()
    box.gw.write_text(json.dumps({"outgoing": {"something-else": HAND}}))
    app_file(box, "messenger")
    gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    assert gateway(box)["something-else"] == HAND and "messenger" in gateway(box)


def test_uninstall_removes_the_row_then_the_key(box):
    m, n = app_file(box, "messenger", [{"from": "calendar", "sha256": "cc" * 32}]), app_file(box, "notes")
    gateway_keys.sync(reg(app("messenger"), app("notes")), box.home, box.runtipi_root, files=box.files)
    box.files.writes.clear()
    res = gateway_keys.sync(reg(app("notes")), box.home, box.runtipi_root, files=box.files)
    assert "messenger" not in gateway(box) and "notes" in gateway(box)
    assert rows(m) == [{"from": "calendar", "sha256": "cc" * 32}]   # the other app's row is untouched
    assert [w[0] for w in box.files.writes] == ["messenger", "agent-service.json"]
    assert res["removed"] == ["messenger"]
    assert json.loads((box.home / gateway_keys.STATE).read_text()) == {"managed": ["notes"]}


def test_app_folder_already_gone_still_drops_the_key(box):
    app_file(box, "messenger")
    gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    import shutil
    shutil.rmtree(box.runtipi_root / "app-data" / "wizardapps" / "messenger")
    gateway_keys.sync(reg(), box.home, box.runtipi_root, files=box.files)
    assert gateway(box) == {}


def test_other_stores_apps_without_tools_and_broken_files_are_left_alone(box):
    app_file(box, "messenger")
    bad = box.runtipi_root / "app-data" / "wizardapps" / "notes" / "wizard" / "grants.json"
    bad.parent.mkdir(parents=True)
    bad.write_text(json.dumps({"incoming": "nope"}))
    r = reg(app("messenger"), app("notes"), app("clock", mcp=False), app("mine", store="private-store"))
    res = gateway_keys.sync(r, box.home, box.runtipi_root, files=box.files)
    assert set(gateway(box)) == {"messenger"}
    assert json.loads(bad.read_text()) == {"incoming": "nope"}
    assert set(res["skipped"]) == {"notes", "mine"}


def test_no_grants_file_yet_means_no_key(box):
    gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    assert gateway(box) == {}


def test_nothing_happens_before_the_agent_service_made_its_data_folder(box, tmp_path):
    import shutil
    shutil.rmtree(box.os_home)
    app_file(box, "messenger")
    res = gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    assert box.files.writes == [] and "*" in res["skipped"]


def test_a_broken_gateway_file_is_never_replaced(box):
    box.gw.parent.mkdir()
    box.gw.write_text("{")
    app_file(box, "messenger")
    res = gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    assert box.gw.read_text() == "{" and box.files.writes == [] and "*" in res["skipped"]


def test_a_wrong_mode_gateway_file_is_fixed(box):
    app_file(box, "messenger")
    gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    os.chmod(box.gw, 0o644)
    box.files.writes.clear()
    gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files)
    assert box.files.writes == [("agent-service.json", 0o600, 1000, 1000)]


# ---------------------------------------------------------------- always on (bug lane, no switch)


def test_the_registry_hook_runs_the_producer_with_no_switch(box):
    """#387 ships as a bug fix: no feature switch; the hook always gives the gateway its keys."""
    app_file(box, "messenger")
    c = SimpleNamespace(home=box.home, runtipi_root=box.runtipi_root)
    res = gateway_keys.sync_if_on(c, reg(app("messenger")), files=box.files)
    assert res["keys"] == ["messenger"] and "messenger" in gateway(box)


def test_the_pre_split_wizard_shell_gateway_gets_its_keys_too(tmp_path):
    """Codex on #632: a box still on the pre-split `wizard-shell` bundle uses the same file `path()` and the doctor
    check look at, so the producer must write it there instead of skipping the box."""
    home, root = tmp_path / "wizard-os", tmp_path / "runtipi"
    home.mkdir()
    os_home = root / "app-data" / "wizardapps" / "wizard-shell" / "data" / "wizard-os"
    os_home.mkdir(parents=True)
    files = LocalFiles({str(os_home): (1000, 1000)})
    box = SimpleNamespace(home=home, runtipi_root=root, files=files)
    app_file(box, "messenger")
    r = {"apps": [app("wizard-shell"), app("messenger")]}
    res = gateway_keys.sync(r, home, root, files=files)
    assert res["keys"] == ["messenger"]
    gw = os_home / "grants" / "agent-service.json"
    assert gateway_keys.path(home, root) == gw and "messenger" in json.loads(gw.read_text())["outgoing"]
    assert gateway_keys.check(r, home, root)[1] is True


def test_no_feature_switch_is_read(monkeypatch):
    import inspect
    from wam import feature_switches
    monkeypatch.setattr(feature_switches, "is_on", lambda *a, **k: (_ for _ in ()).throw(AssertionError("switch read")))
    src = inspect.getsource(gateway_keys)
    assert "feature_switches" not in src and "wizard/gateway-keys" not in src.replace("used to sit behind `wizard/gateway-keys`", "")


def test_a_failure_never_breaks_the_registry_write(box, monkeypatch):
    monkeypatch.setattr(gateway_keys, "sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker down")))
    c = SimpleNamespace(home=box.home, runtipi_root=box.runtipi_root)
    assert gateway_keys.sync_if_on(c, reg()) == {"error": "docker down"}


def test_write_registry_calls_the_hook(monkeypatch):
    import inspect
    assert "gateway_keys.sync_if_on(c, reg)" in inspect.getsource(cli.write_registry)


def test_a_new_key_replaces_old_gateway_rows_but_keeps_other_callers(box):
    """Codex on #546: when the gateway gets a new key, an older host:agent-service row must stop working."""
    box.gw.parent.mkdir()
    box.gw.write_text(json.dumps({"outgoing": {"messenger": "not valid"}}))
    old = {"from": HOST, "sha256": gateway_keys.digest(HAND)}
    other = {"from": "calendar", "sha256": "cc" * 32}
    m = app_file(box, "messenger", [other, old])
    gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=box.files, mint=lambda: "33" * 32)
    assert gateway(box) == {"messenger": "33" * 32}
    assert rows(m) == [other, {"from": HOST, "sha256": gateway_keys.digest("33" * 32)}]


def test_concurrent_syncs_agree_on_one_key(box):
    """Codex on #546: registry writers overlap; two first-time syncs must not publish different keys."""
    import threading
    m = app_file(box, "messenger")
    errors = []

    def run():
        try:
            gateway_keys.sync(reg(app("messenger")), box.home, box.runtipi_root, files=LocalFiles(box.files.owners))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert rows(m) == [{"from": HOST, "sha256": gateway_keys.digest(gateway(box)["messenger"])}]


def test_doctor_leaves_other_stores_apps_to_their_store(box):
    """Codex on #546: an app from another store is not checked against app-data/wizardapps."""
    app_file(box, "messenger")
    r = reg(app("messenger"), app("mine", store="private-store"))
    gateway_keys.sync(r, box.home, box.runtipi_root, files=box.files)
    _, ok, detail = gateway_keys.check(r, box.home, box.runtipi_root)
    assert ok and detail.startswith("1 apps")
