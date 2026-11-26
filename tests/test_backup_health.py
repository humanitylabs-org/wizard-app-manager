"""wizard-ux#1743: Home > Resources > Health shows the box's own backup (switch wizard/backup-health), and tells the
owner once, with a safe Include it, when the main agent is left out (#885, #960)."""
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import FakeBox, _install_now, _package, _w, hermes_home, write_tools  # noqa: E402
from wam import alongside, backup_health as bh, cli  # noqa: E402
from wam.backup import capture, schedule  # noqa: E402
from wam.backup.box import MAIN_AGENT_LEFT_OUT, main_agent_warning  # noqa: E402

GiB = 1 << 30
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def _home(tmp_path, status=None, enabled=True, every=None):
    h = tmp_path / "wizard-os"
    h.mkdir(parents=True, exist_ok=True)
    cfg = {"schema": 1, "enabled": enabled, "target": {"kind": "path", "path": "/srv/b"}}
    if every:
        cfg["every"] = every
    (h / "backup.json").write_text(json.dumps(cfg))
    if status is not None:
        (h / "backup-status.json").write_text(json.dumps(status))
    return h


def _st(hours, **kw):
    return {"ok": True, "verified": True, "error": None, "warnings": [], "snapshots": 4,
            "last_success_at": NOW - int(hours * 3600), "finished_at": NOW - int(hours * 3600), **kw}


def row(h):
    return bh.backup_row(h, NOW)


# ---- 1. the Backup row ---------------------------------------------------------------------------------------------

def test_green_says_when_checked_and_kept(tmp_path):
    r = row(_home(tmp_path, _st(11)))
    assert r == {"key": "backups", "label": "Backup", "status": "ok", "value": "Last backup 11h ago, checked, 4 kept",
                 "detail": ""}


def test_amber_after_36h_unchecked_or_with_warnings(tmp_path):
    assert row(_home(tmp_path, _st(37)))["status"] == "warn"
    assert row(_home(tmp_path, _st(35)))["status"] == "ok"
    r = row(_home(tmp_path, _st(5, verified=False)))
    assert r["status"] == "warn" and "not checked" in r["value"]
    r = row(_home(tmp_path, _st(5, warnings=["notes-app: left out the copy in store old"])))
    assert r["status"] == "warn" and r["value"] == "Last backup 5h ago, 1 warning" and "notes-app" in r["detail"]
    hh = "/home/o/.hermes"
    r = row(_home(tmp_path, _st(5, warnings=[main_agent_warning(hh)])))
    assert r["status"] == "warn" and r["detail"] == "Your main agent (its memory, skills and chats) isn't in this backup."
    assert "alongside" not in r["detail"]                  # plain words, never the command


def test_red_after_48h_on_failure_or_never(tmp_path):
    r = row(_home(tmp_path, _st(49)))
    assert r["status"] == "error" and r["value"] == "Last backup 2 days ago"
    r = row(_home(tmp_path, _st(3, ok=False, error="restic: repository not found")))
    assert r["status"] == "error" and r["value"] == "Last backup failed"
    assert "restic: repository not found" in r["detail"] and "Last good backup 3h ago" in r["detail"]
    r = row(_home(tmp_path, {"ok": False, "error": "no key"}))
    assert r["status"] == "error" and r["value"] == "No backup yet" and "no key" in r["detail"]
    assert row(_home(tmp_path, None))["status"] == "error"     # scheduled, but nothing ran yet


def test_weekly_schedule_waits_a_week(tmp_path):
    assert row(_home(tmp_path, _st(60), every="weekly"))["status"] == "ok"
    assert row(_home(tmp_path, _st(49 * 7), every="weekly"))["status"] == "error"


def test_no_row_while_backups_are_off_or_not_set_up(tmp_path):
    assert row(_home(tmp_path, _st(1), enabled=False)) is None
    h = tmp_path / "bare"
    h.mkdir()
    assert row(h) is None and bh.status(h, NOW)["checks"] == []


# ---- 2. the main agent ---------------------------------------------------------------------------------------------

def test_main_agent_row_with_include_until_it_clears(tmp_path):
    hh = str(tmp_path / "o/.hermes")
    h = _home(tmp_path, _st(5, warnings=[main_agent_warning(hh)]))
    rows = bh.status(h, NOW)["checks"]
    ma = next(r for r in rows if r["key"] == "main-agent-backup")
    assert ma["status"] == "warn" and ma["value"] == "not in backup"
    assert ma["action"] == {"id": "include-main-agent", "label": "Include it"}
    # Include it turned alongside mode on: no button, says the next backup includes it
    (h / "alongside.json").write_text(json.dumps({"mode": "alongside", "agent": {"hermes_home": hh}}))
    ma = next(r for r in bh.status(h, NOW)["checks"] if r["key"] == "main-agent-backup")
    assert ma["status"] == "ok" and "action" not in ma and "next nightly backup" in ma["detail"]
    # the next backup included it: the row is gone
    (h / "backup-status.json").write_text(json.dumps(_st(1)))
    assert [r["key"] for r in bh.status(h, NOW)["checks"]] == ["backups"]


