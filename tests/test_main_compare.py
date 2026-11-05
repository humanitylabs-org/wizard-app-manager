"""Settings > About: how far the running shell is from wizard-ux main (wizard-ux#887 rule 5)."""
import io
import json
import urllib.error

import pytest

from wam import main_compare as mc

A, B = "a" * 40, "b" * 40


@pytest.fixture(autouse=True)
def _clean():
    mc._cache.clear()
    yield
    mc._cache.clear()


def _answer(behind=2, own=0, base=B):
    return {"merge_base_commit": {"sha": base}, "ahead_by": behind, "behind_by": own, "files": [], "commits": []}


def test_compare_reads_main_view_and_asks_public_api_without_token():
    urls = []
    res = mc.compare(A, fetch=lambda u: urls.append(u) or _answer(behind=2, own=120))
    assert urls == [f"https://api.github.com/repos/humanitylabs-org/wizard-ux/compare/{A}...main?per_page=1&page=2"]
    assert res["commit"] == A and res["base"] == B and res["behind"] == 2 and res["own"] == 120
    assert "checked_at" in res and "error" not in res


def test_one_request_per_hour_per_commit_and_failures_retry_after_ten_minutes():
    t, calls = [1000.0], []
    now = lambda: t[0]  # noqa: E731
    ok = lambda u: calls.append(u) or _answer()  # noqa: E731
    mc.compare(A, fetch=ok, now=now)
    t[0] += 3599
    mc.compare(A, fetch=ok, now=now)
    assert len(calls) == 1
    t[0] += 2
    mc.compare(A, fetch=ok, now=now)
    assert len(calls) == 2

    def down(u):
        calls.append(u)
        raise OSError("offline")
    assert mc.compare(B, fetch=down, now=now) == {"commit": B, "error": "couldn't reach GitHub"}
    t[0] += 599
    mc.compare(B, fetch=down, now=now)
    assert len(calls) == 3
    t[0] += 2
    assert mc.compare(B, fetch=ok, now=now)["behind"] == 2


def test_unknown_commit_and_odd_answers_are_errors_not_numbers():
    def missing(u):
        raise urllib.error.HTTPError(u, 404, "Not Found", {}, io.BytesIO(b""))
    assert mc.compare(A, fetch=missing)["error"] == "not on GitHub"
    assert "behind" not in mc.compare(B, fetch=lambda u: {"ahead_by": "2"})
    for bad in ["", "main", A[:12], A.upper(), A + "0", "../x"]:
        with pytest.raises(ValueError):
            mc.compare(bad, fetch=lambda u: pytest.fail("never asks GitHub for a bad commit"))


def test_store_listener_main_compare_route_is_owner_only_get(tmp_path, monkeypatch):
    from wam import cli, store_install as si

    h = tmp_path / "wos"; h.mkdir()
    (h / "links.json").write_text(json.dumps({"host": "box.ts.net"}))
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@example.com")
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.ts.net:8444"})
    monkeypatch.setattr(mc, "_fetch", lambda u, timeout=8: _answer(behind=3, own=1))

    class C:
        home = h
    sent = []

    def call(method, path, **hdr):
        hd = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "owner@example.com", "Origin": "https://box.ts.net:8444", "X-Wizard-Store": "1",
              "Content-Type": "application/json", "Content-Length": "2", **hdr}
        o = cli.StoreHandler.__new__(cli.StoreHandler)
        o.cfg, o.path, o.client_address = C(), path, ("127.0.0.1", 5555)
        o.headers = {k: v for k, v in hd.items() if v is not None}
        o.rfile = io.BytesIO(b"{}")
        o._json = lambda code, b: sent.append((code, b))
        o._go(method)
        return sent[-1]

    code, b = call("GET", f"/_main-compare?commit={A}")
    assert code == 200 and b["behind"] == 3 and b["own"] == 1 and b["base"] == B
    assert call("GET", "/_main-compare?commit=HEAD")[0] == 400
    assert call("GET", "/_main-compare")[0] == 400
    assert call("GET", f"/_main-compare?commit={A}", **{"Tailscale-User-Login": "intruder@example.com"})[0] == 403
    assert call("POST", f"/_main-compare?commit={A}")[0] == 405


def test_far_behind_box_reads_summary_not_a_megabytes_file_list(monkeypatch):
    # GitHub's page 1 carries up to 300 files with patches (1.2 MB for 514 commits on wizard-ux, more for big
    # diffs); past the 4 MiB read cap the JSON is cut off and About said "couldn't reach GitHub". Page 2 of a
    # one-commit page carries the same ahead_by/behind_by/merge_base_commit and no "files" (wizard-ux#887).
    summary = {"status": "diverged", "ahead_by": 514, "behind_by": 7, "total_commits": 514,
               "merge_base_commit": {"sha": B}, "commits": [{"sha": "c" * 40}]}
    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        url = req.full_url
        seen.append(url)
        if "page=2" in url.split("?", 1)[1].split("&"):
            return Resp(json.dumps(summary).encode())
        big = dict(summary, files=[{"filename": f"f{i}", "patch": "x" * 20000} for i in range(300)])
        body = json.dumps(big).encode()
        assert len(body) > mc.MAX_BYTES
        return Resp(body)

    monkeypatch.setattr(mc.urllib.request, "urlopen", urlopen)
    res = mc.compare(A)
    assert "error" not in res, res
    assert res["behind"] == 514 and res["own"] == 7 and res["base"] == B
    assert len(seen) == 1
