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
    assert urls == [f"https://api.github.com/repos/humanitylabs-org/wizard-ux/compare/{A}...main?per_page=1"]
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