def _host_main_box(tmp_path):
    """James's box: the Wizard runs on the host Hermes (~/.hermes), no hermes-agent app, no alongside.json."""
    b = FakeBox(tmp_path, write_tools(tmp_path / "tools"), "host")
    _w(b.home / "settings.json", json.dumps({"name": "hostbox"}))
    _w(b.home / "backup.json", json.dumps({"schema": 1, "enabled": True, "target": {"kind": "path", "path": "/x"}}))
    hermes_home(b.homedir / ".hermes", wiring=True, messaging=True, marker="hostmain", jobs=[])
    (b.homedir / ".hermes" / "big.bin").write_bytes(b"x" * (3 << 20))
    _package(b.runtipi, "wizardapps", "notes-app", 2, "1.0")
    _install_now(b.runtipi, b.rt, "notes-app:wizardapps")
    launcher = b.base / "hermes-launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    return b, launcher


def test_include_refuses_with_numbers_when_the_disk_is_too_small(tmp_path, monkeypatch):
    b, launcher = _host_main_box(tmp_path)
    monkeypatch.setenv("WIZARD_HERMES_BIN", str(launcher))
    monkeypatch.setenv("HERMES_HOME", str(b.homedir / ".hermes"))
    c = SimpleNamespace(home=b.home, runtipi_root=b.runtipi)
    turned = []
    real_room = lambda c, p: capture.stream_room(b.box(), host_home=p)  # noqa: E731 - the real estimate
    b.free = 1 << 20
    r = bh.include(c, room=real_room, turn_on=lambda c, p: turned.append(p))
    assert not r["ok"] and r["state"] == "no_room" and turned == []
    assert r["need_bytes"] > r["free_bytes"] == 1 << 20
    assert "needs" in r["reason"] and "1.0 MB free" in r["reason"] and "tap Include it again" in r["reason"]
    assert not (b.home / "alongside.json").exists()             # a refusal changes nothing
    b.free = 50 * GiB
    r = bh.include(c, room=real_room, turn_on=lambda c, p: turned.append(p) or {"next": None})
    assert r["ok"] and r["state"] == "on" and "next nightly backup includes your main agent" in r["message"]
    assert turned == [str(b.homedir / ".hermes")]


def test_include_counts_the_host_hermes_in(tmp_path):
    b, _ = _host_main_box(tmp_path)
    b.free = 50 * GiB
    without = capture.stream_room(b.box())
    with_host = capture.stream_room(b.box(), host_home=b.homedir / ".hermes")
    assert with_host["need"] > without["need"]


def test_include_turns_alongside_on_for_real(tmp_path, monkeypatch):
    b, launcher = _host_main_box(tmp_path)
    monkeypatch.setenv("WIZARD_HERMES_BIN", str(launcher))
    monkeypatch.setenv("HERMES_HOME", str(b.homedir / ".hermes"))
    c = cli.Config.__new__(cli.Config)
    c.home, c.runtipi_root = b.home, b.runtipi
    monkeypatch.setattr(cli, "session_mounts_timer", lambda c, st: "skipped")
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: (_ for _ in ()).throw(OSError("no runtipi here")),
                        raising=False)
    b.free = 50 * GiB
    r = bh.include(c, room=lambda c, p: capture.stream_room(b.box(), host_home=p))
    assert r["ok"], r
    st = alongside.load(b.home)
    assert st and st["agent"]["hermes_home"] == str(b.homedir / ".hermes")
    # the backup now counts it as the main agent: no more left-out warning
    assert b.box().main_agent_left_out(b.box().agents()) is None
    assert bh.include(c)["state"] == "on"                        # a second tap: already on


def test_include_without_hermes_says_why(tmp_path, monkeypatch):
    h = _home(tmp_path, _st(1))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "none"))
    r = bh.include(SimpleNamespace(home=h, runtipi_root=tmp_path / "rt"))
    assert not r["ok"] and r["state"] == "no_agent"


# ---- tell the owner once -------------------------------------------------------------------------------------------

def test_told_once_then_again_after_it_cleared(tmp_path):
    h = _home(tmp_path)
    (h / "settings.json").write_text(json.dumps({"network": {"base_url": "https://box.example.ts.net"}}))
    sent = []
    note = lambda home, t, m, link: sent.append((t, m, link)) or True  # noqa: E731
    on = lambda home: True  # noqa: E731
    left = {"ok": True, "warnings": [main_agent_warning("/home/o/.hermes")]}
    assert bh.after_run(h, left, notify=note, on=on) == "told"
    assert sent == [("Your main agent isn't in your backup", sent[0][1],
                     "https://box.example.ts.net/wizard-os-home/?view=backup-health")]
    assert "Home › Resources › Health" in sent[0][1] and "alongside" not in sent[0][1]
    assert bh.after_run(h, left, notify=note, on=on) == "already told" and len(sent) == 1
    assert bh.after_run(h, {"ok": True, "warnings": []}, notify=note, on=on) == "cleared"
    assert bh.after_run(h, left, notify=note, on=on) == "told" and len(sent) == 2


