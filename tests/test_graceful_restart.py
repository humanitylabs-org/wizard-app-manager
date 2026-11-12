"""Graceful Agent Service restarts through the service's restart gate (#642, switch wizard/graceful-restart)."""
import json

import pytest

from wam import cli
from wam import graceful_restart as gr

ORIGIN = "http://127.0.0.1:18080"
URL = ORIGIN + gr.ROUTE
URN = "wizard-agent-service:wizardapps"


class FakeService:
    """The Agent Service restart gate: answers POST/GET/DELETE from a scripted list of GET phases."""

    def __init__(self, phases, post_code=200, health=(200,)):
        self.phases = list(phases)
        self.post_code = post_code
        self.health = health if callable(health) else list(health)
        self.calls = []

    def __call__(self, method, url, body=None, timeout=10):
        self.calls.append((method, url, body))
        if url.endswith("/wizard/health"):
            if callable(self.health):
                return self.health(), {}
            return (self.health.pop(0) if len(self.health) > 1 else self.health[0]), {}
        if method == "POST":
            return self.post_code, {"phase": "waiting", "turns": [{"agent_id": "research", "running_s": 700}]}
        if method == "DELETE":
            return 200, {"phase": "idle"}
        phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        return 200, {"phase": phase, "turns": [{"agent_id": "research", "running_s": 700}], "active": 1}

    def methods(self):
        return [m for m, u, _ in self.calls if u == URL]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_gate(tmp_path, svc, **kw):
    clock = Clock()
    told = []
    kw.setdefault("ask", lambda st: None)
    return gr.gate(None, "update", home=tmp_path, origin=ORIGIN, req=svc, sleep=clock.sleep, clock=clock,
                   tell=lambda c, doc: told.append(doc), **kw), told


def test_waits_until_ready_then_restarts_without_calling_it_off(tmp_path):
    svc = FakeService(["waiting", "waiting", "ready"])
    res, told = run_gate(tmp_path, svc)
    assert res == {"state": "ready", "origin": ORIGIN}
    assert svc.methods() == ["POST", "GET", "GET", "GET"]
    assert svc.calls[0][2] == {"reason": "update", "wait_s": gr.WAIT_S}
    assert told == [] and not (tmp_path / gr.STATE).exists()


def test_active_unknown_is_never_ready(tmp_path):
    answers = iter([(200, {"phase": "ready", "active_unknown": True}), (200, {"phase": "ready"})])
    calls = []

    def svc(method, url, body=None, timeout=10):
        calls.append(method)
        return (200, {"phase": "waiting"}) if method == "POST" else next(answers)
    res, _ = run_gate(tmp_path, svc)
    assert res["state"] == "ready" and calls == ["POST", "GET", "GET"]


@pytest.mark.parametrize("code,state", [(404, "unsupported"), (0, "unreachable")])
def test_a_service_without_the_gate_restarts_as_before(tmp_path, code, state):
    svc = FakeService(["ready"], post_code=code)
    res, _ = run_gate(tmp_path, svc)
    assert res["state"] == state and svc.methods() == ["POST"] * (3 if code == 0 else 1)


def test_a_lost_poll_after_the_hold_is_retried_not_read_as_nothing_runs(tmp_path):
    answers = iter([(0, {}), (500, {}), (200, {"phase": "waiting"}), (200, {"phase": "ready"})])
    calls = []

    def svc(method, url, body=None, timeout=10):
        calls.append(method)
        return (200, {"phase": "waiting"}) if method == "POST" else next(answers)
    res, _ = run_gate(tmp_path, svc)
    assert res["state"] == "ready" and calls == ["POST", "GET", "GET", "GET", "GET"]


def test_a_service_silent_after_the_hold_calls_the_restart_off(tmp_path):
    calls = []

    def svc(method, url, body=None, timeout=10):
        calls.append(method)
        return (200, {"phase": "waiting"}) if method == "POST" and not calls.count("GET") else (0, {})
    with pytest.raises(gr.CalledOff, match="stopped answering"):
        run_gate(tmp_path, svc)
    assert calls[-1] == "DELETE" and calls.count("GET") >= gr.LOST_SECONDS // gr.POLL_S


