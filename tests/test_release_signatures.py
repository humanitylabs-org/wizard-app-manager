"""Signed releases, warn-only (wizard-app-manager#565): release + promotion signatures and the installer's hash.

Every key here is a throwaway Ed25519 key made in the test's temp folder; no private key is committed."""
import json
import shutil
import subprocess

import pytest

from wam import cli, quick_update, release_sig, updates

pytestmark = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs OpenSSH ssh-keygen")


def _keygen_has_y():
    r = subprocess.run(["ssh-keygen", "-Y", "check-novalidate"], capture_output=True, text=True, input="")
    return "unknown option" not in (r.stderr or "").lower()


@pytest.fixture(autouse=True)
def _clean():
    updates._latest.clear(); quick_update._docs.clear(); release_sig._cache.clear()
    yield
    updates._latest.clear(); quick_update._docs.clear(); release_sig._cache.clear()


def _key(d, name):
    k = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(k)], check=True)
    return k


def _signers(d, *keys):
    f = d / "allowed_signers"
    f.write_text("# test list\n" + "".join(
        f'{release_sig.PRINCIPAL} namespaces="{release_sig.NAMESPACE}" {k.with_suffix(".pub").read_text().strip()}\n'
        for k in keys))
    return f


def _sign(key, path):
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", release_sig.NAMESPACE, str(path)],
                   check=True, capture_output=True)
    return path.with_name(path.name + ".sig")


@pytest.fixture
def keys(tmp_path):
    if not _keygen_has_y():
        pytest.skip("ssh-keygen has no -Y (OpenSSH < 8.0)")
    d = tmp_path / "keys"; d.mkdir()
    trusted, other = _key(d, "release"), _key(d, "stranger")
    return {"trusted": trusted, "other": other, "list": _signers(d, trusted)}


def _mirror(tmp_path, version="0.9.2", installer=None, promotion=True):
    rel = tmp_path / "rel"; rel.mkdir(exist_ok=True)
    doc = {"version": version, "components": {}}
    if installer is not None:
        doc["installer"] = installer
    (rel / f"{version}.json").write_text(json.dumps(doc, indent=2) + "\n")
    (rel / "stable").write_text(version + "\n")
    if promotion:
        (rel / "promotions").mkdir(exist_ok=True)
        (rel / "promotions" / f"{version}.json").write_text(json.dumps({
            "version": version, "latest_since": "2026-10-01T00:00:00Z", "promoted_at": "2026-10-05T00:00:00Z",
            "upgrade_test": {"from": "0.0.1", "to": version, "passed": True, "evidence": "https://example.test/run"}}))
    return rel


def _box(tmp_path, have="0.9.1"):
    h = tmp_path / "wos"; h.mkdir(exist_ok=True)
    (h / "release.json").write_text(json.dumps({"release": have, "status": "pinned"}))
    return h


def _runner(calls, out="", rc=0):
    return lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, rc, out, "")


# --- the signature itself -------------------------------------------------------------------------------------------

def test_a_good_signature_is_ok(tmp_path, keys):
    f = tmp_path / "0.9.2.json"; f.write_text('{"version": "0.9.2"}\n')
    sig = _sign(keys["trusted"], f)
    assert release_sig.verify(f.read_bytes(), sig.read_bytes(), keys["list"]) == {"signature": "ok"}


def test_a_missing_signature_is_missing(tmp_path, keys):
    assert release_sig.verify(b"{}", None, keys["list"])["signature"] == "missing"


def test_a_bad_signature_is_bad(tmp_path, keys):
    f = tmp_path / "0.9.2.json"; f.write_text('{"version": "0.9.2"}\n')
    sig = _sign(keys["trusted"], f)
    res = release_sig.verify(b'{"version": "0.9.2", "evil": 1}\n', sig.read_bytes(), keys["list"])
    assert res["signature"] == "bad" and "does not match" in res["reason"]
    assert release_sig.verify(f.read_bytes(), b"not a signature", keys["list"])["signature"] == "bad"


def test_a_release_signed_by_a_key_not_on_the_list_is_bad(tmp_path, keys):
    f = tmp_path / "0.9.2.json"; f.write_text('{"version": "0.9.2"}\n')
    sig = _sign(keys["other"], f)
    res = release_sig.verify(f.read_bytes(), sig.read_bytes(), keys["list"])
    assert res == {"signature": "bad", "reason": "signed by a key that is not on the trusted list"}


