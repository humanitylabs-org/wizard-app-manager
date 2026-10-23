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


def test_sibling_modules_the_adapter_imports_are_fetched_too(tmp_path):
    """Live 0.3.6: store-private-catalog.mjs imports ./wizard-alpha.mjs; without it node exits ERR_MODULE_NOT_FOUND."""
    files = {
        "store-data-origin.mjs": b"import { x } from './store-index-views.mjs';\nimport {w} from './store-private-catalog.mjs';",
        "store-index-views.mjs": b"export const x = 1;",
        "system-overview.mjs": b"export const y = 2;",
        "store-private-catalog.mjs": b"import {cached, refresh} from './wizard-alpha.mjs';\nconst m = await import('./lazy.mjs');",
        "wizard-alpha.mjs": b"import {join} from 'node:path';\nexport const cached = 1;",
        "lazy.mjs": b"export default 1;",
    }
    asked = []
    tools = store_data.fetch_tools(SHA, tmp_path, fetch=lambda s, n: asked.append(n) or files.get(n))
    assert (tools / "wizard-alpha.mjs").is_file() and (tools / "lazy.mjs").is_file()
    assert sorted(set(asked)) == sorted(files) and len(asked) == len(set(asked))


def test_missing_imported_module_fails_cleanly(tmp_path):
    files = {"store-data-origin.mjs": b"import './gone.mjs';", "store-index-views.mjs": b"", "system-overview.mjs": b""}
    with pytest.raises(RuntimeError, match="no tools/gone.mjs"):
        store_data.fetch_tools(SHA, tmp_path, fetch=lambda s, n: files.get(n))
    assert not any(tmp_path.iterdir())


def test_a_cache_missing_an_imported_module_is_refetched(tmp_path):
    """Live 0.3.7 rerun: a 0.3.6 cache (no wizard-alpha.mjs) was reused as complete and the unit kept failing."""
    tools = tmp_path / SHA / "tools"
    tools.mkdir(parents=True)
    for n in ("store-data-origin.mjs", "store-index-views.mjs", "system-overview.mjs"):
        (tools / n).write_text("// old")
    (tools / "store-private-catalog.mjs").write_text("import {cached} from './wizard-alpha.mjs';")
    files = {"store-data-origin.mjs": b"// new", "store-index-views.mjs": b"", "system-overview.mjs": b"",
             "store-private-catalog.mjs": b"import {cached} from './wizard-alpha.mjs';", "wizard-alpha.mjs": b"//"}
    asked = []
    out = store_data.fetch_tools(SHA, tmp_path, fetch=lambda s, n: asked.append(n) or files.get(n))
    assert (out / "wizard-alpha.mjs").is_file() and asked
    asked.clear()
    store_data.fetch_tools(SHA, tmp_path, fetch=lambda s, n: asked.append(n) or files.get(n))
    assert asked == [], "a complete cache is reused without refetching"


def test_unit_points_the_adapter_at_the_boxs_agent_service(tmp_path):
    tools = tmp_path / "tools"
    unit = store_data.unit_text("/usr/bin/node", tools, SHA, service="http://127.0.0.1:8798")
    assert "Environment=WIZARD_STORE_SERVICE_ORIGIN=http://127.0.0.1:8798\n" in unit
    for bad in ("https://example.com", "http://10.0.0.5:8798", "http://127.0.0.1:8798/x\nExecStart=/bin/sh"):
        assert "WIZARD_STORE_SERVICE_ORIGIN" not in store_data.unit_text("/usr/bin/node", tools, SHA, service=bad)
    assert "WIZARD_STORE_SERVICE_ORIGIN" not in store_data.unit_text("/usr/bin/node", tools, SHA)
