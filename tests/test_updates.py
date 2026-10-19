"""Owner-chosen updates (wam/updates.py): check vs stable, Ask me / Automatic, apply = installer rerun."""
import json
import subprocess

import pytest

from wam import updates


def _base(tmp_path, stable):
    d = tmp_path / "rel"; d.mkdir(exist_ok=True)
    (d / "stable").write_text(stable + "\n")
    return d.as_uri()


def test_status_compares_installed_release_with_stable(tmp_path):
    h = tmp_path / "wos"; h.mkdir()
    base = _base(tmp_path, "0.3.8")
    assert updates.status(h, base) == {"mode": "ask", "installed": None, "latest": "0.3.8",
                                       "update_available": True, "checked": True}   # unpinned box
    (h / "release.json").write_text(json.dumps({"release": "0.3.8", "status": "pinned"}))
    assert updates.status(h, base)["update_available"] is False
    (h / "release.json").write_text(json.dumps({"release": "0.3.10"}))
    assert updates.status(h, _base(tmp_path, "0.3.9"))["update_available"] is False   # numeric, not string order
    assert updates.status(h, _base(tmp_path, "0.4.0"))["update_available"] is True
    off = updates.status(h, (tmp_path / "missing").as_uri())
    assert off["checked"] is False and off["update_available"] is False


def test_mode_defaults_to_ask_and_auto_installs_the_nightly_timer(tmp_path):
    h, ud = tmp_path / "wos", tmp_path / "units"
    assert updates.mode(h) == "ask"
    updates.set_mode("auto", h, ud, systemctl=False)
    assert updates.mode(h) == "auto"
    timer = (ud / "wizard-os-update.timer").read_text()
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "updates --apply-if-auto" in (ud / "wizard-os-update.service").read_text()
    updates.set_mode("ask", h, ud, systemctl=False)
    assert updates.mode(h) == "ask" and not list(ud.iterdir())
    with pytest.raises(SystemExit):
        updates.set_mode("sometimes", h, ud, systemctl=False)


def test_apply_reruns_the_installer_only_when_newer(tmp_path):
    h = tmp_path / "wos"; h.mkdir()
    (h / "release.json").write_text(json.dumps({"release": "0.3.7"}))
    calls = []
    run = lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
    assert updates.apply_if_auto(h, _base(tmp_path, "0.3.8"), run)["started"] is False and calls == []  # Ask me
    res = updates.apply(h, _base(tmp_path, "0.3.8"), run)
    assert res["started"] is True and calls[-1][-1] == "curl -fsSL https://aiwizards.com/os.sh | bash -s start"
    (h / "release.json").write_text(json.dumps({"release": "0.3.8"}))
    assert updates.apply(h, _base(tmp_path, "0.3.8"), run)["started"] is False and len(calls) == 1
