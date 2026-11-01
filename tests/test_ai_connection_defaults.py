"""Every box gets one connection app per AI provider the agent uses, and each app holds the agent's own sign-in.

Incident: an owner's Opus chats failed with Hermes' generic "Your request was not processed" because the host
Hermes' Claude Code login had expired (ClaudeCodeLoggedOut). His box had no Claude connection app, so nothing
renewed the sign-in or offered Connect/Test. On boxes that did have one (alongside), it renewed its own copy in a
named volume, not the folder the agent actually reads.
"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam import alongside, cli, claude_login, slots  # noqa: E402

CLAUDE_RECIPE = """services:
  wizard-claude:
    image: python:3.12-alpine
    user: "1000:1000"
  wizard-claude-engine:
    image: ghcr.io/humanitylabs-org/wizard-claude:0.1.2
    user: "1000:1000"
    volumes:
      - claude-login:/claude
volumes:
  claude-login:
    name: wizard-ai-accounts-claude
"""
CLAUDE_CFG = {"model": {"default": "claude-opus-5-5", "provider": "claude-subscription-directsdk-experimental"}}


# ---------------------------------------------------------------- (1) defaults: Claude and ChatGPT on every box

def test_stock_defaults_install_claude_and_chatgpt_connection_apps():
    d = slots.load_defaults()
    assert d["ai-connection"]["app"] == "wizard-chatgpt" and d["ai-connection"]["multi"]
    assert d["claude-subscription"]["app"] == "wizard-claude"
    assert d["claude-subscription"]["repo"] == "oscarhenrycollins/claude-connection"   # resolvable before indexing
    assert not d["claude-subscription"].get("required")   # the owner may remove it
    todo = slots.to_install({"apps": []}, d)
    assert "wizard-chatgpt" in todo and "wizard-claude" in todo


def test_existing_box_with_only_chatgpt_gets_claude_and_not_twice():
    d = slots.load_defaults()
    reg = {"apps": [{"id": "wizard-chatgpt", "provides": ["ai-connection", "chatgpt-subscription"]}]}
    assert "wizard-claude" in slots.to_install(reg, d) and "wizard-chatgpt" not in slots.to_install(reg, d)
    reg["apps"].append({"id": "wizard-claude", "provides": ["ai-connection", "claude-subscription"]})
    assert not {"wizard-claude", "wizard-chatgpt"} & set(slots.to_install(reg, d))


def test_a_released_manifest_without_claude_keeps_its_own_default_set(monkeypatch, tmp_path):
    """Pinned releases decide which defaults exist (release_pins.defaults); this change reaches them only when a
    release adds the claude-subscription row. Here: unchanged for old manifests."""
    rel = json.loads((ROOT / "releases" / "0.3.15.json").read_text())
    p = tmp_path / "rel.json"
    p.write_text(json.dumps(rel))
    monkeypatch.setenv("WIZARD_RELEASE_PINS", str(p))
    assert "claude-subscription" not in slots.load_defaults()


# ---------------------------------------------------------------- (2) alongside: one sign-in, the agent's

def test_detects_claude_subscription_provider_main_or_fallback():
    assert claude_login.uses_claude_subscription(CLAUDE_CFG)
    assert claude_login.uses_claude_subscription(
        {"model": {"provider": "openai-codex"}, "fallback_providers": [{"provider": "claude-subscription-x"}]})
    assert not claude_login.uses_claude_subscription({"model": {"provider": "openai-codex"}})
    assert not claude_login.uses_claude_subscription({})


def test_login_dir_follows_the_provider_resolution(tmp_path):
    hh = tmp_path / "hermes"
    hh.mkdir()
    uid = os.getuid()
    assert claude_login.login_dir(hh, uid, env={}) == claude_login.owner_home(uid) / ".claude"
    assert claude_login.login_dir(hh, uid, env={"CLAUDE_CONFIG_DIR": "/srv/cc"}) == Path("/srv/cc")
    (hh / ".env").write_text("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR=/srv/agent-claude\n")
    assert claude_login.login_dir(hh, uid, env={"CLAUDE_CONFIG_DIR": "/srv/cc"}) == Path("/srv/agent-claude")


@pytest.mark.parametrize("rootless,user", [(True, "0:0"), (False, "1001:1002")])
def test_engine_binds_the_agents_login_as_the_owner(tmp_path, monkeypatch, rootless, user):
    """Rootless Docker: container root IS the owner (files stay owner-owned). Rootful: never container root in the
    owner's folder; run as the owner's own uid:gid so it can read the 0600 credentials and nothing turns root-owned."""
    rt, hh, login = tmp_path / "runtipi", tmp_path / "hermes", tmp_path / "owner" / ".claude"
    (rt / "apps" / "wizardapps" / "wizard-claude").mkdir(parents=True)
    (rt / "apps" / "wizardapps" / "wizard-claude" / "docker-compose.yml").write_text(CLAUDE_RECIPE)
    hh.mkdir()
    (hh / ".env").write_text(f"CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR={login}\n")
    out = claude_login.ensure(rt, "wizardapps", "wizard-claude", ["ai-connection", "claude-subscription"], hh,
                              1001, 1002, rootless, CLAUDE_CFG)
    assert out == {"state": "ready", "folder": str(login), "changed": True}
    assert login.is_dir() and (login.stat().st_mode & 0o777) == 0o700   # made by the owner, not by dockerd as root
    ov = yaml.safe_load((rt / "user-config" / "wizardapps" / "wizard-claude" / "docker-compose.yml").read_text())
    eng = ov["services"]["wizard-claude-engine"]
    assert eng["volumes"] == [f"{login}:/claude"] and eng["user"] == user
    assert "wizard-claude" not in ov["services"]          # the MCP front is untouched
    # Idempotent: a rerun changes nothing (no needless app update).
    assert claude_login.ensure(rt, "wizardapps", "wizard-claude", ["claude-subscription"], hh, 1001, 1002, rootless,
                               CLAUDE_CFG)["changed"] is False


def test_keeps_owner_override_and_off_restores_it(tmp_path):
    """An owner's own override (image pin, env) survives; --off removes only the login bind."""
    rt, hh = tmp_path / "runtipi", tmp_path / "hermes"
    (rt / "apps" / "wizardapps" / "wizard-claude").mkdir(parents=True)
    (rt / "apps" / "wizardapps" / "wizard-claude" / "docker-compose.yml").write_text(CLAUDE_RECIPE)
    hh.mkdir()
    (hh / ".env").write_text(f"CLAUDE_CONFIG_DIR={tmp_path}/cc\n")
    ovp = rt / "user-config" / "wizardapps" / "wizard-claude" / "docker-compose.yml"
    ovp.parent.mkdir(parents=True)
    mine = ("services:\n  wizard-claude-engine:\n    image: wizard-claude:private\n    pull_policy: never\n"
            "    environment:\n      X: '1'\n    networks: !reset []\n")
    ovp.write_text(mine)
    claude_login.ensure(rt, "wizardapps", "wizard-claude", [], hh, 1000, 1000, True, CLAUDE_CFG)
    text = ovp.read_text()
    assert "!reset" in text                                # custom compose tags are preserved
    eng = yaml.load(text, Loader=claude_login._Loader)["services"]["wizard-claude-engine"]
    assert eng["image"] == "wizard-claude:private" and eng["environment"] == {"X": "1"}
    assert eng["volumes"] == [f"{tmp_path}/cc:/claude"]
    assert claude_login.remove(ovp)
    back = yaml.load(ovp.read_text(), Loader=claude_login._Loader)
    assert back["services"]["wizard-claude-engine"]["image"] == "wizard-claude:private"
    assert "volumes" not in back["services"]["wizard-claude-engine"]
    assert "user" not in back["services"]["wizard-claude-engine"] and claude_login.MARK not in back
    assert not claude_login.remove(ovp)                    # nothing of ours left


