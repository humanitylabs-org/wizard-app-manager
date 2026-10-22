"""Release channels (James, 21 Oct 2026): latest = every release as cut; stable = soaked 72h on latest and passed an
upgrade test, recorded in releases/promotions/<version>.json. Automatic installs only promoted stable releases."""
import json
import subprocess
from pathlib import Path

import pytest

from wam import updates

ROOT = Path(__file__).resolve().parents[1]
REL = ROOT / "releases"
GOOD = {"version": "1.2.3", "latest_since": "2026-10-21T11:00:00Z", "promoted_at": "2026-10-24T11:00:00Z",
        "upgrade_test": {"from": "1.2.2", "to": "1.2.3", "passed": True, "evidence": "https://example.test/run/1"}}


@pytest.fixture(autouse=True)
def _no_cache():
    updates._latest.clear()
    yield
    updates._latest.clear()


def _v(path):
    return path.read_text().strip()


# The public mirror itself: what every box reads.

def test_mirror_stable_is_promoted_and_latest_never_trails_it():
    stable, latest = _v(REL / "stable"), _v(REL / "latest")
    assert (REL / f"{stable}.json").is_file() and (REL / f"{latest}.json").is_file()
    assert updates._key(latest) >= updates._key(stable)
    rec = json.loads((REL / "promotions" / f"{stable}.json").read_text())
    problems = updates.promotion_problems(rec, stable)
    assert problems == [], (f"releases/stable names {stable} without a valid promotion: {problems}. Cut releases onto "
                            "releases/latest; promote with wizard-os tools/promote_stable.py and mirror its record.")


def test_every_promotion_record_in_the_mirror_is_valid():
    for p in sorted((REL / "promotions").glob("*.json")):
        v = p.stem
        assert (REL / f"{v}.json").is_file(), f"promotion for {v} without its release file"
        assert updates.promotion_problems(json.loads(p.read_text()), v) == [], p.name


# The gate.

def test_a_full_promotion_passes():
    assert updates.promotion_problems(GOOD, "1.2.3") == []


@pytest.mark.parametrize("change, why", [
    ({"promoted_at": "2026-10-22T11:00:00Z"}, "soaked 24.0h"),
    ({"latest_since": "yesterday"}, "ISO"),
    ({"upgrade_test": {**GOOD["upgrade_test"], "passed": False}}, "no passed upgrade test"),
    ({"upgrade_test": {**GOOD["upgrade_test"], "from": "1.2.3"}}, "older release"),
    ({"upgrade_test": {**GOOD["upgrade_test"], "to": "1.2.4"}}, "older release"),
    ({"upgrade_test": {**GOOD["upgrade_test"], "evidence": " "}}, "evidence"),
    ({"version": "1.2.4"}, "not 1.2.3"),
])
def test_the_gate_names_what_is_missing(change, why):
    assert any(why in p for p in updates.promotion_problems({**GOOD, **change}, "1.2.3"))


QUICK = {**GOOD, "promoted_at": "2026-10-21T15:00:00Z"}
FIX = {"classification": "fix", "reason": "Store 404s on fresh boxes"}


def test_a_fast_tracked_fix_passes_without_the_soak():
    """wizard-os DECISIONS #82: verified fixes reach stable the same day; the upgrade test is still required."""
    assert updates.promotion_problems({**QUICK, "fast_track": FIX}, "1.2.3") == []
    assert any("soaked 4.0h" in p for p in updates.promotion_problems(QUICK, "1.2.3"))


@pytest.mark.parametrize("change, why", [
    ({"fast_track": {**FIX, "classification": "feature"}}, "features, data-format changes and big UX changes soak"),
    ({"fast_track": {**FIX, "reason": ""}}, "needs a reason"),
    ({"fast_track": "fix"}, "must be an object"),
    ({"fast_track": FIX, "upgrade_test": None}, "no passed upgrade test"),
    ({"fast_track": FIX, "upgrade_test": {**GOOD["upgrade_test"], "evidence": ""}}, "evidence"),
    ({"fast_track": FIX, "promoted_at": "2026-10-20T00:00:00Z"}, "before latest_since"),
])
def test_fast_track_is_only_for_verified_fixes(change, why):
    assert any(why in p for p in updates.promotion_problems({**QUICK, **change}, "1.2.3"))


