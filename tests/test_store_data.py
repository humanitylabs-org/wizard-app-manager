"""App Store browse data: fetched at the shell's own commit, loopback-only unit, prefix-only tailnet route."""
import pytest
from wam import store_data

SHA = "c" * 40
REG = {"apps": [{"id": "wizard-ux", "provides": ["shell"], "commit": SHA}, {"id": "files", "commit": "d" * 40}]}


def fake_fetch(sha, name):
    assert sha == SHA
    return None if name == "store-private-catalog.mjs" else f"// {name}".encode()


def test_fetches_the_shells_commit_and_writes_a_loopback_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(store_data, "_fetch", fake_fetch)
    monkeypatch.setattr(store_data, "find_node", lambda: "/usr/bin/node")
    calls = []
    monkeypatch.setattr(store_data, "_systemctl", lambda *a: calls.append(a) or type("R", (), {"returncode": 0})())
    out = store_data.ensure(REG, data_root=tmp_path / "data", unit_dir=tmp_path / "units")
    assert out["state"] == "ready" and out["commit"] == SHA and out["changed"]
    tools = tmp_path / "data" / SHA / "tools"
    assert (tools / "store-data-origin.mjs").is_file() and not (tools / "store-private-catalog.mjs").exists()
    unit = (tmp_path / "units" / store_data.UNIT).read_text()
    assert f"ExecStart=/usr/bin/node {tools}/store-data-origin.mjs 9584" in unit
    assert ("restart", store_data.UNIT) in calls
    calls.clear()
    assert store_data.ensure(REG, data_root=tmp_path / "data", unit_dir=tmp_path / "units")["changed"] is False
    assert ("start", store_data.UNIT) in calls and ("restart", store_data.UNIT) not in calls


def test_missing_required_file_fails_cleanly(tmp_path):
    with pytest.raises(RuntimeError, match="no tools/store-data-origin.mjs"):
        store_data.fetch_tools(SHA, tmp_path, fetch=lambda s, n: None)
    assert not any(tmp_path.iterdir())


def test_no_pinned_shell_is_skipped():
    assert store_data.ensure({"apps": [{"id": "wizard-ux", "provides": ["shell"]}]}, apply=False)["state"] == "skipped"


def test_route_is_prefix_only_on_the_shell_port():
    assert store_data.route(8796) == ["tailscale", "serve", "--bg", "--https=8796",
                                      "--set-path=/wizard-os-home/store-data",
                                      "http://127.0.0.1:9584/wizard-os-home/store-data"]
