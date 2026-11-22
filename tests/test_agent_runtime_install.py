"""`wam agent-runtime` (wizard-agent-service#582): every fresh box gets the agent runtime helper, on a Unix socket that
only the Agent Service's container mounts, and the Agent Service's override names it.

A synthetic Agent Service repo (a stand-in helper that speaks the same /healthz over --socket) and a systemctl
stand-in that runs the unit's ExecStart. `WAM_AGENT_RUNTIME_SOURCE=<wizard-agent-service checkout>` runs the same
flow against a real checkout (its ops/agent-runtime) instead of the stand-in.
"""
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wam import agent_runtime as ar, browser_boundary as bb
from wam.network import load_yaml

STORE = "wizardapps"
AS = "wizard-agent-service"

STAND_IN = r'''
import argparse, http.server, os, socketserver
ap = argparse.ArgumentParser()
for a in ("--driver", "--wam", "--agents-dir", "--token-file", "--socket", "--home-mount"):
    ap.add_argument(a)
args = ap.parse_args()
token = open(args.token_file).read().strip()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("Authorization") == f"Bearer {token}"
        self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    def get_request(self):
        r, _ = super().get_request(); return r, ("unix", 0)
try: os.unlink(args.socket)
except FileNotFoundError: pass
s = S(args.socket, H); os.chmod(args.socket, 0o666); s.serve_forever()
'''


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def source(tmp_path, monkeypatch):
    real = os.environ.get("WAM_AGENT_RUNTIME_SOURCE")
    if real:
        monkeypatch.setenv("WAM_AGENT_RUNTIME_REPO_URL", f"file://{real}")
        return subprocess.run(["git", "-C", real, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    repo = tmp_path / "as-repo"
    (repo / "ops/agent-runtime").mkdir(parents=True)
    (repo / "ops/agent-runtime/agent_runtime.py").write_text('# ap.add_argument("--socket")\n' + STAND_IN)
    (repo / "server.mjs").write_text("// not checked out\n")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "x", cwd=repo)
    monkeypatch.setenv("WAM_AGENT_RUNTIME_REPO_URL", f"file://{repo}")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


class Systemctl:
    """Runs the unit's ExecStart on start/restart, like systemd would."""

    def __init__(self, units: Path):
        self.units, self.proc, self.calls = units, None, []

    def __call__(self, *a):
        self.calls.append(a)
        if a[0] in ("restart", "start") and (a[0] == "restart" or self.proc is None):
            self.stop()
            text = (self.units / ar.UNIT).read_text()
            line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
            env = dict(os.environ)
            for l in text.splitlines():
                if l.startswith("Environment="):
                    k, v = l[len("Environment="):].split("=", 1)
                    env[k] = v
            self.proc = subprocess.Popen(shlex.split(line[len("ExecStart="):]), env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return subprocess.CompletedProcess(a, 0, "", "")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None


@pytest.fixture
def box(monkeypatch):
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="ar"))   # a Unix socket path must stay under 108 bytes
    home = short / "h"
    wiz = home / ".wizard-os"
    rt = home / "runtipi"
    (rt / "app-data" / STORE / AS / "data/wizard-os/agents").mkdir(parents=True)
    monkeypatch.delenv("WIZARD_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    sc = Systemctl(ar.paths(home, wiz)["units"])
    yield {"home": home, "wiz": wiz, "rt": rt, "sc": sc}
    sc.stop()
    shutil.rmtree(short, ignore_errors=True)


def _install(b, **kw):
    kw.setdefault("reads_unix", lambda store, app: True)
    return ar.install(b["wiz"], b["rt"], [AS], STORE, home=b["home"], systemctl=b["sc"], **kw)


def test_fresh_box_gets_the_helper_on_a_socket_and_the_agent_service_is_wired(box, source):
    res = _install(box)
    assert res["state"] == "ready", res
    assert res["commit"] == source and res["override_changed"] is True
    p = ar.paths(box["home"], box["wiz"])
    # The helper answers on its socket with the token, and on no TCP port.
    assert ar.healthy(p)
    assert stat.S_IMODE(p["sock_dir"].stat().st_mode) == 0o711
    assert stat.S_IMODE(p["token"].parent.stat().st_mode) == 0o700
    assert len(p["token"].read_text().strip()) == 64
    unit = (p["units"] / ar.UNIT).read_text()
    assert ar.MARKER in unit and "--driver runtipi" in unit and f"--socket {p['socket']}" in unit
    assert f"--agents-dir {box['rt']}/app-data/{STORE}/{AS}/data/wizard-os/agents" in unit
    assert "--port" not in unit
    # Only ops/agent-runtime is staged (as it is: the restricted runtime checks its own file list).
    assert (p["current"] / "agent_runtime.py").is_file() and not (p["current"] / "server.mjs").exists()
    assert os.readlink(p["current"]).endswith(source)
    # The Agent Service's override names the socket and token, mounted read-only.
    doc = load_yaml(bb.override_path(box["rt"], STORE, AS).read_text())
    svc = doc["services"][AS]
    assert svc["environment"] == {"WIZARD_AGENT_RUNTIME_URL": "unix:/run/wizard-agent-runtime/runtime.sock",
                                  "WIZARD_AGENT_RUNTIME_TOKEN_FILE": "/run/wizard-agent-runtime-token",
                                  "WIZARD_AGENT_UPSTREAM": "network"}
    assert f"{p['sock_dir']}:/run/wizard-agent-runtime:ro" in svc["volumes"]
    assert f"{p['token']}:/run/wizard-agent-runtime-token:ro" in svc["volumes"]
    assert json.loads(p["status"].read_text())["state"] == "ready"


def test_a_rerun_changes_nothing_and_keeps_the_token(box, source):
    _install(box)
    p = ar.paths(box["home"], box["wiz"])
    token = p["token"].read_text()
    res = _install(box)
    assert res["state"] == "ready" and res["override_changed"] is False
    assert p["token"].read_text() == token
    assert ("restart", ar.UNIT) not in box["sc"].calls[len(box["sc"].calls) - 3:]


def test_a_remade_token_restarts_the_helper_and_recreates_the_agent_service(box, source):
    _install(box)
    p = ar.paths(box["home"], box["wiz"])
    p["token"].write_text("broken\n")   # a damaged token: the rerun makes a new file (a new inode)
    n = len(box["sc"].calls)
    res = _install(box)
    assert res["state"] == "ready" and res["token_replaced"] is True
    # the running helper keeps its old token in memory, so it is restarted ...
    assert ("restart", ar.UNIT) in box["sc"].calls[n:]
    assert ar.healthy(p)
    # ... and the Agent Service's bind mount still holds the old file, so the caller recreates it
    assert res["override_changed"] is True
    assert _install(box)["token_replaced"] is False


def test_the_command_recreates_the_agent_service_when_the_token_was_remade(monkeypatch):
    from types import SimpleNamespace
    from wam import cli
    calls = []
    monkeypatch.setattr(cli, "_alongside_apps", lambda c, rt: {AS: "service"})
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(ar, "install", lambda *a, **k: {"state": "ready", "app": AS, "override_changed": False,
                                                       "token_replaced": True})
    monkeypatch.setattr(cli, "recreate_apps", lambda c, rt, ids: calls.append(ids) or {AS: "recreated"})
    c = SimpleNamespace(home=Path("/nonexistent"), runtipi_root=Path("/nonexistent"), runtipi=lambda: object())
    assert cli.agent_runtime_cmd(c, SimpleNamespace(dry_run=False, no_restart=False, json=True)) == 0
    assert calls == [[AS]]


def test_other_override_blocks_are_kept(box, source):
    from wam import private_first as pf
    pf.set_box(box["wiz"], box["rt"], STORE, [AS], "1")
    _install(box)
    env = load_yaml(bb.override_path(box["rt"], STORE, AS).read_text())["services"][AS]["environment"]
    assert env["WIZARD_PRIVATE_FIRST"] == "1" and env["WIZARD_AGENT_UPSTREAM"] == "network"
    # A reinstall of the Agent Service app keeps the block (sync_app on install).
    bb.override_path(box["rt"], STORE, AS).unlink()
    assert ar.sync_app(box["home"], box["rt"], STORE, AS, wizard_home=box["wiz"]) is True
    assert "WIZARD_AGENT_RUNTIME_URL" in bb.override_path(box["rt"], STORE, AS).read_text()


def test_hand_installed_helper_and_alongside_boxes_are_left_alone(box, source):
    p = ar.paths(box["home"], box["wiz"])
    p["units"].mkdir(parents=True)
    (p["units"] / ar.UNIT).write_text("[Service]\nExecStart=/usr/bin/python3 by-hand\n")
    assert _install(box)["state"] == "kept"
    assert (p["units"] / ar.UNIT).read_text().endswith("by-hand\n")
    assert not bb.override_path(box["rt"], STORE, AS).exists()
    (p["units"] / ar.UNIT).unlink()
    assert _install(box, alongside_box=True)["state"] == "skipped"
    assert not bb.override_path(box["rt"], STORE, AS).exists()


def test_skips_without_one_agent_service_or_when_switched_off(box, source, monkeypatch):
    assert ar.install(box["wiz"], box["rt"], [], STORE, home=box["home"], systemctl=box["sc"], reads_unix=lambda *a: True)["state"] == "skipped"
    monkeypatch.setenv("WIZARD_AGENT_RUNTIME", "0")
    assert _install(box)["state"] == "skipped"
    assert not box["sc"].calls


def test_an_older_release_without_the_socket_listener_is_skipped(box, tmp_path, monkeypatch):
    repo = tmp_path / "old"
    (repo / "ops/agent-runtime").mkdir(parents=True)
    (repo / "ops/agent-runtime/agent_runtime.py").write_text("print('no socket here')\n")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "x", cwd=repo)
    monkeypatch.setenv("WAM_AGENT_RUNTIME_REPO_URL", f"file://{repo}")
    res = _install(box)
    assert res["state"] == "skipped" and "socket" in res["reason"]
    assert not bb.override_path(box["rt"], STORE, AS).exists()