def test_off_deletes_an_override_that_only_held_the_login(tmp_path):
    ovp = tmp_path / "docker-compose.yml"
    claude_login.apply(ovp, ["wizard-claude-engine"], tmp_path / "cc", "0:0")
    assert claude_login.remove(ovp) and not ovp.exists()


def test_skips_when_the_agent_does_not_use_a_claude_subscription(tmp_path):
    rt, hh = tmp_path / "runtipi", tmp_path / "hermes"
    (rt / "apps" / "wizardapps" / "wizard-claude").mkdir(parents=True)
    (rt / "apps" / "wizardapps" / "wizard-claude" / "docker-compose.yml").write_text(CLAUDE_RECIPE)
    hh.mkdir()
    out = claude_login.ensure(rt, "wizardapps", "wizard-claude", [], hh, 1000, 1000, True,
                              {"model": {"provider": "openai-codex"}})
    assert out["state"] == "skipped" and not (rt / "user-config").exists()


def test_only_claude_connection_apps_are_rewired():
    assert claude_login.is_consumer("wizard-claude", [])                     # the default, before its record
    assert claude_login.is_consumer("my-claude", ["ai-connection", "claude-subscription"])
    assert not claude_login.is_consumer("ai-accounts", ["ai-connection"])    # retired app mounting /claude
    assert not claude_login.is_consumer("hermes-agent", ["agent"])


