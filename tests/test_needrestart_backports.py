"""Security backports must not be mistaken for vulnerable upstream 3.6."""
import subprocess

import pytest

from wam import os_patch as op


@pytest.mark.parametrize("distro,release,nr,scandeps,allowed", [
    ("ubuntu", "24.04", "3.6-7ubuntu4.3", "1.35-1ubuntu0.24.04.1", True),
    ("ubuntu", "24.04", "3.6-7ubuntu4.10", "1.35-1ubuntu0.24.04.2", True),
    ("debian", "12", "3.6-4+deb12u2", "1.31-2+deb12u1", True),
    ("debian", "12", "3.6-4+deb12u10", "1.31-2+deb12u2", True),
    ("ubuntu", "24.04", "3.6-7ubuntu4.2", "1.35-1ubuntu0.24.04.1", False),
    ("ubuntu", "24.04", "3.6-7ubuntu4.3", "1.35-1", False),
    ("debian", "12", "3.6-4+deb12u1", "1.31-2+deb12u1", False),
    ("debian", "12", "3.6-4+deb12u2", "1.31-2", False),
    ("linuxmint", "24.04", "3.6-7ubuntu4.3", "1.35-1ubuntu0.24.04.1", False),
    ("ubuntu", "22.04", "3.6-7ubuntu4.3", "1.35-1ubuntu0.24.04.1", False),
    ("debian", "13", "3.6-4+deb12u2", "1.31-2+deb12u1", False),
    ("debian", "12", "3.6-7ubuntu4.3", "1.31-2+deb12u1", False),
    ("ubuntu", "24.04", "9:3.6-7ubuntu4.3", "1.35-1ubuntu0.24.04.1", False),
    ("debian", "12", "3.6-4+deb12u2~local", "1.31-2+deb12u1", False),
    ("debian", "12", "3.6-4+deb12u2+custom", "1.31-2+deb12u1", False),
    ("debian", "12", "3.6-4+deb12u2", "1.31-2+deb12u1\nextra", False),
])
@pytest.mark.parametrize("fault", [None, "missing", "unconfigured", "query_error", "compare_error"])
def test_restart_check_backport_security_gate(monkeypatch, distro, release, nr, scandeps, allowed, fault):
    calls = []
    monkeypatch.setattr(op, "detect", lambda: op.Apt({}))
    monkeypatch.setattr(op.Apt, "reboot_required", lambda s: False)
    monkeypatch.setattr(op, "os_release", lambda: {"ID": distro, "VERSION_ID": release})
    monkeypatch.setattr(op.shutil, "which", lambda n: "/usr/bin/" + n)

    def runner(argv, **kw):
        calls.append(argv)
        rc, out = 0, ""
        if argv[0] == "systemctl":
            out = "running\n"
        elif argv == ["needrestart", "--version"]:
            out = "needrestart 3.6 - Restart daemons\n"
        elif argv[0] == "dpkg-query":
            if fault == "query_error":
                raise OSError("metadata unavailable")
            rc = 1 if fault == "missing" else 0
            out = ("deinstall ok config-files" if fault == "unconfigured" else "install ok installed") + "\t" + (nr if argv[-1] == "needrestart" else scandeps) + "\n"
        elif argv[0] == "dpkg":
            if fault == "compare_error":
                raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.run(argv, capture_output=True, text=True)
        else:
            assert argv == ["needrestart", "-b", "-r", "l"]
            out = "NEEDRESTART-SVC: stale.service\n"
        return subprocess.CompletedProcess(argv, rc, out, "")

    monkeypatch.setattr(op, "run", runner)
    doc = op.restart_check()
    permitted = allowed and fault is None
    assert doc["state"] == ("current" if permitted else "report_only")
    assert doc["services"] == (["stale.service"] if permitted else [])
    assert (["needrestart", "-b", "-r", "l"] in calls) is permitted