def test_a_helper_that_never_answers_fails_and_writes_no_override(box, source, monkeypatch):
    box["sc"].__class__ = type("Dead", (Systemctl,), {"__call__": lambda self, *a: subprocess.CompletedProcess(a, 0, "", "")})
    res = _install(box, wait_s=0.5)
    assert res["state"] == "failed" and "did not answer" in res["reason"]
    assert not bb.override_path(box["rt"], STORE, AS).exists()


def test_a_release_pin_is_fetched_exactly(box, source, monkeypatch):
    from wam import release_pins
    pin = {"components": {AS: {"repo": "humanitylabs-org/wizard-agent-service", "commit": source}}}
    monkeypatch.setattr(release_pins, "load", lambda path=None: pin)
    assert ar.pinned_source() == ("humanitylabs-org/wizard-agent-service", source, True)
    res = _install(box)
    assert res["state"] == "ready" and res["pinned"] is True and res["commit"] == source
    pin["components"][AS] = {"repo": "x/y", "commit": "short"}
    assert _install(box)["state"] == "failed"


def test_a_socket_path_too_long_for_unix_sockets_is_refused(tmp_path, source, monkeypatch):
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    home = tmp_path / ("x" * 90)
    res = ar.install(home / ".wizard-os", home / "runtipi", [AS], STORE, home=home, systemctl=lambda *a: None,
                     reads_unix=lambda *a: True)
    assert res["state"] == "failed" and "too long" in res["reason"]


