"""Rehearsal finding (wizard-os 389, KVM alongside box): rolling back from a front-enabled release to 0.3.102 left
wizard-host-dashboard-front.service holding the tailnet :9119, so the stock dashboard 0.3.102 restarts could not bind
("Port 9119 ... is already in use") and ChatGPT/Claude lost the agent dashboard. Older App Managers know nothing of
the front, so the front unit itself must step aside: PartOf= the dashboard unit (its restart reaches the front) and
ExecCondition= that the dashboard unit is still the fronted one (host-only upstream)."""
import shlex
import subprocess
from pathlib import Path

from wam import host_dashboard as hd


def _condition(text: str) -> list[str]:
    line = next(l for l in text.splitlines() if l.startswith("ExecCondition="))
    return shlex.split(line.split("=", 1)[1])


def test_front_unit_follows_the_dashboard_unit(tmp_path):
    text = hd.front_unit_text(tmp_path / "wz", python="/usr/bin/python3", unit_dir=tmp_path / "units")
    assert f"PartOf={hd.UNIT}" in text.splitlines()
    assert f"After=network-online.target tailscaled.service {hd.UNIT}" in text.splitlines()
    argv = _condition(text)
    assert argv[-1] == str(tmp_path / "units" / hd.UNIT)


def test_front_starts_only_while_the_dashboard_is_fronted(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    wz = tmp_path / "wz"
    argv = _condition(hd.front_unit_text(wz, python="/usr/bin/python3", unit_dir=units))
    dash = units / hd.UNIT
    # this release, boundary on: Hermes on the host-only upstream behind the front
    dash.write_text(hd.unit_text("/usr/bin/hermes", "/root/.hermes", hd.UPSTREAM_HOST, wz, hd.UPSTREAM_PORT))
    assert subprocess.run(argv).returncode == 0
    # what a release without the front writes (0.3.102 unit_text: the tailnet address, port 9119)
    dash.write_text(hd.unit_text("/usr/bin/hermes", "/root/.hermes", "100.73.0.1", wz))
    assert subprocess.run(argv).returncode == 1
    # no dashboard unit at all (App Manager removed it): skip too, never fail-start
    dash.unlink()
    assert subprocess.run(argv).returncode not in (0,)


def test_ensure_writes_the_rollback_safe_front_unit(tmp_path, monkeypatch):
    """ensure() under the boundary writes the front unit with the unit dir it actually used."""
    calls = []
    monkeypatch.setattr(hd, "_systemctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(hd, "tailnet_ip", lambda: "100.73.0.1")
    monkeypatch.setattr(hd, "consumers", lambda *a, **k: {"wizard-chatgpt": ["HERMES_DASHBOARD_URL"]})
    monkeypatch.setattr(hd, "stale_running", lambda *a, **k: False)
    rt, wz, units = tmp_path / "rt", tmp_path / "wz", tmp_path / "units"
    (rt / "user-config" / "wizardapps" / "wizard-chatgpt").mkdir(parents=True)
    out = hd.ensure(wz, rt, "wizardapps", {"wizard-chatgpt"}, "/usr/bin/hermes", "/root/.hermes",
                    lambda root, app, env: None, unit_dir=units, boundary=True)
    assert out["front"] == "on", out
    ftext = (units / hd.FRONT_UNIT).read_text()
    assert _condition(ftext)[-1] == str(units / hd.UNIT)
    assert subprocess.run(_condition(ftext)).returncode == 0
