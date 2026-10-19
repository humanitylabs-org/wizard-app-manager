"""get.sh follows one Wizard OS release file (wizard-os#27): pinned App Manager commit + image digests, then a
read-back of what actually runs. Runs get.sh as this user against a local App Manager repo, a file:// release base
and a stand-in `docker`; nothing on the box is installed or contacted."""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WAM_DIGEST = "sha256:" + "a" * 64
SHELL_DIGEST = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64

STUB_INSTALL = """#!/usr/bin/env bash
set -eu
mkdir -p "$HOME/.wizard-os"
printf 'WAM_IMAGE=%s\\nWIZARD_SHELL_IMAGE=%s\\nWIZARD_MODE=%s\\n' "${WAM_IMAGE:-}" "${WIZARD_SHELL_IMAGE:-}" \
  "${WIZARD_MODE:-}" > "$HOME/install-env"
printf '%s\\n' "$APPS_JSON" > "$HOME/.wizard-os/apps.json"
exit "${INSTALL_RC:-0}"
"""

FAKE_DOCKER = """#!/usr/bin/env bash
case "$1 ${2:-}" in
  "info "*) exit 0 ;;
  "ps "*) echo "runtipi"; echo "wizard-shell_wizardapps-1" ;;
  "inspect "*) case "${@: -1}" in runtipi) echo img-runtipi ;; wizard-shell*) echo img-shell ;; *) exit 1 ;; esac ;;
  "image inspect") case "${@: -1}" in
      img-runtipi) echo "[\\"ghcr.io/humanitylabs-org/wizard-app-manager@$RUNTIPI_DIGEST\\"]" ;;
      img-shell) echo "[\\"ghcr.io/humanitylabs-org/wizard-shell@$SHELL_DIGEST\\"]" ;;
    esac ;;
esac
"""


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def box(tmp_path):
    src = tmp_path / "wam-src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    (src / "install.sh").write_text(STUB_INSTALL)
    git("add", ".", cwd=src)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pinned", cwd=src)
    pinned = git("rev-parse", "HEAD", cwd=src)
    (src / "install.sh").write_text(STUB_INSTALL.replace("exit", "echo moved-on; exit"))
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "later", cwd=src)
    rel = tmp_path / "releases"
    rel.mkdir()
    release = {
        "version": "9.9.9",
        "components": {"wizard-app-manager": {"repo": "humanitylabs-org/wizard-app-manager", "commit": pinned}},
        "images": {
            "wizard-app-manager": {"ref": "ghcr.io/humanitylabs-org/wizard-app-manager:latest", "digest": WAM_DIGEST},
            "wizard-shell": {"ref": "ghcr.io/humanitylabs-org/wizard-shell:0.2.0", "digest": SHELL_DIGEST},
        },
        "default_apps": {
            "shell": {"app": "wizard-shell", "index_version": "0.2.0"},
            "agent": {"app": "hermes-agent", "index_version": "1", "alongside": "external: host Hermes"},
            "files": {"app": "files", "index_version": "2.63.23"},
            "notifications": None,
        },
    }
    (rel / "9.9.9.json").write_text(json.dumps(release))
    (rel / "stable").write_text("9.9.9\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "sudo").write_text("#!/bin/sh\n# Authentication is out of scope for this release-only sandbox.\nexit 0\n")
    (bin_dir / "sudo").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    # Alongside preflight needs an actual sandbox Hermes, not the developer's host install.
    hh = home / ".hermes"; hh.mkdir()
    (hh / "config.yaml").write_text("model: fixture\n")
    hermes = bin_dir / "hermes"
    hermes.write_text("#!/bin/sh\nexit 0\n"); hermes.chmod(0o755)
    apps = {"apps": [{"id": "wizard-shell", "version": "0.2.0"}, {"id": "files", "version": "2.63.23"},
                     {"id": "hermes-agent", "version": "1"}]}
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home), "WIZARD_MODE": "fresh",
           "WAM_REPO": str(src), "WIZARD_RELEASE_BASE": f"file://{rel}", "WAM_DIR": str(home / "wam"),
           "RUNTIPI_DIGEST": WAM_DIGEST, "SHELL_DIGEST": SHELL_DIGEST, "APPS_JSON": json.dumps(apps),
           "GIT_CONFIG_NOSYSTEM": "1", "HERMES_HOME": str(hh), "WIZARD_HERMES_BIN": str(hermes)}

    def run(**extra):
        e = {**env, **{k: v for k, v in extra.items() if v is not None}}
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
        p = subprocess.run(["bash", str(REPO / "get.sh")], env=e, capture_output=True, text=True, timeout=60)
        out = home / ".wizard-os" / "release.json"
        return p, (json.loads(out.read_text()) if out.is_file() else None)

    return {"run": run, "pinned": pinned, "home": home, "rel": rel, "apps": apps, "src": src}