def test_rejects_a_login_path_compose_cannot_carry(tmp_path):
    with pytest.raises(ValueError):
        claude_login.apply(tmp_path / "o.yml", ["e"], Path("/home/a b/.claude"), "0:0")


# ---------------------------------------------------------------- wiring through `wam alongside` / install

@pytest.fixture
def alongside_box(tmp_path, monkeypatch):
    home = tmp_path / "wos"
    home.mkdir()
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    rt = tmp_path / "runtipi"
    for app, recipe in (("wizard-claude", CLAUDE_RECIPE), ("files", "services:\n  files:\n    image: x\n")):
        (rt / "apps" / "wizardapps" / app).mkdir(parents=True)
        (rt / "apps" / "wizardapps" / app / "docker-compose.yml").write_text(recipe)
    monkeypatch.setenv("RUNTIPI_ROOT", str(rt))
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text(yaml.safe_dump(CLAUDE_CFG))
    (hh / ".env").write_text(f"CLAUDE_CONFIG_DIR={tmp_path}/agent-claude\n")
    alongside.save(home, {"mode": "alongside", "agent": {"external": True, "id": "host-hermes", "name": "host Hermes",
                                                         "hermes_home": str(hh), "origin": "http://127.0.0.1:8642",
                                                         "uid": 1000, "gid": 1000},
                          "shell": {"port": 8796}, "service": {"port": 8797}})
    monkeypatch.setattr(alongside, "docker_rootless", lambda: True)
    return cli.Config(), rt, tmp_path / "agent-claude"


def test_claude_login_cmd_wires_installed_claude_apps_only(alongside_box):
    c, rt, login = alongside_box
    out = cli.claude_login_cmd(c, {"wizard-claude": ["ai-connection", "claude-subscription"], "files": ["files"]})
    assert out["changed_apps"] == ["wizard-claude"] and list(out["apps"]) == ["wizard-claude"]
    ov = yaml.safe_load((rt / "user-config" / "wizardapps" / "wizard-claude" / "docker-compose.yml").read_text())
    assert ov["services"]["wizard-claude-engine"]["volumes"] == [f"{login}:/claude"]
    assert not (rt / "user-config" / "wizardapps" / "files").exists()
    assert cli.claude_login_cmd(c, {"wizard-claude": []})["changed_apps"] == []
    assert cli.claude_login_off(c) == ["wizard-claude"]
    assert not (rt / "user-config" / "wizardapps" / "wizard-claude" / "docker-compose.yml").exists()


def test_alongside_cmd_schedules_the_claude_app_update(alongside_box, monkeypatch):
    c, rt, login = alongside_box

    class RT:
        def installed(self): return []
        def wait_status(self, urn, want, timeout=0): return "missing"
        def app(self, urn): return {"app": {"status": "running"}}
    restarted = []
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(cli, "app_lifecycle", lambda c, verb, urn, timeout=300: restarted.append((verb, urn)) or
                        {"status": "running"})
    monkeypatch.setattr(cli, "installed_ids", lambda rt: {"wizard-claude", "files"})
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt=None: {})
    monkeypatch.setattr(cli, "host_dashboard_cmd", lambda c, rt=None: {"state": "skipped"})
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {"apps": []})
    monkeypatch.setattr(alongside, "detect", lambda home: {"found": True, "api_origin": "http://127.0.0.1:8642"})
    out = cli.alongside_cmd(c, False, False, str(Path(json.loads(alongside.state_path(c.home).read_text())
                                                          ["agent"]["hermes_home"])), None)
    assert out["claude_login"]["apps"]["wizard-claude"]["folder"] == str(login)
    # The changed Claude app is recreated right away (Runtipi reads app.env/overrides only on create), not left
    # as a `next` step nobody runs (the installer discards it).
    assert restarted == [("restart", "wizard-claude:wizardapps")] and out["recreated"] == {"wizard-claude": "recreated"}
    assert "wizard-claude" not in out.get("next", "")
    off = cli.alongside_cmd(c, False, True, None, None)
    assert "wizard-claude" in off["overrides_removed"] and "wam app update wizard-claude" in off["next"]
