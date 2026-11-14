"""About's public metadata read reuses the loopback API; Store controls stay root-peer gated."""
import io
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from wam import cli, main_compare, store_install

SHA = "a" * 40


def call(method="GET", path=f"/api/main-compare?commit={SHA}", headers=None):
    h = cli.Handler.__new__(cli.Handler)
    h.cfg = SimpleNamespace()
    h.path = path
    h.token = "test-only-existing-write-token"
    h.headers = Message()
    for name, value in (headers or {}).items():
        h.headers[name] = value
    h.rfile = io.BytesIO(b"{}")
    result = []
    h._json = lambda code, body: result.append((code, body))
    getattr(h, "do_" + method)()
    return result[-1]


def test_existing_compare_engine_on_exact_read_route(monkeypatch):
    seen = []
    expected = {"commit": SHA, "base": SHA, "behind": 4027, "own": 0, "checked_at": "fixed-test-time"}
    monkeypatch.setattr(main_compare, "compare", lambda c: seen.append(c) or expected)
    assert call() == (200, expected)
    assert seen == [SHA]


@pytest.mark.parametrize("query", ["", "commit=", "commit=HEAD", "commit=" + SHA.upper(),
    "commit=" + SHA[:-1], "commit=" + SHA + "0", "commit=" + SHA + "&commit=" + SHA,
    "commit=" + SHA + "&repo=foreign/repo", "commit=%61" + SHA[1:], "commit=" + SHA + "&", "Commit=" + SHA])
def test_bad_or_extra_parameters_never_query_github(monkeypatch, query):
    monkeypatch.setattr(main_compare, "compare", lambda c: pytest.fail("invalid input reached compare"))
    assert call(path="/api/main-compare?" + query)[0] == 400


@pytest.mark.parametrize("suffix", ["/", "/other", "-other"])
def test_not_a_prefix_route(monkeypatch, suffix):
    monkeypatch.setattr(main_compare, "compare", lambda c: pytest.fail("neighbor reached compare"))
    assert call(path="/api/main-compare" + suffix + "?commit=" + SHA)[0] == 404


def test_foreign_fetch_site_is_rejected(monkeypatch):
    monkeypatch.setattr(main_compare, "compare", lambda c: pytest.fail("cross-site reached compare"))
    assert call(headers={"Sec-Fetch-Site": "cross-site"})[0] == 403


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("path", ["/api/main-compare", "/api/install", "/api/updates", "/api/updates/apply", "/api/app-updates"])
def test_write_auth_unchanged_no_identity_headers_authorize(method, path):
    assert call(method=method, path=path, headers={"Tailscale-User-Login": "owner@example.com",
        "X-Wizard-Owner-Proof": "forged", "X-Forwarded-For": "100.64.0.2"})[0] == 401


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", ["/_main-compare", "/some-app", "/_updates", "/_remove/some-app"])
def test_store_root_peer_gate_is_not_bypassed(monkeypatch, method, path):
    monkeypatch.setattr(store_install, "peer_uid", lambda port: 1000)
    h = cli.StoreHandler.__new__(cli.StoreHandler)
    h.path, h.client_address = path + "?commit=" + SHA, ("127.0.0.1", 59999)
    h.headers = {"Tailscale-User-Login": "owner@example.com", "X-Wizard-Owner-Proof": "forged"}
    h.cfg = SimpleNamespace()
    result = []
    h._json = lambda code, body: result.append((code, body))
    h._go(method)
    assert result[0][0] == 403