def test_a_second_key_on_the_list_lets_the_key_change_later(tmp_path, keys):
    both = _signers(tmp_path / "keys", keys["trusted"], keys["other"])
    f = tmp_path / "0.9.2.json"; f.write_text("{}\n")
    assert release_sig.verify(f.read_bytes(), _sign(keys["other"], f).read_bytes(), both)["signature"] == "ok"


def test_the_shipped_list_has_no_key_yet_so_nothing_counts_as_signed(tmp_path, keys):
    assert release_sig.signers() == []                         # the real key is added by a person later
    f = tmp_path / "x.json"; f.write_text("{}\n")
    res = release_sig.verify(f.read_bytes(), _sign(keys["trusted"], f).read_bytes())
    assert res["signature"] == "bad" and "no trusted release key" in res["reason"]


def test_promotion_records_signed_and_unsigned(tmp_path, keys):
    rel = _mirror(tmp_path)
    base = rel.as_uri()
    assert release_sig.check_promotion("0.9.2", base, keys=keys["list"])["signature"] == "missing"
    release_sig._cache.clear()
    _sign(keys["trusted"], rel / "promotions" / "0.9.2.json")
    assert release_sig.check_promotion("0.9.2", base, keys=keys["list"])["signature"] == "ok"


# --- the installer hash ---------------------------------------------------------------------------------------------

def _installer(tmp_path, body="echo ran \"$@\" > \"$OUT\"\n"):
    f = tmp_path / "os.sh"; f.write_text("#!/usr/bin/env bash\n" + body)
    import hashlib
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def _run(script, tmp_path):
    out = tmp_path / "ran.txt"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin",
                       "OUT": str(out), "TMPDIR": str(tmp_path)})
    return r, (out.read_text() if out.exists() else None)


@pytest.fixture
def tmpdir_env(tmp_path, monkeypatch):
    d = tmp_path / "tmp"; d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    return d


def test_an_installer_that_matches_runs_from_the_checked_file(tmp_path, tmpdir_env):
    f, sha = _installer(tmp_path)
    h = tmp_path / "wos"
    script, rec = release_sig.checked_installer(h, {"mode": "warn", "release": "0.9.2", "expect": [sha]}, "", f.as_uri())
    assert rec["installer"] == "ok" and rec["installer_sha256"] == sha
    f.write_text("echo swapped > \"$OUT\"\n")                   # a later change to the source is not what runs
    r, ran = _run(script, tmp_path)
    assert r.returncode == 0 and ran == "ran start\n"
    assert list(tmpdir_env.iterdir()) == []                      # the checked copy is removed afterwards


def test_an_installer_whose_hash_does_not_match_warns_and_carries_on(tmp_path, tmpdir_env):
    body = 'echo ran "$@" > "$OUT"; echo "installer-sha256: ' + "0" * 64 + '" >&2\n'   # it cannot forge the hash
    f, sha = _installer(tmp_path, body)
    h = tmp_path / "wos"
    script, rec = release_sig.checked_installer(h, {"mode": "warn", "release": "0.9.2", "expect": ["0" * 64]}, "",
                                                f.as_uri())
    assert rec["installer"] == "mismatch" and rec["installer_sha256"] == sha
    assert release_sig.read(h)["installer"] == "mismatch"
    assert "[signature] installer sha256" in (h / "install.log").read_text()
    r, ran = _run(script, tmp_path)
    assert r.returncode == 0 and ran == "ran start\n"


def test_enforce_refuses_a_mismatched_installer_and_runs_nothing(tmp_path, tmpdir_env):
    f, _ = _installer(tmp_path)
    script, rec = release_sig.checked_installer(tmp_path / "wos", {"mode": "enforce", "expect": ["0" * 64]}, "",
                                                f.as_uri())
    assert script is None and "does not match" in rec["installer_error"] and list(tmpdir_env.iterdir()) == []


def test_a_failed_installer_download_starts_nothing(tmp_path, tmpdir_env, monkeypatch):
    monkeypatch.setattr(release_sig.time, "sleep", lambda s: None)
    script, rec = release_sig.checked_installer(tmp_path / "wos", {"mode": "warn", "expect": []}, "",
                                                (tmp_path / "gone.sh").as_uri())
    assert script is None and "could not download" in rec["installer_error"]