def test_stable_release_pins_commit_and_images_and_reads_back(box):
    p, rec = box["run"]()
    assert p.returncode == 0, p.stdout + p.stderr
    assert git("rev-parse", "HEAD", cwd=box["home"] / "wam") == box["pinned"]
    seen = (box["home"] / "install-env").read_text()
    assert f"WAM_IMAGE=ghcr.io/humanitylabs-org/wizard-app-manager@{WAM_DIGEST}" in seen
    assert f"WIZARD_SHELL_IMAGE=ghcr.io/humanitylabs-org/wizard-shell@{SHELL_DIGEST}" in seen
    assert rec["release"] == "9.9.9" and rec["status"] == "pinned", rec
    assert all(c["ok"] for c in rec["checks"])
    assert {c["name"] for c in rec["checks"]} >= {"wizard-app-manager commit", "image wizard-app-manager",
                                                  "image wizard-shell", "default app shell", "default app files"}


def test_rerun_resolves_the_same_bits(box):
    first, rec1 = box["run"]()
    second, rec2 = box["run"]()
    assert first.returncode == second.returncode == 0, second.stdout + second.stderr
    assert [c["observed"] for c in rec1["checks"]] == [c["observed"] for c in rec2["checks"]]
    assert git("rev-parse", "HEAD", cwd=box["home"] / "wam") == box["pinned"]


def test_default_app_drift_is_reported_and_strict_fails(box):
    apps = json.loads(json.dumps(box["apps"]))
    apps["apps"][1]["version"] = "2.70.0"
    p, rec = box["run"](APPS_JSON=json.dumps(apps))
    assert p.returncode == 0, p.stdout + p.stderr
    assert rec["status"] == "drift" and "DRIFT  default app files: files 2.70.0" in p.stdout
    p, rec = box["run"](APPS_JSON=json.dumps(apps), WIZARD_STRICT="1")
    assert p.returncode == 3


def test_app_manager_image_mismatch_fails(box):
    p, rec = box["run"](RUNTIPI_DIGEST=OTHER)
    assert p.returncode == 1
    assert rec["status"] == "mismatch"
    assert "FAIL  image wizard-app-manager" in p.stdout


def test_failed_install_keeps_its_exit_code_and_records_it_as_failed(box):
    p, rec = box["run"](INSTALL_RC="5")
    assert p.returncode == 5
    assert rec["release"] == "9.9.9" and rec["status"] == "install-failed" and rec["install_rc"] == 5


def test_explicit_release_and_alongside_skip_the_external_agent(box):
    p, rec = box["run"](WIZARD_RELEASE="9.9.9", WIZARD_MODE="alongside")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "default app agent" not in {c["name"] for c in rec["checks"]}


def test_no_stable_pointer_installs_main_unpinned_and_says_so(box):
    (box["rel"] / "stable").unlink()
    p, rec = box["run"]()
    assert p.returncode == 0, p.stdout + p.stderr
    assert "no Wizard OS stable release is published" in p.stdout and rec["status"] == "unpinned"
    assert "moved-on" in p.stdout   # main's install.sh ran, not the pinned one


def test_strict_refuses_an_unpinned_install(box):
    (box["rel"] / "stable").unlink()
    p, rec = box["run"](WIZARD_STRICT="1")
    assert p.returncode == 3 and rec is None
    assert not (box["home"] / "install-env").exists()