def test_not_delivered_is_tried_after_the_next_run(tmp_path):
    h = _home(tmp_path)
    left = {"warnings": [main_agent_warning("/h/.hermes")]}
    assert bh.after_run(h, left, notify=lambda *a: False, on=lambda h: True) == "not delivered"
    assert bh.after_run(h, left, notify=lambda *a: True, on=lambda h: True) == "told"


def test_switch_off_tells_nobody_and_reads_no_switch_when_all_is_well(tmp_path):
    h = _home(tmp_path)
    left = {"warnings": [main_agent_warning("/h/.hermes")]}
    assert bh.after_run(h, left, notify=lambda *a: pytest.fail("sent"), on=lambda h: False) == "off"
    assert not (h / bh.STATE).exists()
    assert bh.after_run(h, {"ok": True}, notify=lambda *a: pytest.fail("sent"),
                        on=lambda h: pytest.fail("read the switch")) == "nothing"


def test_every_backup_run_goes_through_after_run(tmp_path, monkeypatch):
    b, _ = _host_main_box(tmp_path)
    seen = []
    monkeypatch.setattr(schedule, "_run", lambda box, cfg, now=None: {"ok": True, "warnings": ["w"]})
    monkeypatch.setattr(bh, "after_run", lambda home, st: seen.append((home, st)))
    assert schedule.run(b.box(), scheduled=True)["ok"]
    assert seen == [(b.home, {"ok": True, "warnings": ["w"]})]
    monkeypatch.setattr(bh, "after_run", lambda home, st: 1 / 0)   # never fails the backup
    assert schedule.run(b.box(), scheduled=True)["ok"]


# ---- the route -----------------------------------------------------------------------------------------------------

def _route(tmp_path, monkeypatch, on=True):
    from wam import store_install as si
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@example.com")
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.example.ts.net:8444"})
    monkeypatch.setattr(bh, "switch_on", lambda home=None: on)
    h = _home(tmp_path, _st(11))
    (h / "links.json").write_text(json.dumps({"host": "box.example.ts.net"}))
    c = SimpleNamespace(home=h, runtipi_root=tmp_path / "rt")
    sent = []

    def call(method, body=None, **hdr):
        hd = {"X-Forwarded-For": ".".join(["100", "64", "0", "9"]), "Tailscale-User-Login": "owner@example.com", "Origin": "https://box.example.ts.net:8444",
              "X-Wizard-Store": "1", "Content-Type": "application/json", **hdr}
        raw = json.dumps(body).encode() if body is not None else b""
        hd["Content-Length"] = str(len(raw))
        o = cli.StoreHandler.__new__(cli.StoreHandler)
        o.cfg, o.path, o.client_address = c, "/_backup-health", ("127.0.0.1", 5555)
        o.headers = {k: v for k, v in hd.items() if v is not None}
        o.rfile = io.BytesIO(raw)
        o._json = lambda code, b: sent.append((code, b))
        o._go(method)
        return sent[-1]
    return call, c


def test_route_owner_only_and_switch_gated(tmp_path, monkeypatch):
    call, c = _route(tmp_path, monkeypatch)
    code, b = call("GET")
    assert code == 200 and b["available"] and b["checks"][0]["key"] == "backups"
    assert call("GET", **{"Tailscale-User-Login": "intruder@example.com"})[0] == 403
    assert call("POST", {"action": "include-main-agent"}, Origin="https://evil.example")[0] == 403
    assert call("POST", {"action": "include-main-agent"}, **{"X-Wizard-Store": None})[0] == 403
    assert call("POST", {"action": "other"})[0] == 400
    monkeypatch.setattr(bh, "include", lambda c: {"ok": False, "state": "no_room", "reason": "needs 106 GB free"})
    assert call("POST", {"action": "include-main-agent"}) == (200, {"ok": False, "state": "no_room",
                                                                   "reason": "needs 106 GB free"})
    call, c = _route(tmp_path, monkeypatch, on=False)
    assert call("GET")[0] == 404 and call("POST", {"action": "include-main-agent"})[0] == 404


def test_gateway_allows_the_route():
    from wam import store_install as si
    assert si.gateway_path_ok("/_backup-health") and "_backup-health" in si.GATEWAY_CONTROL
    assert not si.gateway_path_ok("/_backup-health/x")


def test_notify_passes_the_link(tmp_path, monkeypatch):
    from wam import app_updates
    got = []

    class R:
        def read(self):
            return b'{"result": {"content": []}}'

    (tmp_path / "apps.json").write_text(json.dumps({"apps": [{"id": "notifications", "mcp": {"url": "http://n/mcp"}}]}))
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", lambda req, timeout=0: got.append(req) or R())
    assert app_updates._notify(SimpleNamespace(home=tmp_path), "T", "M", required=True, link="https://x/y")
    args = json.loads(got[0].data)["params"]["arguments"]
    assert args["link"] == "https://x/y" and args["title"] == "T"
    app_updates._notify(SimpleNamespace(home=tmp_path), "T", "M")
    assert "link" not in json.loads(got[1].data)["params"]["arguments"]