def test_a_release_naming_no_installer_hash_runs_the_installer_unchecked(tmp_path, tmpdir_env):
    f, sha = _installer(tmp_path)
    script, rec = release_sig.checked_installer(tmp_path / "wos", {"mode": "warn", "expect": []}, "", f.as_uri())
    assert rec["installer"] == "unnamed"
    r, ran = _run(script, tmp_path)
    assert r.returncode == 0 and ran == "ran start\n"
    assert release_sig.installer_hashes({"installer": {"os.sh": sha.upper(), "get.sh": "short"}}) == [sha]


def _serve_installer(tmp_path, monkeypatch, body="exit 0\n"):
    import hashlib
    f = tmp_path / "served-os.sh"; f.write_text(body)
    monkeypatch.setattr(updates, "INSTALLER", f.as_uri())
    (tmp_path / "tmp").mkdir(exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    return hashlib.sha256(f.read_bytes()).hexdigest()


# --- the switch on the update path ----------------------------------------------------------------------------------

def test_with_the_switch_off_updates_are_unchanged(tmp_path, monkeypatch):
    rel, h = _mirror(tmp_path), _box(tmp_path)
    before = sorted(p.name for p in h.iterdir())
    calls = []
    res = updates.apply(h, rel.as_uri(), _runner(calls))
    assert res["started"] is True and "signature" not in res
    assert calls == [["bash", "-c", updates.installer_script("WIZARD_RELEASE=0.9.2 ")]]   # exactly main's command
    assert sorted(p.name for p in h.iterdir()) == before                                 # nothing new written
    assert "signature" not in updates.status(h, rel.as_uri())
    monkeypatch.setenv(release_sig.ENV, "nonsense")                                      # unknown = off
    assert release_sig.mode(h) == "off"


def test_warn_against_todays_unsigned_mirror_completes_and_records_missing(tmp_path, monkeypatch):
    rel, h = _mirror(tmp_path), _box(tmp_path)
    _serve_installer(tmp_path, monkeypatch)
    monkeypatch.setenv(release_sig.ENV, "warn")
    calls = []
    res = updates.apply(h, rel.as_uri(), _runner(calls))
    assert res["started"] is True and len(calls) == 1
    assert calls[0][-1].startswith("trap 'rm -f ") and "WIZARD_RELEASE=0.9.2 bash " in calls[0][-1]
    assert res["signature"]["signature"] == "missing" and res["signature"]["installer"] == "unnamed"
    rec = release_sig.read(h)
    assert rec["release"] == "0.9.2" and rec["signature"] == "missing" and rec["promotion"]["signature"] == "missing"
    log = (h / "install.log").read_text()
    assert "release 0.9.2: signature missing" in log and "carrying on (warn-only)" in log


def test_warn_records_a_good_signature_and_the_installer_hash(tmp_path, monkeypatch, keys):
    sha = _serve_installer(tmp_path, monkeypatch)
    rel, h = _mirror(tmp_path, installer={"os.sh": sha, "get.sh": sha}), _box(tmp_path)
    _sign(keys["trusted"], rel / "0.9.2.json"); _sign(keys["trusted"], rel / "promotions" / "0.9.2.json")
    monkeypatch.setattr(release_sig, "SIGNERS", keys["list"])
    monkeypatch.setenv(release_sig.ENV, "warn")
    calls = []
    res = updates.apply(h, rel.as_uri(), _runner(calls))
    assert res["started"] is True and len(calls) == 1
    assert res["signature"] == {**res["signature"], "signature": "ok", "installer": "ok", "promotion": "ok"}


def test_warn_with_a_bad_signature_still_updates(tmp_path, monkeypatch, keys):
    rel, h = _mirror(tmp_path, installer={"os.sh": "0" * 64}), _box(tmp_path)
    _serve_installer(tmp_path, monkeypatch)
    _sign(keys["other"], rel / "0.9.2.json")
    monkeypatch.setattr(release_sig, "SIGNERS", keys["list"])
    monkeypatch.setenv(release_sig.ENV, "warn")
    calls = []
    res = updates.apply(h, rel.as_uri(), _runner(calls))
    assert res["started"] is True and res["signature"]["signature"] == "bad" and len(calls) == 1
    assert res["signature"]["installer"] == "mismatch"           # warn-only: the update still started


def test_enforce_is_warn_only_in_this_step(tmp_path, monkeypatch):
    rel, h = _mirror(tmp_path), _box(tmp_path)
    _serve_installer(tmp_path, monkeypatch)
    monkeypatch.setenv(release_sig.ENV, "enforce")
    assert release_sig.mode(h) == "warn"
    calls = []
    assert updates.apply(h, rel.as_uri(), _runner(calls))["started"] is True and len(calls) == 1
    st = updates.status(h, rel.as_uri())
    assert st["signature"]["requested"] == "enforce" and "not available yet" in st["signature"]["note"]


def test_enforce_once_available_refuses_an_unsigned_release_and_changes_nothing(tmp_path, monkeypatch):
    rel, h = _mirror(tmp_path), _box(tmp_path)
    monkeypatch.setattr(release_sig, "ENFORCE_AVAILABLE", True)
    monkeypatch.setenv(release_sig.ENV, "enforce")
    calls = []
    res = updates.apply(h, rel.as_uri(), _runner(calls))
    assert res["started"] is False and "release signature missing" in res["error"] and calls == []


def test_dry_run_status_prints_the_signature_state(tmp_path, monkeypatch, capsys):
    rel, h = _mirror(tmp_path), _box(tmp_path)
    monkeypatch.setenv("WIZARD_OS_HOME", str(h))
    monkeypatch.setenv("WIZARD_RELEASE_BASE", rel.as_uri())
    assert cli.main(["updates", "--verify-releases", "warn"]) == 0
    assert json.loads((h / "updates.json").read_text())["verify_releases"] == "warn"
    capsys.readouterr()
    assert cli.main(["updates"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["signature"]["mode"] == "warn" and out["signature"]["release"] == "0.9.2"
    assert out["signature"]["signature"] == "missing"
    updates.set_channel("latest", h)                                # other saved choices keep the setting
    assert json.loads((h / "updates.json").read_text())["verify_releases"] == "warn"
    assert cli.main(["updates", "--verify-releases", "off"]) == 0
    capsys.readouterr(); updates._latest.clear()
    assert cli.main(["updates"]) == 0
    assert "signature" not in json.loads(capsys.readouterr().out)


def test_quick_update_writes_the_signature_into_release_and_install_status(tmp_path, monkeypatch):
    from tests.test_quick_update import FakeOps, _apps, _rel, WAM
    rel = tmp_path / "rel"; rel.mkdir()
    old, new = _rel("0.9.1"), _rel("0.9.2", ux="9", ux_commit="f")
    for d in (old, new):
        (rel / f"{d['version']}.json").write_text(json.dumps(d, indent=2) + "\n")
    h = tmp_path / "wos"; h.mkdir()
    (h / "release.json").write_text(json.dumps({"release": "0.9.1", "mode": "alongside", "status": "pinned"}))
    (h / "release-pins.json").write_bytes((rel / "0.9.1.json").read_bytes())
    (h / "alongside.json").write_text(json.dumps({"mode": "alongside", "shell": {"port": 8796}}))
    _apps(h, old)
    monkeypatch.setattr(quick_update, "checkout_state", lambda: (WAM, True))
    monkeypatch.delenv("WIZARD_RELEASE_BASE", raising=False)
    monkeypatch.setenv(release_sig.ENV, "warn")
    res = quick_update.run("0.9.2", h, rel.as_uri(), ops=FakeOps(h))
    assert res["done"] is True
    assert json.loads((h / "release.json").read_text())["signature"] == "missing"
    st = json.loads((h / "install-status.json").read_text())
    assert st["state"] == "done" and st["signature"]["signature"] == "missing"


def test_a_detached_quick_update_keeps_an_environment_only_switch(tmp_path, monkeypatch):
    assert "WIZARD_VERIFY_RELEASES" not in updates._run_env(tmp_path, None)         # off: main's environment
    monkeypatch.setenv(release_sig.ENV, "warn")
    assert updates._run_env(tmp_path, None)["WIZARD_VERIFY_RELEASES"] == "warn"