def test_the_default_ask_never_blocks(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not wait for a terminal answer"))
    svc = FakeService(["overdue"])
    clock = Clock()
    with pytest.raises(gr.CalledOff):
        gr.gate(None, "update", home=tmp_path, origin=ORIGIN, req=svc, sleep=clock.sleep, clock=clock,
                tell=lambda c, d: None, ask_seconds=30)
    assert "wam agent-restart" in capsys.readouterr().err
    assert svc.methods().count("GET") >= 10    # kept polling (renewing the hold) while waiting for an answer


def test_overdue_asks_the_owner_and_wait_longer_posts_a_new_limit(tmp_path):
    svc = FakeService(["overdue", "ready"])
    seen = []

    def owner_answers_later(st):
        seen.append(json.loads((tmp_path / gr.STATE).read_text()))
        gr.choose("wait", 15, home=tmp_path)   # e.g. `wam agent-restart wait --minutes 15` from another shell
        return None
    res, told = run_gate(tmp_path, svc, ask=owner_answers_later)
    assert res["state"] == "ready"
    assert len(told) == 1 and told[0]["turns"][0]["agent_id"] == "research"
    assert seen[0]["phase"] == "overdue" and seen[0]["reason"] == "update"
    posts = [b for m, u, b in svc.calls if m == "POST"]
    assert posts[-1] == {"reason": "update", "wait_s": 900}
    assert "DELETE" not in svc.methods() and not (tmp_path / gr.STATE).exists()


def test_overdue_restart_now(tmp_path):
    svc = FakeService(["overdue"])
    res, _ = run_gate(tmp_path, svc, ask=lambda st: {"action": "now"})
    assert res["state"] == "now" and "DELETE" not in svc.methods()


def test_overdue_cancel_calls_the_restart_off(tmp_path):
    svc = FakeService(["overdue"])
    with pytest.raises(gr.CalledOff):
        run_gate(tmp_path, svc, ask=lambda st: {"action": "cancel"})
    assert svc.methods()[-1] == "DELETE" and not (tmp_path / gr.STATE).exists()


def test_no_answer_calls_it_off_rather_than_cutting_a_turn(tmp_path):
    svc = FakeService(["overdue"])
    with pytest.raises(gr.CalledOff, match="nobody chose"):
        run_gate(tmp_path, svc, ask_seconds=60)
    assert svc.methods()[-1] == "DELETE"


def test_choose_needs_a_waiting_restart_and_a_known_action(tmp_path):
    with pytest.raises(ValueError, match="no Agent Service restart"):
        gr.choose("now", home=tmp_path)
    (tmp_path / gr.STATE).write_text(json.dumps({"phase": "overdue"}))
    with pytest.raises(ValueError):
        gr.choose("later", home=tmp_path)
    assert gr.choose("wait", 9999, home=tmp_path)["choice"]["minutes"] == 240


def kw(svc, clock=None):
    clock = clock or Clock()
    return dict(origin=ORIGIN, req=svc, sleep=clock.sleep, clock=clock, ask=lambda st: None, tell=lambda c, d: None)


def test_restart_called_off_never_restarts(tmp_path):
    svc = FakeService(["overdue"])
    ran = []
    res = gr.restart(None, URN, lambda: ran.append(1) or {}, home=tmp_path, **kw(svc), ask_seconds=0)
    assert ran == [] and res["error"].startswith("not restarted") and res["graceful"] == "called_off"


def test_restart_runs_after_ready_and_checks_health(tmp_path):
    svc = FakeService(["ready"], health=(200,))
    res = gr.restart(None, URN, lambda: {"status": "running"}, home=tmp_path, **kw(svc))
    assert res == {"status": "running", "graceful": "ready"}
    svc = FakeService(["ready"], health=(503,))
    res = gr.restart(None, URN, lambda: {"status": "running"}, home=tmp_path, **kw(svc))
    assert "health check" in res["error"]


def test_restart_that_raises_releases_the_hold(tmp_path):
    svc = FakeService(["ready"])

    def boom():
        raise SystemExit("runtipi down")
    with pytest.raises(SystemExit):
        gr.restart(None, URN, boom, home=tmp_path, **kw(svc))
    assert svc.methods()[-1] == "DELETE"


class FakeRt:
    def __init__(self, version=5, status="running"):
        self.version, self.status = version, status

    def app(self, urn):
        return {"app": {"status": self.status, "version": self.version}}


def test_update_failing_health_rolls_back_and_checks_again(tmp_path):
    rolled = []
    svc = FakeService(["ready"], health=lambda: 200 if rolled else 503)

    def rollback(c, rt, urn, started_ms, before):
        rolled.append(before)
        return {"rolled_back": True, "status": "running", "version": 5}
    clock = Clock()
    res = gr.update(None, URN, lambda: {"status": "running", "version": 6}, home=tmp_path, rt=FakeRt(),
                    rollback=rollback, **kw(svc, clock))
    assert rolled == [{"status": "running", "from": 5}]
    assert "rolled back to version 5" in res["error"] and res["rollback"]["healthy"] is True
    assert res["graceful"] == "ready"


def test_update_healthy_keeps_the_new_version(tmp_path):
    svc = FakeService(["waiting", "ready"])
    res = gr.update(None, URN, lambda: {"status": "running", "version": 6}, home=tmp_path, rt=FakeRt(),
                    rollback=lambda *a: pytest.fail("no rollback"), **kw(svc))
    assert "error" not in res and res["graceful"] == "ready"


def test_update_called_off_changes_nothing(tmp_path):
    svc = FakeService(["overdue"])
    with pytest.raises(SystemExit, match="not updated"):
        gr.update(None, URN, lambda: pytest.fail("must not update"), home=tmp_path, rt=FakeRt(),
                  **kw(svc), ask_seconds=0)


def test_update_of_a_stopped_service_skips_gate_and_health(tmp_path):
    svc = FakeService(["overdue"])
    res = gr.update(None, URN, lambda: {"status": "stopped", "version": 6}, home=tmp_path,
                    rt=FakeRt(status="stopped"), **kw(svc))
    assert svc.calls == [] and res["graceful"] == "not_running"


# ---------------------------------------------------------------- wiring in cli (switch off = unchanged)

class Cfg:
    def __init__(self, home):
        self.home = home


def test_switch_off_restarts_straight_away(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "switch_on", lambda home=None: False)
    monkeypatch.setattr(gr, "gate", lambda *a, **k: pytest.fail("gate must not run with the switch off"))
    plain = []
    monkeypatch.setattr(cli, "_app_lifecycle", lambda c, verb, app_id, urn, t=300: plain.append((verb, urn)) or {})
    monkeypatch.setattr(cli, "_app_update", lambda *a, **k: plain.append(("update",)) or {})
    cli.app_lifecycle(Cfg(tmp_path), "restart", "wizard-agent-service")
    cli.app_update(Cfg(tmp_path), "wizard-agent-service")
    assert plain == [("restart", URN), ("update",)]


def test_switch_on_routes_only_the_agent_service(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "switch_on", lambda home=None: True)
    through = []
    monkeypatch.setattr(gr, "restart", lambda c, urn, fn, **k: through.append(urn) or fn())
    monkeypatch.setattr(gr, "update", lambda c, urn, fn, **k: through.append(("update", urn)) or fn())
    monkeypatch.setattr(cli, "_app_lifecycle", lambda c, verb, app_id, urn, t=300: {"urn": urn})
    monkeypatch.setattr(cli, "_app_update", lambda *a, **k: {})
    cli.app_lifecycle(Cfg(tmp_path), "restart", "wizard-agent-service")
    cli.app_lifecycle(Cfg(tmp_path), "restart", "wizard-ux")
    cli.app_lifecycle(Cfg(tmp_path), "stop", "wizard-agent-service")
    cli.app_update(Cfg(tmp_path), "wizard-agent-service")
    cli.app_update(Cfg(tmp_path), "wizard-ux")
    assert through == [URN, ("update", URN)]


def test_cli_agent_restart_answers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    monkeypatch.setattr(gr, "switch_on", lambda home=None: False)
    assert cli.main(["agent-restart", "now"]) == 1
    (tmp_path / gr.STATE).write_text(json.dumps({"phase": "overdue"}))
    assert cli.main(["agent-restart", "wait", "--minutes", "5"]) == 0
    assert json.loads((tmp_path / gr.STATE).read_text())["choice"]["minutes"] == 5
    capsys.readouterr()
    assert cli.main(["agent-restart"]) == 0
    assert json.loads(capsys.readouterr().out)["restart"]["choice"]["action"] == "wait"


def test_owner_api_answers_a_waiting_restart_with_the_token_only(tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    monkeypatch.setattr(gr, "switch_on", lambda home=None: True)
    c = cli.Config()
    cli.Handler.cfg, cli.Handler.token = c, c.api_token()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cli.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, headers, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/agent-restart",
                                     json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    auth = {"Authorization": f"Bearer {c.api_token()}"}
    try:
        assert call("GET", {})[0] == 401 and call("POST", {}, {"action": "now"})[0] == 401
        assert call("GET", auth) == (200, {"switch": gr.SWITCH, "on": True, "restart": None})
        assert call("POST", auth, {"action": "now"})[0] == 409            # nothing is waiting
        (c.home / gr.STATE).write_text(json.dumps({"phase": "overdue"}))
        assert call("POST", auth, {"action": "soon"})[0] == 400
        code, doc = call("POST", auth, {"action": "cancel"})
        assert code == 200 and doc["choice"]["action"] == "cancel"
    finally:
        srv.shutdown()


def test_runtipi_passthrough_cannot_restart_the_service_behind_the_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path))
    monkeypatch.setattr(gr, "switch_on", lambda home=None: True)
    passed, routed = [], []
    monkeypatch.setattr(cli, "runtipi_cli", lambda root, argv: passed.append(argv) or 0)
    monkeypatch.setattr(cli, "app_update", lambda c, app, backup=True, allow_host_access=False: routed.append(
        (app, backup)) or {"status": "running"})
    assert cli.main(["app", "update", "--no-backup", "wizard-agent-service"]) == 0
    assert routed == [("wizard-agent-service", False)]
    assert cli.main(["app", "update", "--all"]) == 1
    assert cli.main(["app", "update", "--no-backup", "wizard-ux", "wizard-agent-service"]) == 1
    assert "on its own" in capsys.readouterr().err
    assert cli.main(["app", "update", "--no-backup", "wizard-ux"]) == 0
    assert passed == [["app", "update", "--no-backup", "wizard-ux"]]
    monkeypatch.setattr(gr, "switch_on", lambda home=None: False)
    assert cli.main(["app", "update", "--all"]) == 0 and passed[-1] == ["app", "update", "--all"]
