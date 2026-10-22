"""Fresh boxes (Runtipi shell, no alongside.json) get the same Store data + Store Get routes as alongside boxes.

Before: `wam store-data` returned "skipped: not an alongside box", so /wizard-os-home/store-data and /store-install
were 404 on the fresh shell origin and the Store said "Installed status is incomplete".
"""
import json
import subprocess
from types import SimpleNamespace

from wam import cli, links, store_data, store_install as si

HOST = "box.tail1.ts.net"
SHA = "c" * 40
REG = {"apps": [
    {"id": "wizard-ux", "provides": ["shell"], "commit": SHA, "health": "http://127.0.0.1:8796/shell-health",
     "view": {"local": "http://127.0.0.1:8796/wizard-os-home/", "url": f"https://{HOST}:8796/wizard-os-home/"}},
    {"id": "wizard-agent-service", "provides": ["agent-service"], "health": "http://127.0.0.1:8797/wizard/health",
     "mcp": {"url": "http://127.0.0.1:8797/wizard/mcp"}},
    {"id": "files", "provides": ["files"], "view": {"local": "http://127.0.0.1:8794/files/"}},
]}


class FakeServe:
    """`tailscale serve` with a real Web/TCP config: --set-path adds a handler, status returns the JSON."""

    def __init__(self, web=None):
        self.cfg = {"TCP": {}, "Web": {}}
        for port, handlers in (web or {}).items():
            self.cfg["TCP"][str(port)] = {"HTTPS": True}
            self.cfg["Web"][f"{HOST}:{port}"] = {"Handlers": {p: {"Proxy": t} for p, t in handlers.items()}}
        self.calls = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        if args[:3] == ["tailscale", "serve", "--bg"]:
            port = args[3].split("=", 1)[1]
            path = args[4].split("=", 1)[1]
            self.cfg["TCP"].setdefault(port, {"HTTPS": True})
            self.cfg["Web"].setdefault(f"{HOST}:{port}", {"Handlers": {}})["Handlers"][path] = {"Proxy": args[5]}
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def _box(monkeypatch, tmp_path, serve, alongside_state=None):
    monkeypatch.setattr(cli.alongside, "load", lambda home: alongside_state)
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: REG)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(links, "tailnet", lambda: {"state": "Running", "host": HOST})
    monkeypatch.setattr(links, "serve_status", lambda: json.loads(json.dumps(serve.cfg)))
    monkeypatch.setattr(links, "_run", serve.run)
    ensured = {}

    def ensure(reg, apply=True, service=None, **kw):
        ensured.update(reg=reg, service=service)
        return {"state": "ready", "commit": SHA, "port": store_data.PORT}
    monkeypatch.setattr(store_data, "ensure", ensure)
    return SimpleNamespace(home=tmp_path, runtipi=lambda: None), ensured


def test_fresh_box_gets_store_data_and_store_get_on_the_shell_port(monkeypatch, tmp_path):
    serve = FakeServe({8796: {"/": "http://127.0.0.1:8796", "/files/": "http://127.0.0.1:8794/files/"}})
    c, ensured = _box(monkeypatch, tmp_path, serve)
    out = cli.store_data_cmd(c)
    assert out["state"] == "ready" and out["mode"] == "fresh", out
    assert (out["tailnet"], out["get"], out["ports"]) == ("routed", "routed", [8796])
    # The adapter reads the shell's app list from the fresh box's own Agent Service port, not the alongside default.
    assert ensured["service"] == "http://127.0.0.1:8797"
    h = serve.cfg["Web"][f"{HOST}:8796"]["Handlers"]
    assert h["/wizard-os-home/store-data"] == {"Proxy": "http://127.0.0.1:9584/wizard-os-home/store-data"}
    assert h["/wizard-os-home/store-install"] == {"Proxy": "http://127.0.0.1:7791"}
    assert h["/"] == {"Proxy": "http://127.0.0.1:8796"} and h["/files/"] == {"Proxy": "http://127.0.0.1:8794/files/"}
    # Idempotent: a rerun adds nothing.
    n = len(serve.calls)
    assert cli.store_data_cmd(c)["get"] == "routed" and len(serve.calls) == n