def test_automatic_installs_a_fast_tracked_stable_fix(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.25")
    updates.set_mode("auto", h, tmp_path / "u", systemctl=False)
    d = tmp_path / "rel"
    (d / "promotions").mkdir(parents=True)
    (d / "stable").write_text("0.3.32\n")
    (d / "promotions" / "0.3.32.json").write_text(json.dumps({**QUICK, "version": "0.3.32", "fast_track": FIX,
        "upgrade_test": {**GOOD["upgrade_test"], "from": "0.3.25", "to": "0.3.32"}}))
    calls = []
    res = updates.apply_if_auto(h, d.as_uri(), _runner(calls), is_busy=lambda: False)
    assert res["started"] is True
    assert calls == [["bash", "-c", "curl -fsSL https://aiwizards.com/os.sh | WIZARD_RELEASE=0.3.32 bash -s start"]]


def test_only_releases_before_the_split_can_be_grandfathered():
    assert updates.promotion_problems({"version": "0.3.25", "grandfathered": "split"}, "0.3.25") == []
    assert updates.promotion_problems({"version": "0.3.26", "grandfathered": "split"}, "0.3.26")
    assert updates.promotion_problems(None, "0.3.26") == ["no promotion record"]


# The box.

def _mirror(tmp_path, stable, latest=None, promote=True):
    d = tmp_path / "rel"
    (d / "promotions").mkdir(parents=True, exist_ok=True)
    (d / "stable").write_text(stable + "\n")
    if latest:
        (d / "latest").write_text(latest + "\n")
    if promote:
        (d / "promotions" / f"{stable}.json").write_text(json.dumps({**GOOD, "version": stable,
                                                                    "upgrade_test": {**GOOD["upgrade_test"],
                                                                                     "from": "0.0.1", "to": stable}}))
    return d.as_uri()


def _pin(h, rel):
    h.mkdir(exist_ok=True)
    (h / "release.json").write_text(json.dumps({"release": rel, "status": "pinned"}))


def _runner(calls):
    return lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")


def test_channel_defaults_to_stable_and_is_saved_beside_the_mode(tmp_path):
    h = tmp_path / "wos"
    assert updates.channel(h) == "stable"
    updates.set_mode("auto", h, tmp_path / "u", systemctl=False)
    updates.set_channel("latest", h)
    assert updates.channel(h) == "latest" and updates.mode(h) == "auto"
    updates.set_mode("ask", h, tmp_path / "u", systemctl=False)
    assert updates.channel(h) == "latest" and updates.mode(h) == "ask"
    with pytest.raises(SystemExit):
        updates.set_channel("nightly", h)


def test_stable_box_sees_stable_and_latest_box_sees_latest(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.25")
    base = _mirror(tmp_path, "0.3.25", latest="0.3.27")
    st = updates.status(h, base)
    assert st["channel"] == "stable" and st["latest"] == "0.3.25" and st["update_available"] is False
    updates.set_channel("latest", h)
    updates._latest.clear()
    st = updates.status(h, base)
    assert st["latest"] == "0.3.27" and st["stable"] == "0.3.25" and st["update_available"] is True


def test_latest_never_trails_stable_and_a_missing_pointer_falls_back(tmp_path):
    assert updates.latest(_mirror(tmp_path, "0.3.30", latest="0.3.27"), chan="latest") == "0.3.30"
    updates._latest.clear()
    assert updates.latest(_mirror(tmp_path / "b", "0.3.30"), chan="latest") == "0.3.30"


def test_update_button_installs_the_channel_release_it_checked(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.25")
    updates.set_channel("latest", h)
    calls = []
    res = updates.apply(h, _mirror(tmp_path, "0.3.25", latest="0.3.27"), _runner(calls))
    assert res["started"] is True
    assert calls == [["bash", "-c", "curl -fsSL https://aiwizards.com/os.sh | WIZARD_RELEASE=0.3.27 bash -s start"]]


def test_automatic_installs_only_a_promoted_stable_release(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.25")
    updates.set_mode("auto", h, tmp_path / "u", systemctl=False)
    updates.set_channel("latest", h)                         # even a latest box: Automatic means stable only
    calls = []
    res = updates.apply_if_auto(h, _mirror(tmp_path, "0.3.26", latest="0.3.28"), _runner(calls), is_busy=lambda: False)
    assert res["started"] is True
    assert calls == [["bash", "-c", "curl -fsSL https://aiwizards.com/os.sh | WIZARD_RELEASE=0.3.26 bash -s start"]]


def test_automatic_skips_a_stable_pointer_without_a_promotion(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.25")
    updates.set_mode("auto", h, tmp_path / "u", systemctl=False)
    calls = []
    res = updates.apply_if_auto(h, _mirror(tmp_path, "0.3.26", promote=False), _runner(calls), is_busy=lambda: False)
    assert res["started"] is False and res["skipped"] == "unpromoted" and calls == []
    assert updates.apply(h, _mirror(tmp_path, "0.3.26", promote=False), _runner(calls))["started"] is True  # Update
