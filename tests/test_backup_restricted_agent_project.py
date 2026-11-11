"""The restricted-agent runtime names its compose project after the app id alone (`agent-main-agent`), not Runtipi's
`<app>_<store>` (`agent-main-agent_wizardapps`). Backups must still find the agent's image from its running container."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam.backup.box import Agent, Box, HermesError, HermesRunner  # noqa: E402


def _runner(tmp_path, monkeypatch, projects: dict):
    """projects: compose project -> list of {id, image, home} containers."""
    box = Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin"})
    r = HermesRunner(box)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        i = argv.index("ps") if "ps" in argv else argv.index("inspect")
        if argv[i] == "ps":
            proj = [a.rsplit("=", 1)[1] for a in argv if a.startswith("label=com.docker.compose.project=")][0]
            return "\n".join(c["id"] for c in projects.get(proj, []))
        ids = argv[i + 1:]
        out = [{"Id": c["id"], "Config": {"Image": c["image"]},
                "Mounts": [{"Source": str(c["home"]), "Destination": "/opt/data"}]}
               for cs in projects.values() for c in cs if c["id"] in ids]
        return json.dumps(out)
    monkeypatch.setattr(r, "_run", fake_run)
    monkeypatch.setattr(r, "compose_image", lambda agent: None)
    return r, calls


def _agent(tmp_path, app="agent-main-agent", aid="main-agent"):
    home = tmp_path / "agents" / aid / "home"
    home.mkdir(parents=True)
    return Agent(aid, aid == "main-agent", home, "container", urn=f"{app}:wizardapps")


def test_runtipi_project_still_wins(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    r, _ = _runner(tmp_path, monkeypatch, {
        "agent-main-agent_wizardapps": [{"id": "c1", "image": "img:runtipi", "home": "/elsewhere"}],
        "agent-main-agent": [{"id": "c2", "image": "img:restricted", "home": a.home}]})
    assert r.image(a) == "img:runtipi"


@pytest.mark.parametrize("app,aid", [("agent-main-agent", "main-agent"), ("agent-sandboxed", "sandboxed")])
def test_restricted_agent_runtime_project_is_found(tmp_path, monkeypatch, app, aid):
    a = _agent(tmp_path, app, aid)
    r, calls = _runner(tmp_path, monkeypatch, {
        app: [{"id": "e1", "image": "img:edge", "home": "/unrelated"},
              {"id": "a1", "image": "ghcr.io/x/hermes-agent@sha256:abc", "home": a.home}]})
    assert r.image(a) == "ghcr.io/x/hermes-agent@sha256:abc"
    projects = [x.rsplit("=", 1)[1] for c in calls for x in c if x.startswith("label=com.docker.compose.project=")]
    assert projects == [f"{app}_wizardapps", app]


def test_bare_project_must_mount_this_agents_home(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    r, _ = _runner(tmp_path, monkeypatch, {
        "agent-main-agent": [{"id": "x", "image": "img:other", "home": tmp_path / "someone-else"}]})
    with pytest.raises(HermesError, match="mounts an agent home"):
        r.image(a)


def test_no_container_anywhere_keeps_the_old_error(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    r, _ = _runner(tmp_path, monkeypatch, {})
    with pytest.raises(HermesError, match="no container and no compose file"):
        r.image(a)


# ---------------------------------------------------------------- rootless Docker: run as the home's owner *inside* the userns

ROWS = [(0, 1000, 1), (1, 100000, 65536)]


def test_rootless_maps_a_subuid_home_owner_into_the_container(tmp_path, monkeypatch):
    r = HermesRunner(Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin"}))
    monkeypatch.setattr(r, "rootless", lambda: True)
    monkeypatch.setattr(r, "id_map", lambda kind: ROWS)
    assert r.container_owner(109999, 109999) == (10000, 10000)   # the restricted agent's hermes user
    assert r.container_owner(1000, 100999) == (0, 1000)          # the owner is root inside


def test_rootful_docker_keeps_host_ids(tmp_path, monkeypatch):
    r = HermesRunner(Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin"}))
    monkeypatch.setattr(r, "rootless", lambda: False)
    assert r.container_owner(109999, 109999) == (109999, 109999)


def test_rootless_detection_reads_docker_info(tmp_path, monkeypatch):
    r = HermesRunner(Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin"}))
    monkeypatch.setattr(r, "_run", lambda argv, **kw: '["name=seccomp,profile=builtin","name=rootless"]')
    assert r.rootless() is True
    r2 = HermesRunner(Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin"}))
    monkeypatch.setattr(r2, "_run", lambda argv, **kw: (_ for _ in ()).throw(HermesError("no docker")))
    assert r2.rootless() is False


def test_docker_run_uses_the_mapped_owner(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    r = HermesRunner(Box(tmp_path / "home", tmp_path / "rt", env={"PATH": "/usr/bin:/bin"}))
    monkeypatch.setattr(r, "container_owner", lambda u, g: (10000, 10001))
    monkeypatch.setattr(r, "image", lambda agent: "img")
    seen = []
    monkeypatch.setattr(r, "_run", lambda argv, **kw: seen.append(argv) or "")
    r._docker(a, "/bin/true", [], [])
    argv = seen[-1]
    assert argv[argv.index("-u") + 1] == "10000:10001"
