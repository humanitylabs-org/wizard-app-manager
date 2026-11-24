"""Host services fill system spots (wizard-agent-service#463): a healthy host Wizard Voice Service fills Voice and
Transcription in `wam slots` while the owner's wizard/capability-registry switch is on; off, nothing changes."""
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


def test_switch_off_changes_nothing(voice):
    reg = dict(BASE)
    assert host_services.external_slots(reg, on=lambda: False) == {}
    r = rows(reg)
    assert r["voice"]["state"] == "empty" and r["transcription"]["state"] == "empty"


def test_healthy_host_voice_service_fills_voice_and_transcription(voice):
    reg = {**BASE, "external": host_services.external_slots(BASE, on=lambda: True)}
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
    assert host_services.external_slots(BASE, on=lambda: True) == {}
    voice["health"] = None
    assert host_services.external_slots(BASE, on=lambda: True) == {}


def test_an_installed_app_keeps_its_own_spot(voice):
    reg = {"apps": [*BASE["apps"], {"id": "transcriber", "provides": ["transcription", "speech-to-text-engine"]}]}
    ext = host_services.external_slots(reg, on=lambda: True)
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
    for on, state in ((False, "empty"), (True, "filled")):
        monkeypatch.setattr(host_services, "switch_on", lambda home=None, _on=on: _on)
        assert cli.main(["slots"]) in (0, None)
        text = capsys.readouterr().out
        out = {line.split()[0]: line for line in text.splitlines() if line.split()}
        assert state in out["Voice"] and state in out["Transcription"], out
        parked = {p["slot"]: p for p in json.loads((c.home / "apps.json").read_text())["parking"]}
        assert parked["voice"]["state"] == state
