"""The installer's WIZARD_PRIVATE_FIRST reaches the Agent Service env (WAM#759, wizard-os#676, DECISIONS #115).

Unset by default: nothing is recorded and no override changes. `WIZARD_PRIVATE_FIRST=1|0` at install (or
`wam private-first on|off`) records the choice and writes it as its own marked block of the Agent Service's
user-config override, which Runtipi applies when bootstrap first creates the container and keeps across updates.
Synthetic paths only.
"""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from wam import alongside, browser_boundary as bb, cli, private_first as pf
from wam.network import load_yaml

STORE = "wizardapps"
AS = "wizard-agent-service"
ROOT = Path(__file__).resolve().parents[1]
ALONGSIDE_LIKE = """\
# Written by wam (alongside mode): header kept.
services:
  wizard-agent-service:
    network_mode: host
    networks: !reset []
    environment:
      WIZARD_HOST: 127.0.0.1
"""


def _ovr(rt):
    return pf.override_path(rt, STORE, AS)


def _env(rt):
    doc = load_yaml(_ovr(rt).read_text())
    return doc["services"][AS].get("environment") or {}


def test_unset_records_nothing_and_writes_nothing(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    assert pf.choice(home) is None
    assert pf.sync_app(home, rt, STORE, AS) is False
    assert not _ovr(rt).exists() and not pf.state_path(home).exists()


@pytest.mark.parametrize("raw,want", [("1", "1"), ("on", "1"), ("TRUE", "1"), ("0", "0"), ("off", "0"),
                                      ("", None), (None, None), ("maybe", None), ("2", None)])
def test_only_a_clear_value_counts(raw, want):
    assert pf.parse(raw) == want


def test_on_puts_the_value_in_the_agent_service_env_before_it_exists(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    res = pf.set_box(home, rt, STORE, [], "1")            # fresh box: nothing installed yet, the choice is recorded
    assert res["changed"] == [] and pf.choice(home) == "1"
    assert oct(pf.state_path(home).stat().st_mode & 0o777) == "0o600"
    assert pf.sync_app(home, rt, STORE, AS) is True      # what `wam install` does when bootstrap installs the AS
    doc = load_yaml(_ovr(rt).read_text())
    assert doc["services"][AS] == {"environment": {"WIZARD_PRIVATE_FIRST": "1"}}   # env only, no volumes key
    assert doc[pf.MARK] == {"service": AS, "environment": ["WIZARD_PRIVATE_FIRST"], "volumes": []}
    assert pf.sync_app(home, rt, STORE, AS) is False     # idempotent
    assert pf.status(home, rt, STORE, [AS]) == {"value": "1", "apps": {AS: "1"}}


def test_off_writes_zero_and_clear_removes_only_its_block(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    _ovr(rt).parent.mkdir(parents=True)
    _ovr(rt).write_text(ALONGSIDE_LIKE)
    assert pf.set_box(home, rt, STORE, [AS], "0")["changed"] == [AS]
    assert _env(rt) == {"WIZARD_HOST": "127.0.0.1", "WIZARD_PRIVATE_FIRST": "0"}
    assert _ovr(rt).read_text().startswith("# Written by wam (alongside mode): header kept.\n")
    assert pf.set_box(home, rt, STORE, [AS], None)["changed"] == [AS]
    assert pf.choice(home) is None
    doc = load_yaml(_ovr(rt).read_text())
    assert pf.MARK not in doc and _env(rt) == {"WIZARD_HOST": "127.0.0.1"}
    assert doc["services"][AS]["network_mode"] == "host"


def test_lives_beside_the_browser_boundary_block(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    bb.set_box(home, rt, STORE, [AS], True)
    pf.set_box(home, rt, STORE, [AS], "1")
    env = _env(rt)
    assert env[bb.SWITCH] == "1" and env["WIZARD_PRIVATE_FIRST"] == "1"
    bb.set_box(home, rt, STORE, [AS], False)               # turning the boundary off keeps private first
    assert _env(rt) == {"WIZARD_PRIVATE_FIRST": "1"}
    pf.set_box(home, rt, STORE, [AS], None)                # and clearing it leaves no override at all
    assert not _ovr(rt).exists()


def test_alongside_rewrite_keeps_the_block_for_the_agent_service_only(tmp_path, monkeypatch):
    home, rt = tmp_path / "home", tmp_path / "rt"
    st = {"shell": {"port": 8796}, "agent": {"hermes_home": str(tmp_path / "hermes"), "origin": "http://127.0.0.1:8642"}}
    monkeypatch.setattr(alongside, "load", lambda h: st)
    monkeypatch.setattr(alongside, "save", lambda h, s: None)
    monkeypatch.setattr(alongside, "reconcile_images", lambda s, a: None)
    monkeypatch.setattr(alongside, "gateway_token", lambda h: "t")
    monkeypatch.setattr(alongside, "host_media_root", lambda r: None)
    monkeypatch.setattr(alongside, "transcriber_origin", lambda r, s: None)
    monkeypatch.setattr(alongside, "shell_override", lambda st, app_id, *a: ALONGSIDE_LIKE.replace(AS, app_id))
    pf.record(home, "1")
    out = alongside.write_shell_override(rt, STORE, "wizard-ux", home, "shell")
    assert pf.MARK not in out.read_text()                  # the shell never gets it
    out = alongside.write_shell_override(rt, STORE, AS, home, "service")   # rewrites the whole file
    doc = load_yaml(out.read_text())
    assert doc[pf.MARK]["service"] == AS and doc["services"][AS]["environment"]["WIZARD_PRIVATE_FIRST"] == "1"


def test_a_hand_written_value_is_never_overwritten(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    _ovr(rt).parent.mkdir(parents=True)
    _ovr(rt).write_text("services:\n  wizard-agent-service:\n    environment:\n      WIZARD_PRIVATE_FIRST: '0'\n")
    before = _ovr(rt).read_text()
    with pytest.raises(ValueError, match="hand-written WIZARD_PRIVATE_FIRST"):
        pf.set_box(home, rt, STORE, [AS], "1")
    assert _ovr(rt).read_text() == before and pf.choice(home) is None   # nothing recorded either


def test_dry_run_changes_nothing(tmp_path):
    home, rt = tmp_path / "home", tmp_path / "rt"
    assert pf.set_box(home, rt, STORE, [AS], "1", dry_run=True)["changed"] == [AS]
    assert not _ovr(rt).exists() and pf.choice(home) is None


def test_cli_targets_only_agent_service_apps_and_restarts_them(tmp_path, monkeypatch, capsys):
    home, rt = tmp_path / "home", tmp_path / "rt"
    c = SimpleNamespace(home=home, runtipi_root=rt, runtipi=lambda: "RT")
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, r=None: {AS: "service", "wizard-ux": "shell"})
    restarted = []
    monkeypatch.setattr(cli, "recreate_apps", lambda c, r, ids: restarted.extend(ids) or {a: "recreated" for a in ids})
    args = SimpleNamespace(action="on", no_restart=False, dry_run=False, json=True)
    assert cli.private_first_cmd(c, args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] == [AS] and restarted == [AS] and out["restart"] == {AS: "recreated"}
    assert not pf.override_path(rt, STORE, "wizard-ux").exists()
    args.action = "status"
    cli.private_first_cmd(c, args)
    assert json.loads(capsys.readouterr().out) == {"value": "1", "apps": {AS: "1"}}


def _installer_block():
    text = (ROOT / "install.sh").read_text()
    start = text.index('case "${WIZARD_PRIVATE_FIRST:-}" in')
    return text[start:text.index("esac", start) + 4]


@pytest.mark.parametrize("value,want", [("", None), ("1", "private-first on"), ("on", "private-first on"),
                                        ("0", "private-first off"), ("bogus", None)])
def test_installer_passes_the_variable_to_wam(tmp_path, value, want):
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    log = tmp_path / "calls"
    (bindir / "wam").write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    os.chmod(bindir / "wam", 0o755)
    prog = "set -euo pipefail; DEGRADED=(); " + _installer_block() + '; echo "degraded=${#DEGRADED[@]}"'
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    if value:
        env["WIZARD_PRIVATE_FIRST"] = value
    r = subprocess.run(["bash", "-c", prog], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "degraded=0" in r.stdout
    calls = log.read_text().strip() if log.exists() else None
    assert calls == want
    if value == "bogus":
        assert "is not 1 or 0; left unset" in r.stdout


def test_get_sh_forwards_the_variable_to_the_install_user():
    text = (ROOT / "get.sh").read_text()
    body = text[text.index("fwd_env() {"):]
    body = body[:body.index("\n}\n")]
    assert 'WIZARD_PRIVATE_FIRST="${WIZARD_PRIVATE_FIRST:-}"' in body
