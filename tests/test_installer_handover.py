"""#499: the installer handover never pipes curl into bash.

A retried `curl | bash` can run a partial script and then the whole script again; an exhausted download must fail
the handover and run nothing. Each caller downloads to a private temporary file, then runs it from stdin."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from wam import quick_update, updates

ROOT = Path(__file__).resolve().parent.parent
FAKE_CURL = """#!/bin/sh
# fake curl: writes $FAKE_BODY to the -o target (or stdout), then exits $FAKE_RC
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { out="$2"; shift; }; shift; done
if [ -n "$out" ]; then cat "$FAKE_BODY" > "$out"; else cat "$FAKE_BODY"; fi
exit "${FAKE_RC:-0}"
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "curl").write_text(FAKE_CURL); (bin_ / "curl").chmod(0o755)
    tmp = tmp_path / "tmp"; tmp.mkdir()
    marker = tmp_path / "ran"
    body = tmp_path / "body.sh"
    body.write_text(f'echo "run $*" >> "{marker}"\n')   # the "script": records each execution and its arguments
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setenv("FAKE_BODY", str(body))
    return {"marker": marker, "tmp": tmp}


def _runs(fake):
    return fake["marker"].read_text().splitlines() if fake["marker"].exists() else []


def _as_user(fake, tmp_path):
    """Run get.sh's own as_user (sliced from get.sh) with a fake `sudo -iu` that runs the command as this user."""
    fn = re.search(r"^as_user\(\) \{\n.*?^\}\n", (ROOT / "get.sh").read_text(), re.S | re.M).group(0)
    sudo = tmp_path / "bin" / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" = -iu ] && shift 2\nexec "$@"\n'); sudo.chmod(0o755)
    prog = ("set -euo pipefail; fwd_env() { printf '%s\\n' WAM_REF=; }; enable_linger() { :; }; "
            "repo_raw() { printf 'https://example.test/%s' \"$1\"; }; WIZARD_BUS_WAIT=0\n" + fn
            + 'as_user "$(id -un)" alongside /h; echo "returned"\n')
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)


def test_no_retried_curl_is_piped_into_bash():
    """The printed one-line commands for people stay `curl … | bash` (no retry, so no replay); callers retry."""
    for f in ("get.sh", "wam/updates.py", "wam/quick_update.py"):
        assert not re.search(r"curl [^\n|]*--retry[^\n|]*\|(?!\|)", (ROOT / f).read_text()), f


@pytest.mark.parametrize("rc", ["22", "23"])
def test_a_failed_download_runs_nothing_and_fails(fake, monkeypatch, rc):
    monkeypatch.setenv("FAKE_RC", rc)                   # curl gave up after a partial body
    r = subprocess.run(["bash", "-c", updates.installer_script("WIZARD_RELEASE=0.9.2 ")], capture_output=True, text=True)
    assert r.returncode != 0 and _runs(fake) == []
    assert list(fake["tmp"].iterdir()) == []            # the partial download is removed


def test_a_good_download_runs_once_with_its_env_and_is_removed(fake):
    r = subprocess.run(["bash", "-c", updates.installer_script("WIZARD_RELEASE=0.9.2 ")], capture_output=True, text=True)
    assert r.returncode == 0 and _runs(fake) == ["run start"]
    assert list(fake["tmp"].iterdir()) == []


def test_updates_apply_reports_an_exhausted_download(fake, monkeypatch, tmp_path):
    """Without pipefail, `curl … | bash` exited 0 after curl gave up: Update looked started (#499)."""
    monkeypatch.setenv("FAKE_RC", "22")
    monkeypatch.setattr(updates, "_quick", lambda *a: False)      # the full installer path
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, capture_output=True, text=True)
    h = tmp_path / "wos"; h.mkdir()
    (h / "release.json").write_text(json.dumps({"release": "0.3.7", "status": "pinned"}))
    d = tmp_path / "rel"; d.mkdir(); (d / "stable").write_text("0.3.8\n")
    res = updates.apply(h, d.as_uri(), runner)
    assert len(calls) == 1 and res["started"] is False and _runs(fake) == []


def test_quick_update_handover_runs_once(fake):
    ops = quick_update.Ops(fake["tmp"].parent / "h", log=lambda *_: None)
    assert ops.start_installer("0.9.2", auto=False) is True
    assert _runs(fake) == ["run start"] and list(fake["tmp"].iterdir()) == []


def test_quick_update_handover_fails_closed(fake, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "22")
    ops = quick_update.Ops(fake["tmp"].parent / "h", log=lambda *_: None)
    assert ops.start_installer("0.9.2", auto=False) is False and _runs(fake) == []


def test_get_sh_as_user_runs_once_and_fails_closed(fake, monkeypatch, tmp_path):
    r = _as_user(fake, tmp_path)
    assert r.returncode == 0 and "returned" not in r.stdout          # exec: the root run ends in the user's run
    assert _runs(fake) == ["run "] and list(fake["tmp"].iterdir()) == []
    fake["marker"].unlink()
    monkeypatch.setenv("FAKE_RC", "22")
    r = _as_user(fake, tmp_path)
    assert r.returncode != 0 and "returned" not in r.stdout and "Could not download the installer" in r.stderr
    assert _runs(fake) == [] and list(fake["tmp"].iterdir()) == []