def test_an_unreachable_release_base_fails_instead_of_going_unpinned(box):
    p, rec = box["run"](WIZARD_RELEASE_BASE="http://127.0.0.1:9/releases")
    assert p.returncode == 1, p.stdout + p.stderr
    assert not (box["home"] / "install-env").exists() and rec is None


def test_an_old_pinned_record_never_survives_a_later_unpinned_run(box):
    p, rec = box["run"]()
    assert rec["status"] == "pinned"
    p, rec = box["run"](WAM_REF="main")
    assert p.returncode == 0 and rec["status"] == "unpinned"


def test_wam_ref_is_the_unpinned_developer_override(box):
    p, rec = box["run"](WAM_REF="main")
    assert p.returncode == 0, p.stdout + p.stderr
    assert rec["status"] == "unpinned" and "WAM_REF" in rec["reason"] and "moved-on" in p.stdout


def test_fresh_box_reports_a_different_shell_as_drift(box):
    p, rec = box["run"](SHELL_DIGEST=OTHER)
    assert p.returncode == 0 and rec["status"] == "drift" and "DRIFT  image wizard-shell" in p.stdout


def test_alongside_updates_a_shell_left_on_an_old_digest_and_fails_if_it_stays(box):
    # stand-in wam records the update; the stand-in docker keeps reporting the old digest, so it must FAIL
    wam = Path(box["home"]) / ".local" / "bin" / "wam"
    wam.parent.mkdir(parents=True, exist_ok=True)
    wam.write_text('#!/usr/bin/env bash\necho "$@" >> "$HOME/wam-calls"; exit 1\n')   # real wam exits 1 too
    wam.chmod(0o755)
    p, rec = box["run"](SHELL_DIGEST=OTHER, WIZARD_MODE="alongside", WIZARD_SHELL_WAIT="1")
    assert (box["home"] / "wam-calls").read_text().strip() == "app update wizard-shell"
    assert p.returncode == 1 and rec["status"] == "mismatch" and "FAIL  image wizard-shell" in p.stdout
    assert "fix: ~/.local/bin/wam app update wizard-shell" in p.stdout


def test_alongside_shell_update_that_lands_is_a_pass(box, tmp_path):
    # the stand-in wam "updates" the shell: from then on the stand-in docker reports the pinned digest
    wam = Path(box["home"]) / ".local" / "bin" / "wam"
    wam.parent.mkdir(parents=True, exist_ok=True)
    wam.write_text(f'#!/usr/bin/env bash\necho "$@" >> "$HOME/wam-calls"; echo {SHELL_DIGEST} > "$HOME/shell-now"\n')
    wam.chmod(0o755)
    docker = Path(box["home"]).parent / "bin" / "docker"
    docker.write_text(docker.read_text().replace('$SHELL_DIGEST', '$(cat "$HOME/shell-now" 2>/dev/null || echo $SHELL_DIGEST)'))
    p, rec = box["run"](SHELL_DIGEST=OTHER, WIZARD_MODE="alongside")
    assert (box["home"] / "wam-calls").read_text().strip() == "app update wizard-shell"
    assert p.returncode == 0 and rec["status"] == "pinned", p.stdout


def test_strict_failure_leaves_the_previous_record_alone(box):
    p, rec = box["run"]()
    assert rec["status"] == "pinned"
    p, rec = box["run"](WAM_REF="main", WIZARD_STRICT="1")
    assert p.returncode == 3 and rec["status"] == "pinned"


