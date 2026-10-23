"""wam troubleshooter: every box gets the native (host) Troubleshooter, pinned, wired to THIS box, never on a live box.

Everything runs in a fake HOME with a local git fixture standing in for the public repo; systemctl, tailscale and the
service's ping are stubbed, so no unit, route or service on the machine running the tests is touched.
"""
import json
import subprocess
from pathlib import Path

import pytest

from wam import troubleshooter as T

REPO = Path(__file__).resolve().parents[1]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def fixture_repo(tmp_path):
    src = tmp_path / "ts-src"
    (src / "bin").mkdir(parents=True)
    (src / "server.mjs").write_text("// v1\n")
    (src / "bin/guardian.mjs").write_text("// g\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run([*GIT, "-C", str(src), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(src), "commit", "-qm", "v1"], check=True)
    v1 = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (src / "server.mjs").write_text("// v2\n")
    subprocess.run([*GIT, "-C", str(src), "commit", "-qam", "v2"], check=True)
    v2 = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(src), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
    return src, v1, v2


def box(tmp_path, mode="fresh", shell_port=8444, agent_port=8798):
    home = tmp_path / "home"
    wh = home / ".wizard-os"
    wh.mkdir(parents=True)
    apps = [{"id": "wizard-ux", "category": "interface", "health": f"http://127.0.0.1:{shell_port}/shell-health",
             "view": {"local": f"http://127.0.0.1:{shell_port}/wizard-os-home/"}},
            {"id": "wizard-agent-service", "category": "system", "health": f"http://127.0.0.1:{agent_port}/wizard/health"}]
    (wh / "apps.json").write_text(json.dumps({"apps": apps}))
    if mode == "alongside":
        (wh / "alongside.json").write_text(json.dumps({
            "mode": "alongside", "agent": {"origin": "http://127.0.0.1:8642", "hermes_home": str(home / ".hermes")},
            "shell": {"port": 8796}, "service": {"port": 8798}}))
    return home, wh


SERVE = {"TCP": {"8444": {"HTTPS": True}, "8806": {"HTTPS": True}},
         "Web": {"box.tail.ts.net:8444": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8444"}}},
                 "box.tail.ts.net:8806": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8806"},
                                                       "/wizard-os-home/store-install": {"Proxy": "http://127.0.0.1:7791"}}}}}


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """systemctl / tailscale / ping stand-ins. Serve state is a dict the fake `tailscale serve` mutates."""
    calls, serve = [], json.loads(json.dumps(SERVE))
    real_run = T._run
    tagged = {"on": False}

    def fake_run(args, timeout=120, **kw):
        if args[0] == "systemctl":
            calls.append(" ".join(args))
            return subprocess.CompletedProcess(args, 0 if args[2] != "cat" else 1, "", "")
        return real_run(args, timeout, **kw)

    def fake_ts(args, timeout=30):
        calls.append(" ".join(args))
        if args[1:3] == ["status", "--json"]:
            me = {"DNSName": "box.tail.ts.net.", "UserID": 7, "Tags": ["tag:server"] if tagged["on"] else None}
            out = {"BackendState": "Running", "Self": me, "User": {"7": {"LoginName": "owner@example.com"}}}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        if args[1:4] == ["serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(serve), "")
        if args[1] == "serve":
            port = next(a.split("=")[1] for a in args if a.startswith("--https="))
            path = next(a.split("=")[1] for a in args if a.startswith("--set-path="))
            serve["Web"].setdefault(f"box.tail.ts.net:{port}", {"Handlers": {}})["Handlers"][path] = {"Proxy": args[-1]}
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    from wam import links
    monkeypatch.setattr(T, "_run", fake_run)
    monkeypatch.setattr(links, "_run", fake_ts)
    monkeypatch.setattr(links.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(T.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(T, "ping", lambda timeout_s=30: True)
    fake_node = tmp_path / "fake-node"
    fake_node.write_text("#!/bin/sh\nexit 0\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_NODE", str(fake_node))
    monkeypatch.delenv("WIZARD_RELEASE_PINS", raising=False)
    monkeypatch.delenv("WIZARD_TROUBLESHOOTER", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return {"calls": calls, "serve": serve, "tagged": tagged}


def pins(tmp_path, repo="humanitylabs-org/wizard-troubleshooter", commit=None):
    rel = json.loads((REPO / "releases" / ((REPO / "releases/stable").read_text().strip() + ".json")).read_text())
    if commit:
        rel["components"]["wizard-troubleshooter"] = {"repo": repo, "commit": commit, "branch": "main"}
    f = tmp_path / "pins.json"
    f.write_text(json.dumps(rel))
    return f


def envfile(home):
    return dict(l.split("=", 1) for l in (home / ".local/share/wizard-troubleshooter/host.env").read_text().splitlines())


def test_fresh_box_gets_the_pinned_native_service_wired_to_this_box(tmp_path, stubs, monkeypatch):
    src, v1, v2 = fixture_repo(tmp_path)
    home, wh = box(tmp_path)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_REPO_URL", f"file://{src}")
    monkeypatch.setenv("WIZARD_RELEASE_PINS", str(pins(tmp_path, commit=v1)))
    res = T.install(wh, home / "runtipi", home)
    assert res["state"] == "ready", res
    assert res["commit"] == v1 and res["pinned"], "the release's commit, not main"
    assert (home / ".local/share/wizard-troubleshooter/app/server.mjs").read_text() == "// v1\n"
    env = envfile(home)
    # Owner gate: the login this box is signed in as, derived at install time (never a fixed address).
    assert env["TROUBLESHOOT_OWNERS"] == "owner@example.com"
    assert env["TROUBLESHOOT_FRONT"] == "tailscale-serve"
    assert env["GUARDIAN_ACT"] == "0", "guardian is report-only by default"
    # Fresh layout: Hermes is the hermes-agent app (no host port), restarted as its container.
    assert env["HERMES_PORT"] == "0" and env["HERMES_UNIT"] == ""
    assert env["HERMES_CONTAINER"] == "hermes-agent_wizardapps-hermes-agent-engine-1"
    assert env["SHELL_PORT"] == "8444" and env["AGENT_PORT"] == "8798"
    assert env["CHATGPT_AUTH_FILE"].endswith("app-data/wizardapps/hermes-agent/data/auth.json")
    # Both owner shell origins are trusted and routed (8806 is a shell alias with the store-install route).
    assert env["WIZARD_PRIVATE_ORIGINS"] == "https://box.tail.ts.net:8444,https://box.tail.ts.net:8806"
    for port in ("8444", "8806"):
        h = stubs["serve"]["Web"][f"box.tail.ts.net:{port}"]["Handlers"]
        assert h["/troubleshoot"] == {"Proxy": "http://127.0.0.1:9691/troubleshoot"}
    units = home / ".config/systemd/user"
    svc = (units / "wizard-troubleshooter.service").read_text()
    assert T.MARKER in svc and "EnvironmentFile=" in svc and "/server.mjs" in svc
    assert "systemctl --user enable wizard-troubleshooter.service wizard-guardian.timer" in stubs["calls"]
    assert "systemctl --user restart wizard-troubleshooter.service" in stubs["calls"]
    assert json.loads((wh / "troubleshooter.json").read_text())["state"] == "ready"
    written = ((home / ".local/share/wizard-troubleshooter/host.env").read_text() + svc).replace(str(tmp_path), "<sandbox>")
    for k in ("miguel", "askmiguel", "oscar", "tail6adf1a"):
        assert k not in written, f"box-specific {k!r} leaked into generated files" 


def test_rerun_moves_to_the_new_pin_and_refuses_a_commit_that_is_not_there(tmp_path, stubs, monkeypatch):
    src, v1, v2 = fixture_repo(tmp_path)
    home, wh = box(tmp_path)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_REPO_URL", f"file://{src}")
    monkeypatch.setenv("WIZARD_RELEASE_PINS", str(pins(tmp_path, commit=v1)))
    assert T.install(wh, home / "runtipi", home)["commit"] == v1
    monkeypatch.setenv("WIZARD_RELEASE_PINS", str(pins(tmp_path, commit=v2)))
    assert T.install(wh, home / "runtipi", home)["commit"] == v2
    monkeypatch.setenv("WIZARD_RELEASE_PINS", str(pins(tmp_path, commit="0" * 40)))
    res = T.install(wh, home / "runtipi", home)
    assert res["state"] == "failed" and "could not fetch" in res["reason"]


def test_unpinned_box_follows_main(tmp_path, stubs, monkeypatch):
    src, v1, v2 = fixture_repo(tmp_path)
    home, wh = box(tmp_path)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_REPO_URL", f"file://{src}")
    res = T.install(wh, home / "runtipi", home)
    assert res["state"] == "ready" and res["commit"] == v2 and res["pinned"] is False


def test_alongside_box_uses_host_hermes_unit_and_its_auth_store(tmp_path, stubs, monkeypatch):
    src, v1, v2 = fixture_repo(tmp_path)
    home, wh = box(tmp_path, "alongside", shell_port=8796)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_REPO_URL", f"file://{src}")
    real = T._run
    monkeypatch.setattr(T, "_run", lambda a, t=120, **k: subprocess.CompletedProcess(a, 0, "", "") if a[:3] == ["systemctl", "--user", "cat"] else real(a, t, **k))
    res = T.install(wh, home / "runtipi", home)
    env = envfile(home)
    assert res["state"] in ("ready", "degraded"), res
    assert env["HERMES_PORT"] == "8642" and env["HERMES_UNIT"] == "hermes-gateway.service" and env["HERMES_CONTAINER"] == ""
    assert env["SHELL_PORT"] == "8796"
    assert env["CHATGPT_AUTH_FILE"] == str(home / ".hermes/auth.json")


def test_tagged_box_has_no_owner_list_so_whois_admits_people_only(tmp_path, stubs, monkeypatch):
    src, v1, v2 = fixture_repo(tmp_path)
    home, wh = box(tmp_path)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_REPO_URL", f"file://{src}")
    stubs["tagged"]["on"] = True
    T.install(wh, home / "runtipi", home)
    assert envfile(home)["TROUBLESHOOT_OWNERS"] == ""


def test_hand_installed_troubleshooter_is_left_alone(tmp_path, stubs, monkeypatch):
    home, wh = box(tmp_path)
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    hand = "[Service]\nExecStart=/usr/bin/env node %h/apps/wizard-troubleshooter/server.mjs\n"
    (units / "wizard-troubleshooter.service").write_text(hand)
    res = T.install(wh, home / "runtipi", home)
    assert res["state"] == "kept"
    assert (units / "wizard-troubleshooter.service").read_text() == hand
    assert not any(c.startswith("systemctl") or "serve --bg" in c for c in stubs["calls"])


def test_someone_elses_troubleshoot_route_is_not_replaced(tmp_path, stubs, monkeypatch):
    src, v1, v2 = fixture_repo(tmp_path)
    home, wh = box(tmp_path)
    monkeypatch.setenv("WAM_TROUBLESHOOTER_REPO_URL", f"file://{src}")
    stubs["serve"]["Web"]["box.tail.ts.net:8444"]["Handlers"]["/troubleshoot"] = {"Proxy": "http://127.0.0.1:1234"}
    res = T.install(wh, home / "runtipi", home)
    assert res["state"] == "degraded" and "8444" in res["route_errors"]
    assert stubs["serve"]["Web"]["box.tail.ts.net:8444"]["Handlers"]["/troubleshoot"] == {"Proxy": "http://127.0.0.1:1234"}


def test_skips_without_tailscale_or_when_turned_off(tmp_path, stubs, monkeypatch):
    home, wh = box(tmp_path)
    monkeypatch.setenv("WIZARD_TROUBLESHOOTER", "0")
    assert T.install(wh, home / "runtipi", home)["state"] == "skipped"
    monkeypatch.delenv("WIZARD_TROUBLESHOOTER")
    from wam import links
    monkeypatch.setattr(links, "tailnet", lambda: {"state": "NeedsLogin", "host": ""})
    assert T.install(wh, home / "runtipi", home)["state"] == "waiting"
    assert not (home / ".config/systemd/user/wizard-troubleshooter.service").exists()


def test_release_pin_must_be_a_full_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("WIZARD_RELEASE_PINS", str(pins(tmp_path, commit="main")))
    with pytest.raises(ValueError):
        T.pinned_source()


def test_installer_sets_it_up_after_links_on_every_run():
    text = (REPO / "install.sh").read_text()
    assert text.index('"$HOME/.local/bin/wam" links') < text.index('"$HOME/.local/bin/wam" troubleshooter')


def test_node_download_is_refused_when_its_sha256_does_not_match(tmp_path, monkeypatch):
    import io, tarfile
    dist = tmp_path / "dist"
    dist.mkdir()
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(T.platform.machine(), "x64")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as t:
        info = tarfile.TarInfo(f"node-v{T.NODE_VERSION}-linux-{arch}/bin/node"); data = b"#!/bin/sh\nexit 0\n"
        info.size, info.mode = len(data), 0o755
        t.addfile(info, io.BytesIO(data))
    (dist / f"node-v{T.NODE_VERSION}-linux-{arch}.tar.xz").write_bytes(buf.getvalue())
    monkeypatch.setenv("WAM_NODE_DIST", f"file://{dist}")
    monkeypatch.setenv("WAM_TROUBLESHOOTER_NODE", "")
    monkeypatch.setattr(T, "_node_ok", lambda path: False)  # pretend the box has no usable Node
    p = T.paths(tmp_path / "home")
    with pytest.raises(ValueError, match="sha256"):
        T.node(p)
    assert not (p["node"] / "bin/node").exists()
