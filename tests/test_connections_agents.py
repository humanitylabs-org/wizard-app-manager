"""Connections → Agents and Restore defaults (Miguel, 29 Oct 2026).

- Restore defaults: the plan says what changes in plain words; the reset drops exactly the owner's own app-to-app rows
  (his cuts and the links he added) and reconciles, through the App Manager's API or, on an older App Manager, its file.
- Per-agent access: the owner's switch goes to the Agent Service's owner API for that agent only; the owner listener
  takes it only from the owner's device (never the box itself or an app); `wizard call` refuses an app turned off.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from wam import agent_access, alongside, connections_reset as cr

ROOT = Path(__file__).resolve().parents[1]

# The box as Miguel has it: declared links, one he added (Music → YouTube Music), one he added and then cut
# (Messenger → Money), one declared link he cut (Card Benefits → Monarch) and one declared link whose row is his own
# (Backup → its storage, reconnected by him once).
STATE = {
    "schema": 1, "mode": "declared", "sensitive": [], "seeded": [], "notices": [],
    "grants": [
        {"kind": "app", "from": "money", "to": "wizard-monarch", "reason": "uses", "approved": "install"},
        {"kind": "app", "from": "backup", "to": "slot:backup-storage", "reason": "owner", "approved": "owner"},
        {"kind": "app", "from": "wizard-music", "to": "wizard-youtube-music", "reason": "owner", "approved": "owner"},
        {"kind": "app", "from": "carlos", "to": "resend", "reason": "request", "approved": "request"},
        {"kind": "share", "from": "hermes-agent", "to": "wizard-claude", "share": "claude-login", "approved": "owner"},
    ],
    "denied": [
        {"kind": "app", "from": "messenger", "to": "money", "reason": "owner", "approved": "owner"},
        {"kind": "app", "from": "card-benefits", "to": "wizard-monarch", "reason": "uses", "approved": "owner"},
    ],
    "pending": [{"kind": "app", "from": "wizard-claude", "to": "money", "reason": "request"}],
}
REPORT = {
    "apps": [{"id": i, "name": n} for i, n in [
        ("money", "Money"), ("wizard-monarch", "Monarch"), ("backup", "Backup"), ("wizard-cloudflare", "Cloudflare"),
        ("wizard-music", "Music"), ("wizard-youtube-music", "YouTube Music"), ("messenger", "Messenger"),
        ("card-benefits", "Card Benefits")]],
    "controls": [
        {"from": "backup", "to": "slot:backup-storage", "origin": "declared", "providers": ["wizard-cloudflare"],
         "label": "Backup → Cloudflare"},
        {"from": "messenger", "to": "money", "origin": "owner", "providers": ["money"], "label": "Messenger → Money"},
        {"from": "card-benefits", "to": "wizard-monarch", "origin": "declared", "providers": ["wizard-monarch"],
         "label": "Card Benefits → Monarch"},
        {"from": "wizard-music", "to": "wizard-youtube-music", "origin": "owner", "providers": ["wizard-youtube-music"],
         "label": "Music → YouTube Music"},
    ],
}


def test_restore_plan_says_what_changes_in_plain_words():
    plan = cr.plan(STATE, REPORT)
    assert [p["text"] for p in plan] == [
        "Card Benefits → Monarch will be turned back on.",
        "Your cut between Messenger and Money will be cleared (it changes nothing while apps are kept apart).",
        "Music → YouTube Music, connected with your OK, will be removed.",
    ]
    assert [p["change"] for p in plan] == ["on", "remove", "remove"]
    # a declared link to an app set to Ask me first comes back as a request
    sens = {**STATE, "sensitive": ["wizard-monarch"]}
    assert cr.plan(sens, REPORT)[0]["text"] == "Card Benefits → Monarch will wait for your OK again."
    # the same cut in Open mode keeps them apart, so clearing it lets them talk again
    assert cr.plan(STATE, {**REPORT, "mode": "open"})[1]["text"] == "Messenger and Money will be able to talk again (you cut them apart)."
    # nothing of his own: nothing to change
    assert cr.plan({**STATE, "grants": STATE["grants"][:1], "denied": []}, REPORT) == []


def test_reset_drops_only_the_owners_own_app_rows():
    out = cr.reset_state(json.loads(json.dumps(STATE)))
    assert [(g["from"], g["to"]) for g in out["grants"]] == [
        ("money", "wizard-monarch"), ("carlos", "resend"), ("hermes-agent", "wizard-claude")]
    assert out["denied"] == []
    assert out["pending"] == STATE["pending"] and out["mode"] == "declared" and out["sensitive"] == []


class FakeRT:
    def __init__(self, reset_api=True):
        self.calls, self.reset_api = [], reset_api

    def req(self, method, path, body=None, timeout=60):
        from wam.runtipi import RuntipiError
        self.calls.append((method, path, body))
        if path == cr.RESET_PATH and not self.reset_api:
            raise RuntipiError(f"{method} {path} -> 404: b'not found'")
        return {"mode": "declared", "grants": [], "denied": []}


def _box(tmp_path, monkeypatch):
    from wam import network
    (tmp_path / "state").mkdir()
    f = tmp_path / "state" / "wizard-grants.json"
    f.write_text(json.dumps(STATE))
    os.chmod(f, 0o600)
    monkeypatch.setattr(network, "connection_state", lambda c, rt: json.loads(f.read_text()))
    monkeypatch.setattr(network, "report", lambda c, rt=None: REPORT)

    class C:
        runtipi_root = tmp_path
        home = tmp_path
    return C(), f


def test_restore_goes_through_the_app_managers_owner_api(tmp_path, monkeypatch):
    c, f = _box(tmp_path, monkeypatch)
    rt = FakeRT()
    seen = []
    out = cr.apply(c, rt, lambda: seen.append("reconciled"))
    assert rt.calls == [("POST", cr.RESET_PATH, {})]
    assert seen == ["reconciled"] and len(out["changed"]) == 3
    assert json.loads(f.read_text()) == STATE   # the App Manager changed it, not wam


def test_restore_on_an_older_app_manager_edits_its_file_then_reconciles_the_current_mode(tmp_path, monkeypatch):
    c, f = _box(tmp_path, monkeypatch)
    rt = FakeRT(reset_api=False)
    cr.apply(c, rt)
    doc = json.loads(f.read_text())
    assert doc["denied"] == [] and not [g for g in doc["grants"] if g.get("kind", "app") == "app" and g["approved"] == "owner"]
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert rt.calls[-1] == ("POST", "/apps/wizard/connection-mode", {"mode": "declared"})


def test_restore_changes_nothing_when_the_plan_changed_after_the_owner_saw_it(tmp_path, monkeypatch):
    c, f = _box(tmp_path, monkeypatch)
    rt = FakeRT()
    from wam import network
    seen = cr.plan(network.connection_state(c, rt), network.report(c, rt))
    stale = cr.plan_id(seen[1:])   # what the dialog showed before another tab cut or added a link
    with pytest.raises(cr.PlanChanged):
        cr.apply(c, rt, expect=stale)
    assert rt.calls == [] and json.loads(f.read_text()) == STATE
    cr.apply(c, rt, expect=cr.plan_id(seen))
    assert rt.calls == [("POST", cr.RESET_PATH, {})]


def test_restore_refuses_a_file_it_may_not_write(tmp_path, monkeypatch):
    c, f = _box(tmp_path, monkeypatch)
    monkeypatch.setattr(cr.os, "access", lambda p, m: False)
    with pytest.raises(PermissionError):
        cr.apply(c, FakeRT(reset_api=False))
    assert json.loads(f.read_text()) == STATE


# ---- per-agent access: a fake Agent Service with its owner API (PUT /wizard/v1/apps/<app>/agents) ----------------

class Service:
    def __init__(self):
        self.lists = {"hermes": {"cinequeue", "money", "browser"}, "helper": {"money"}}
        self.puts = []
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/wizard/v1/agents":
                    return self._send(200, {"agents": [
                        {"id": "hermes", "label": "Oscar", "harness": "hermes", "privacy": "standard",
                         "model": {"provider": "host", "model": ""},
                         "permissions": {"wizard_apps": sorted(svc.lists["hermes"] - {"browser"}),
                                         "agent_apps": sorted(svc.lists["hermes"] & {"browser"})}},
                        {"id": "helper", "label": "Helper", "harness": "hermes", "privacy": "standard",
                         "model": {"provider": "anthropic", "model": "claude-opus-5-5"},
                         "permissions": {"wizard_apps": sorted(svc.lists["helper"]), "agent_apps": []}}]})
                if self.path == "/wizard/v1/agent-options":
                    return self._send(200, {"wizard_apps": [{"id": "cinequeue"}, {"id": "money"}],
                                            "agent_apps": [{"id": "browser"}]})
                app = self.path.split("/")[4]
                if app not in ("cinequeue", "money", "browser"):
                    return self._send(404, {"error": "app_not_found"})
                return self._send(200, {"app_id": app, "agents": [
                    {"id": a, "enabled": app in apps} for a, apps in svc.lists.items()]})

            def do_PUT(self):
                app = self.path.split("/")[4]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                svc.puts.append((app, body))
                for a in svc.lists:
                    (svc.lists[a].add if a in body["agents"] else svc.lists[a].discard)(app)
                return self._send(200, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture
def service(monkeypatch, tmp_path):
    s = Service()
    monkeypatch.setattr(agent_access, "service_origin", lambda home: s.origin)
    yield s
    s.srv.shutdown()


def _named(tmp_path):
    hh = tmp_path / "hermes"
    hh.mkdir(exist_ok=True)
    (hh / "config.yaml").write_text("model:\n  default: claude-opus-5-5\n  provider: anthropic\n")
    (tmp_path / "alongside.json").write_text(json.dumps({"mode": "alongside", "agent": {
        "id": "host-hermes", "name": "host Hermes", "hermes_home": str(hh), "origin": "http://127.0.0.1:8642"}}))
    return tmp_path


def test_agents_are_instances_with_name_harness_model_and_what_each_may_use(service, tmp_path):
    got = agent_access.instances(_named(tmp_path))
    oscar, helper = got["agents"]
    assert (oscar["id"], oscar["name"], oscar["harness"], oscar["model"], oscar["provider"], oscar["main"]) == \
        ("hermes", "Oscar", "Hermes", "claude-opus-5-5", "anthropic", True)
    assert oscar["on"] == ["browser", "cinequeue", "money"] and oscar["known"] == ["browser", "cinequeue", "money"]
    assert (helper["name"], helper["on"], helper["model"]) == ("Helper", ["money"], "claude-opus-5-5")


def test_two_owner_changes_at_once_never_undo_each_other(service, tmp_path, monkeypatch):
    # Codex (PR #328): each change reads an app's agent list and writes it back whole; slow the service between the
    # read and the write so two unserialized changes would interleave and the last one would undo the first.
    import time
    real = agent_access._req

    def slow(origin, method, path, body=None, timeout=10):
        out = real(origin, method, path, body, timeout)
        if method == "GET" and path.endswith("/agents") and "/apps/" in path:
            time.sleep(0.3)
        return out
    monkeypatch.setattr(agent_access, "_req", slow)
    runs = [threading.Thread(target=agent_access.set_access, args=(tmp_path, "helper", ["cinequeue"], True)),
            threading.Thread(target=agent_access.set_access, args=(tmp_path, "hermes", ["cinequeue"], False))]
    for t in runs:
        t.start()
    for t in runs:
        t.join()
    assert service.lists["helper"] >= {"cinequeue"} and "cinequeue" not in service.lists["hermes"]


def test_a_switch_changes_only_that_agent_through_the_agent_service(service, tmp_path):
    got = agent_access.set_access(tmp_path, "hermes", ["cinequeue"], False)
    assert service.puts == [("cinequeue", {"agents": []})]
    assert "cinequeue" not in got["agents"][0]["on"]
    # Turn all on for a group: only what is off is changed; the other agent keeps its own access
    service.puts.clear()
    agent_access.set_access(tmp_path, "hermes", ["cinequeue", "money"], True)
    assert service.puts == [("cinequeue", {"agents": ["hermes"]})]
    assert service.lists["helper"] == {"money"}
    agent_access.set_access(tmp_path, "helper", ["money"], False)
    assert service.lists["hermes"] >= {"money"} and service.lists["helper"] == set()


def test_bad_switches_are_refused_before_the_agent_service(service, tmp_path):
    for agent, apps, on in [("../x", ["money"], False), ("hermes", [], True), ("hermes", ["Money!"], True),
                            ("hermes", ["money"], "yes"), ("nobody", ["money"], False)]:
        with pytest.raises(ValueError):
            agent_access.set_access(tmp_path, agent, apps, on)
    assert service.puts == []
    with pytest.raises(agent_access.AccessError) as e:
        agent_access.set_access(tmp_path, "hermes", ["files"], False)   # not something the Agent Service gives agents
    assert "refused" in str(e.value)


def test_no_agent_service_is_said_plainly(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_access, "service_origin", lambda home: "http://127.0.0.1:9")
    with pytest.raises(agent_access.AccessError) as e:
        agent_access.instances(tmp_path)
    assert e.value.status == 503


def test_turn_all_on_is_all_or_nothing(service, tmp_path):
    service.lists["hermes"] -= {"cinequeue", "browser"}
    with pytest.raises(agent_access.AccessError):   # "files" isn't an app the Agent Service gives agents
        agent_access.set_access(tmp_path, "hermes", ["cinequeue", "browser", "files"], True)
    assert service.puts == [] and service.lists["hermes"] == {"money"}
    # a write that fails half way is put back
    real = agent_access._req
    def flaky(origin, method, path, body=None, timeout=10):
        if method == "PUT" and "/browser/" in path and body == {"agents": ["hermes"]}:
            raise agent_access.AccessError("the Agent Service refused: boom")
        return real(origin, method, path, body, timeout)
    agent_access._req = flaky
    try:
        with pytest.raises(agent_access.AccessError):
            agent_access.set_access(tmp_path, "hermes", ["cinequeue", "browser"], True)
    finally:
        agent_access._req = real
    assert service.lists["hermes"] == {"money"}
    assert service.puts[-1] == ("cinequeue", {"agents": []})


# ---- `wizard call`: the host agent's other way to an app honours the same lists ---------------------------------

def _cli(tmp_path, *args, allowed=("money",)):
    home = _named(tmp_path)
    (home / "apps.json").write_text(json.dumps({"schema": 1, "apps": [
        {"id": "cinequeue", "name": "Movies", "status": "running", "mcp": {"url": "http://127.0.0.1:9/mcp"}},
        {"id": "money", "name": "Money", "status": "running", "mcp": {"url": "http://127.0.0.1:9/mcp"}}]}))
    agents = tmp_path / "wizard-os" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents.parent / "settings.json").write_text(json.dumps({"schema": 1, "agent": {"name": "Oscar"}}))
    main = agents / ".main-agent.json"
    if allowed is None:
        main.write_text('{"schema": 1, "wizard_apps": [')   # cut short mid-write
    else:
        main.write_text(json.dumps({"schema": 1, "wizard_apps": list(allowed), "agent_apps": []}))
    env = {**os.environ, "WIZARD_OS_HOME": str(home), "WIZARD_AGENTS_DIR": str(agents), "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, str(ROOT / "wizard-cli" / "wizard"), *args], capture_output=True, text=True,
                          env=env, timeout=30)


def test_wizard_call_refuses_an_app_the_owner_turned_off_in_plain_words(tmp_path):
    r = _cli(tmp_path, "call", "cinequeue.cinequeue_list", "{}", "--json")
    assert r.returncode == 10
    assert json.loads(r.stdout)["error"] == {"code": "not_allowed", "exit": 10, "app": "cinequeue", "message":
                                             "Oscar is not allowed to use Movies. The owner can turn it on in Connections."}
    assert "Oscar is not allowed to use Movies." in _cli(tmp_path, "call", "cinequeue.cinequeue_list", "{}").stderr
    tools = _cli(tmp_path, "tools", "cinequeue", "--json")
    assert tools.returncode == 10
    listed = json.loads(_cli(tmp_path, "apps", "list", "--json").stdout)
    assert {a["id"]: (a["allowed"], a["callable"]) for a in listed["apps"]}["cinequeue"] == (False, False)
    # turned on again (or never chosen): it is only the app's own reachability that decides
    r = _cli(tmp_path, "call", "cinequeue.cinequeue_list", "{}", "--json", allowed=("money", "cinequeue"))
    assert r.returncode == 7   # unreachable: the test app isn't running, but the call was allowed
    # a lists file that can't be read allows nothing: the owner's Off never turns into On
    assert _cli(tmp_path, "call", "money.x", "{}", "--json", allowed=None).returncode == 10



def test_cli_line_counts_like_connections():
    """`wam agents` counts what the Agent Service can give an agent, by group, like the Connections page: an app left
    in the agent's own list that isn't offered any more (or isn't installed) doesn't inflate it ("34 of 31")."""
    from wam import agent_access as aa
    groups = {"money": "app", "files": "app", "transcriber": "agent-app", "wizard-claude": "connector",
              "wizard-monarch": "connector", "hermes-agent": "agent"}
    agent = {"known": ["files", "money", "transcriber", "wizard-claude", "wizard-monarch", "gone"],
             "on": ["files", "money", "transcriber", "wizard-claude", "wizard-monarch", "home-assistant", "wizard-teller"]}
    assert aa.can_use_line(agent, groups) == "can use 2 apps, 1 agent app, 2 connectors"
    agent["on"].remove("money")
    assert aa.can_use_line(agent, groups) == "can use 1 app, 1 agent app, 2 connectors; off: money"
    # without the groups: counted against what it is offered, never more than that
    assert aa.can_use_line(agent, None) == "can use 4 of 6 apps; off: money, gone"
    assert aa.can_use_line({"known": [], "on": ["x"]}, groups) == "nothing to use yet"