def test_alongside_source_readback_uses_registry_after_image_update(box):
    """A landed update changes both the running digest and the registry source commit."""
    new_commit, old_commit = "d" * 40, "e" * 40
    release_path = box["rel"] / "9.9.9.json"
    release = json.loads(release_path.read_text())
    release["default_apps"]["shell"].update(repo="humanitylabs-org/wizard-shell", commit=new_commit)
    release_path.write_text(json.dumps(release))
    before = json.loads(json.dumps(box["apps"]))
    before["apps"][0]["commit"] = old_commit
    after = json.loads(json.dumps(before))
    after["apps"][0]["commit"] = new_commit
    wam = box["home"] / ".local" / "bin" / "wam"
    wam.parent.mkdir(parents=True, exist_ok=True)
    wam.write_text('#!/usr/bin/env bash\n' +
                   f'printf "%s\\n" "{SHELL_DIGEST}" > "$HOME/shell-now"\n' +
                   "printf '%s\\n' '" + json.dumps(after) + "' > \"$HOME/.wizard-os/apps.json\"\n")
    wam.chmod(0o755)
    docker = box["home"].parent / "bin" / "docker"
    docker.write_text(docker.read_text().replace('$SHELL_DIGEST', '$(cat "$HOME/shell-now" 2>/dev/null || echo $SHELL_DIGEST)'))
    p, rec = box["run"](APPS_JSON=json.dumps(before), SHELL_DIGEST=OTHER, WIZARD_MODE="alongside")
    assert p.returncode == 0 and rec["status"] == "pinned", p.stdout + p.stderr
    source = next(c for c in rec["checks"] if c["name"] == "default app source shell")
    assert source["ok"] and source["observed"] == new_commit


def test_a_failed_release_fetch_leaves_the_previous_record_alone(box):
    p, rec = box["run"]()
    p, rec = box["run"](WIZARD_RELEASE_BASE="http://127.0.0.1:9/releases")
    assert p.returncode == 1 and rec["status"] == "pinned"


def test_an_odd_wam_ref_still_writes_valid_json(box):
    git("branch", 'dev"x', cwd=box["src"])
    p, rec = box["run"](WAM_REF='dev"x')
    assert p.returncode == 0, p.stdout + p.stderr
    assert rec["status"] == "unpinned" and 'dev"x' in rec["reason"]


def test_a_modified_app_manager_checkout_is_refused(box):
    p, _ = box["run"]()
    assert p.returncode == 0
    (box["home"] / "wam" / "install.sh").write_text("#!/usr/bin/env bash\necho tampered\n")
    p, rec = box["run"]()
    assert p.returncode == 1 and "local changes" in p.stderr and rec is None


def test_a_broken_default_app_entry_still_writes_a_record(box):
    f = box["rel"] / "9.9.9.json"
    rel = json.loads(f.read_text())
    rel["default_apps"]["files"] = {"index_version": "1"}
    f.write_text(json.dumps(rel))
    p, rec = box["run"]()
    assert rec is not None and rec["status"] in ("drift", "pinned", "mismatch")


@pytest.mark.parametrize("bad", [
    {"drop_image": "wizard-app-manager"},
    {"commit": "main"},
    {"digest": "sha256:short"},
    {"ref": "ghcr.io/x/y; rm -rf ~"},
    {"version": "1.0.0"},
])
def test_a_malformed_release_is_refused_before_anything_runs(box, bad):
    f = box["rel"] / "9.9.9.json"
    rel = json.loads(f.read_text())
    if "commit" in bad:
        rel["components"]["wizard-app-manager"]["commit"] = bad["commit"]
    if "digest" in bad:
        rel["images"]["wizard-shell"]["digest"] = bad["digest"]
    if "ref" in bad:
        rel["images"]["wizard-shell"]["ref"] = bad["ref"]
    if "version" in bad:
        rel["version"] = bad["version"]
    if "drop_image" in bad:
        del rel["images"][bad["drop_image"]]
    f.write_text(json.dumps(rel))
    p, rec = box["run"]()
    assert p.returncode == 2, p.stdout + p.stderr
    assert not (box["home"] / "install-env").exists() and rec is None


def test_a_version_with_shell_characters_is_refused(box):
    p, _ = box["run"](WIZARD_RELEASE="1.0;id")
    assert p.returncode == 2


def test_unpinned_record_is_only_written_once_the_checkout_worked(box):
    p, rec = box["run"]()
    assert rec["status"] == "pinned"
    p, rec = box["run"](WAM_REF="no-such-branch")
    assert p.returncode != 0 and rec["status"] == "pinned"
