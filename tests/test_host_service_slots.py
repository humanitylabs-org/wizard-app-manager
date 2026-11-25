"""Host services fill system spots (wizard-agent-service#463): a healthy host Wizard Voice Service fills Voice and
Transcription in `wam slots` (on for everyone: the wizard/capability-registry switch was removed)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wam import host_services, slots

BASE = {"apps": [{"id": "wizard-ux", "provides": ["shell"]}, {"id": "wizard-agent-service", "provides": ["agent-service"]},
                 {"id": "files", "provides": ["files"]}]}


@pytest.fixture
def voice(monkeypatch):
    """A real loopback HTTP voice service; `state['health']` is what /health answers (None = 503)."""
    state = {"health": {"status": "ok", "provider": "voxtral", "model": "voxtral-mini", "attested": True}}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(state["health"]).encode() if state["health"] is not None else b"{}"
            self.send_response(200 if state["health"] is not None and self.path == "/health" else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv(host_services.ORIGIN_ENV, f"http://127.0.0.1:{srv.server_address[1]}")
    yield state
    srv.shutdown()


def rows(reg):
    return {r["slot"]: r for r in slots.table(reg)}


def test_no_host_service_entry_leaves_the_spots_empty():
    r = rows(dict(BASE))
    assert r["voice"]["state"] == "empty" and r["transcription"]["state"] == "empty"


def test_the_old_switch_is_gone():
    assert not hasattr(host_services, "SWITCH") and not hasattr(host_services, "switch_on")


def test_healthy_host_voice_service_fills_voice_and_transcription(voice):
    reg = {**BASE, "external": host_services.external_slots(BASE)}
    r = rows(reg)
    for slot in ("voice", "transcription"):
        assert r[slot]["state"] == "filled"
        assert r[slot]["active"] == "wizard-voice-service"
        assert r[slot]["external"] == "host service: host Wizard Voice Service"
    # A filled spot is not reinstalled by bootstrap, and the parking list carries it for the shell.
    assert "transcriber" not in slots.to_install(reg) and "wizard-voice-service" not in slots.to_install(reg)
    parked = {p["slot"]: p for p in slots.merge_into_registry(json.loads(json.dumps(reg)))["parking"]}
    assert parked["voice"]["state"] == "filled" and parked["transcription"]["active"] == "wizard-voice-service"


def test_unhealthy_or_absent_service_fills_nothing(voice):
    voice["health"] = {"status": "starting", "provider": "voxtral"}
    assert host_services.external_slots(BASE) == {}
    voice["health"] = None
    assert host_services.external_slots(BASE) == {}


def test_an_installed_app_keeps_its_own_spot(voice):
    reg = {"apps": [*BASE["apps"], {"id": "transcriber", "provides": ["transcription", "speech-to-text-engine"]}]}
    ext = host_services.external_slots(reg)
    assert set(ext) == {"voice"}
    assert rows({**reg, "external": ext})["transcription"]["active"] == "transcriber"


def test_only_a_loopback_origin_is_probed(monkeypatch):
    monkeypatch.setenv(host_services.ORIGIN_ENV, "http://voice.example:8864")
    assert host_services.voice_origin() == host_services.DEFAULT_VOICE_ORIGIN
    monkeypatch.setenv(host_services.ORIGIN_ENV, "http://127.0.0.1:9999/")
    assert host_services.voice_origin() == "http://127.0.0.1:9999"


def test_alongside_agent_row_is_unchanged():
    reg = {**BASE, "external": {"agent": {"external": True, "id": "host-hermes", "name": "host Hermes", "origin": "x"}}}
    r = rows(reg)["agent"]
    assert r["state"] == "external" and r["external"] == "external: host Hermes" and "host_service" not in r


def test_wam_slots_command_shows_the_host_voice_service(voice, tmp_path, monkeypatch, capsys):
    """The real `wam slots` path: write_registry (apps.json + parking) and the printed table."""
    from types import SimpleNamespace

    from test_wam import _fake_store_app
    from wam import cli

    root = tmp_path / "runtipi"
    _fake_store_app(root)
    installed = [{"app": {"status": "running", "port": 8799}, "info": {"urn": "hello-wizard:wizardapps"}}]
    c = SimpleNamespace(home=tmp_path / "home", runtipi_root=root, host_ip=lambda: "127.0.0.1",
                        runtipi=lambda: SimpleNamespace(installed=lambda: installed))
    monkeypatch.setattr(cli, "Config", lambda: c)
    for up, state in ((False, "empty"), (True, "filled")):
        if not up:
            voice["health"] = None
        else:
            voice["health"] = {"status": "ok", "provider": "voxtral", "model": "voxtral-mini"}
        assert cli.main(["slots"]) in (0, None)
        text = capsys.readouterr().out
        out = {line.split()[0]: line for line in text.splitlines() if line.split()}
        assert state in out["Voice"] and state in out["Transcription"], out
        parked = {p["slot"]: p for p in json.loads((c.home / "apps.json").read_text())["parking"]}
        assert parked["voice"]["state"] == state


def test_install_does_not_add_a_speech_stack_beside_the_host_voice_service(monkeypatch):
    """An app whose `uses` names the transcription spot: the host service filling it counts (Codex P2), so the
    spot's default (transcriber) is not installed beside it; without the host entry it still is."""
    from wam import cli
    calls = []

    class RT:
        def app(self, urn): return {"info": {"id": urn.split(":")[0]}}
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: RT())
    monkeypatch.setattr(cli, "ensure_store", lambda c, rt: None)
    monkeypatch.setattr(cli, "installed_ids", lambda rt: set())
    monkeypatch.setattr(cli.alongside, "external_slots", lambda h: {})
    recs = {"notes-app": {"id": "notes-app", "uses": ["transcription"]}, "transcriber": {"id": "transcriber", "uses": []}}
    monkeypatch.setattr(cli, "resolve_any", lambda c, ref: recs[ref])
    reg = {"apps": [], "external": {"transcription": {"external": True, "host_service": True, "id": "wizard-voice-service"}}}
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: reg)

    def stop(c, app_id, *a, **k):
        calls.append(app_id); raise SystemExit("stop here")
    monkeypatch.setattr(cli, "build_form", stop)
    with pytest.raises(SystemExit):
        cli.install(cli.Config(), "notes-app")
    assert calls == ["notes-app"]
    calls.clear()
    reg["external"] = {}
    with pytest.raises(SystemExit):
        cli.install(cli.Config(), "notes-app")
    assert calls[0] == "transcriber"