def test_the_store_get_gate_is_unchanged_on_a_fresh_box(monkeypatch, tmp_path):
    """The new route makes the shell origin a served origin; nothing else gets in (owner, origin, header checks)."""
    serve = FakeServe({8796: {"/": "http://127.0.0.1:8796"}, 9119: {"/": "http://127.0.0.1:9119"}})
    c, _ = _box(monkeypatch, tmp_path, serve)
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(serve.cfg), ""))
    assert si.served_origins(HOST) == set()          # before: no Store Get route, so no origin can post
    cli.store_data_cmd(c)
    origins = si.served_origins(HOST)
    assert origins == {f"https://{HOST}:8796"}      # only the shell's own origin, never another app's port
    good = {"Tailscale-User-Login": "owner@x.com", "Origin": f"https://{HOST}:8796", "X-Wizard-Store": "1",
            "Content-Type": "application/json"}
    assert si.check_request(good, "owner@x.com", origins) is None
    assert si.check_request({**good, "Origin": f"https://{HOST}:9119"}, "owner@x.com", origins) == "wrong origin"
    assert si.check_request({**good, "Tailscale-User-Login": "guest@x.com"}, "owner@x.com", origins)
    assert si.check_request({k: v for k, v in good.items() if k != "X-Wizard-Store"}, "owner@x.com", origins)


def test_a_different_existing_handler_is_never_replaced(monkeypatch, tmp_path):
    serve = FakeServe({8796: {"/": "http://127.0.0.1:8796", "/wizard-os-home/store-install": "http://127.0.0.1:9999"}})
    c, _ = _box(monkeypatch, tmp_path, serve)
    out = cli.store_data_cmd(c)
    assert out["tailnet"] == "routed" and "differs; left untouched" in out["get"]
    assert serve.cfg["Web"][f"{HOST}:8796"]["Handlers"]["/wizard-os-home/store-install"] == {"Proxy": "http://127.0.0.1:9999"}


def test_owner_shell_aliases_get_the_routes_too(monkeypatch, tmp_path):
    serve = FakeServe({8796: {"/": "http://127.0.0.1:8796"},
                       443: {"/": "http://127.0.0.1:8796"}})
    c, _ = _box(monkeypatch, tmp_path, serve)
    assert cli.store_data_cmd(c)["ports"] == [443, 8796]
    for port in (443, 8796):
        assert "/wizard-os-home/store-install" in serve.cfg["Web"][f"{HOST}:{port}"]["Handlers"]


def test_alongside_still_uses_its_recorded_ports(monkeypatch, tmp_path):
    serve = FakeServe({8806: {"/": "http://127.0.0.1:8806"}})
    c, ensured = _box(monkeypatch, tmp_path, serve, {"mode": "alongside", "shell": {"port": 8806},
                                                     "service": {"port": 8798}})
    out = cli.store_data_cmd(c)
    assert out["mode"] == "alongside" and out["ports"] == [8806] and ensured["service"] == "http://127.0.0.1:8798"


def test_no_shell_is_skipped(monkeypatch, tmp_path):
    serve = FakeServe()
    c, _ = _box(monkeypatch, tmp_path, serve)
    monkeypatch.setattr(cli, "write_registry", lambda c, rt=None: {"apps": [REG["apps"][2]]})
    assert cli.store_data_cmd(c)["state"] == "skipped" and serve.calls == []


def test_fresh_ports_come_from_loopback_urls_only():
    assert store_data.fresh_ports(REG) == (8796, 8797)
    assert store_data.fresh_ports({"apps": [{"id": "wizard-ux", "provides": ["shell"],
                                              "view": {"url": "https://evil.example:8796/"}}]}) == (0, 0)


def test_a_fresh_box_without_node_uses_the_troubleshooters_checked_node(monkeypatch, tmp_path):
    node = tmp_path / ".local/share/wizard-troubleshooter/node/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\necho v22.23.2\n")
    node.chmod(0o755)
    monkeypatch.setattr(store_data.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(store_data.shutil, "which", lambda name: None)
    assert store_data.find_node() == str(node)