def test_an_agent_service_without_the_unix_client_is_never_wired(box, source):
    """A fresh box whose Agent Service image predates wizard-agent-service#593 would crash-loop on a unix: URL."""
    res = _install(box, reads_unix=lambda store, app: False)
    assert res["state"] == "skipped" and "older than" in res["reason"]
    over = ar._bb.override_path(box["rt"], STORE, AS)
    assert not over.exists() or ar.MARK not in over.read_text()
    assert not (box["home"] / ".config/systemd/user" / ar.UNIT).exists()
    assert _install(box, reads_unix=lambda store, app: None)["state"] == "skipped"


def test_a_block_is_taken_back_when_the_agent_service_can_no_longer_read_it(box, source):
    assert _install(box)["state"] == "ready"
    over = ar._bb.override_path(box["rt"], STORE, AS)
    assert ar.MARK in over.read_text()
    res = _install(box, reads_unix=lambda store, app: False)
    assert res["state"] == "skipped" and res["override_changed"] is True
    assert not over.exists() or ar.MARK not in over.read_text()
    assert ar.sync_app(None, box["rt"], STORE, AS, wizard_home=box["wiz"]) is False   # a reinstall doesn't bring it back
    assert _install(box)["state"] == "ready" and ar.MARK in over.read_text()


def test_the_image_check_reads_the_agent_service_code_from_its_container(tmp_path):
    import io, tarfile
    def tar(body):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w") as t:
            info = tarfile.TarInfo("agents.mjs"); info.size = len(body); t.addfile(info, io.BytesIO(body))
        return b.getvalue()
    for body, want in ((b"export function unixSocketFetch(p) {}", True), (b"export function createRuntimeClient() {}", False)):
        f = tmp_path / "out.tar"; f.write_bytes(tar(body))
        fake = tmp_path / "docker"; fake.write_text(f"#!/bin/sh\n[ \"$1 $2\" = \"cp {AS}_{STORE}-{AS}-1:/app/server/agents.mjs\" ] || exit 9\ncat {f}\n")
        fake.chmod(0o755)
        assert ar.service_reads_unix(STORE, AS, docker=[str(fake)]) is want
    fake.write_text("#!/bin/sh\necho 'No such container' >&2; exit 1\n")
    assert ar.service_reads_unix(STORE, AS, docker=[str(fake)]) is None


def test_a_box_whose_agent_service_writes_as_another_uid_is_not_wired(box, source, monkeypatch):
    """Owner not uid 1000 (or rootless Docker): the helper could never read the agent folders, so say so."""
    agents = ar.agents_dir(box["rt"], STORE, AS)
    agents.mkdir(parents=True, exist_ok=True)
    (agents / ".main-agent.json").write_text("{}")
    assert ar.foreign_writer(agents) is None
    assert _install(box)["state"] == "ready"
    real = os.getuid()
    monkeypatch.setattr(ar.os, "getuid", lambda: real + 1)
    res = _install(box)
    assert res["state"] == "failed" and f"uid {real}" in res["reason"] and res["override_changed"] is True
    over = ar._bb.override_path(box["rt"], STORE, AS)
    assert not over.exists() or ar.MARK not in over.read_text()
