import json, shutil, subprocess, sys, threading, time, urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam import registry, store, wizardmd  # noqa: E402
from wam.runtipi import cli_token  # noqa: E402

TEMPLATE = ROOT / "templates" / "hello-wizard"


def test_template_passes_checker():
    res = wizardmd.check(TEMPLATE)
    assert wizardmd.passed(res), wizardmd.failures(res)


def test_missing_skill_fails(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    (bad / "skill" / "SKILL.md").unlink()
    assert "skill ships with name+description" in wizardmd.failures(wizardmd.check(bad))


def test_bad_slot_fails(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    p = bad / "wizard.md"
    p.write_text(p.read_text().replace("provides: [diagnostics]", "provides: [Not A Slot]"))
    assert "provides are slot ids" in wizardmd.failures(wizardmd.check(bad))


def test_repo_without_wizard_md_is_skipped(tmp_path):
    (tmp_path / "random").mkdir()
    cands, errs = store.discover([{"local": str(TEMPLATE)}, {"local": str(tmp_path / "random")}], tmp_path / "cache")
    cat = store.compile_store(cands, tmp_path / "out")
    assert [a["id"] for a in cat["apps"]] == ["hello-wizard"]
    assert cat["skipped"] and "wizard.md present" in cat["skipped"][0]["failed"]


def test_compiled_store_is_stock_runtipi_layout(tmp_path):
    cands, _ = store.discover([{"local": str(TEMPLATE)}], tmp_path / "cache")
    out = tmp_path / "out"
    store.compile_store(cands, out)
    app = out / "apps" / "hello-wizard"
    for f in ("config.json", "docker-compose.yml", "metadata/description.md", "metadata/logo.jpg"):
        assert (app / f).is_file(), f
    assert json.loads((app / "config.json").read_text())["id"] == "hello-wizard"
    assert (out / "skills" / "hello-wizard" / "SKILL.md").is_file()
    head = store.commit_store(out)
    assert len(head) == 40


def test_monorepo_glob(tmp_path):
    mono = tmp_path / "mono" / "apps"
    shutil.copytree(TEMPLATE, mono / "hello-wizard")
    two = mono / "second"
    shutil.copytree(TEMPLATE, two)
    for f in (two / "wizard.md", two / "runtipi" / "config.json"):
        f.write_text(f.read_text().replace("hello-wizard", "second-app"))
    cands, _ = store.discover([{"local": str(tmp_path / "mono"), "glob": "apps/*"}], tmp_path / "cache")
    cat = store.compile_store(cands, tmp_path / "out")
    assert sorted(a["id"] for a in cat["apps"]) == ["hello-wizard", "second-app"]


def test_registry_join_and_slots(tmp_path):
    cands, _ = store.discover([{"local": str(TEMPLATE)}], tmp_path / "cache")
    out = tmp_path / "out"
    cat = store.compile_store(cands, out)
    installed = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizard"}},
                 {"app": {"status": "running", "port": 80}, "info": {"urn": "nextcloud:migration"}}]
    reg = registry.build(cat, installed, "wizard", "10.0.0.5", out / "skills")
    assert [a["id"] for a in reg["apps"]] == ["hello-wizard"]
    a = reg["apps"][0]
    assert a["mcp"]["url"] == "http://10.0.0.5:8799/mcp"
    assert a["mcp"]["internal"] == "http://hello-wizard:8799/mcp"
    assert reg["slots"]["diagnostics"]["default"] == "hello-wizard"
    p = registry.write(reg, out / "skills", tmp_path / "home")
    assert json.loads(p.read_text())["apps"][0]["skill"] == "skills/hello-wizard/SKILL.md"
    assert (tmp_path / "home" / "skills" / "hello-wizard" / "SKILL.md").is_file()


def test_cli_token_is_valid_hs256():
    jwt = pytest.importorskip("jwt")
    claims = jwt.decode(cli_token("s3cret"), "s3cret", algorithms=["HS256"])
    assert claims["sub"] == "cli"


def test_template_mcp_server_speaks_mcp():
    src = (TEMPLATE / "app" / "server.py").read_text()
    port = 18799
    proc = subprocess.Popen([sys.executable, "-c", src], env={"PORT": str(port), "PATH": "/usr/bin:/bin"})
    try:
        def rpc(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", json.dumps(body).encode(), {"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=5))
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1); break
            except Exception:
                time.sleep(0.1)
        assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]["serverInfo"]["name"] == "hello-wizard"
        names = [t["name"] for t in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        assert names == ["wizard_time", "wizard_echo"]
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wizard_echo", "arguments": {"text": "hi"}}})
        assert r["result"]["content"][0]["text"] == "hi"
    finally:
        proc.terminate()


def test_compose_inlines_the_same_server():
    compose = (TEMPLATE / "runtipi" / "docker-compose.yml").read_text()
    import yaml
    cmd = yaml.safe_load(compose)["services"]["hello-wizard"]["command"][2]
    assert cmd.strip() == (TEMPLATE / "app" / "server.py").read_text().strip()


def test_future_timestamp_fails(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(TEMPLATE, bad)
    p = bad / "runtipi" / "config.json"
    cfg = json.loads(p.read_text()); cfg["created_at"] = int(time.time() * 1000) + 86_400_000
    p.write_text(json.dumps(cfg))
    assert "runtipi timestamps not in future" in wizardmd.failures(wizardmd.check(bad))
