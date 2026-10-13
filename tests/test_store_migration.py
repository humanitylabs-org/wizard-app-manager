"""Regression tests for narrowly scoped legacy-store migration."""
import pytest

from wam import cli
from test_wam import _Cfg, _FakeStores


@pytest.mark.parametrize("legacy_url", ["https://apps.aiwizards.com", "https://apps.aiwizards.com/"])
def test_removes_only_known_legacy_slug(legacy_url):
    rt = _FakeStores([
        {"slug": "migrated", "url": "https://github.com/runtipi/runtipi-appstore"},
        {"slug": "wizard-apps", "url": legacy_url},
    ])
    cli.ensure_store(_Cfg(), rt)
    assert rt.calls == [("DELETE", "/marketplace/wizard-apps"),
                        ("POST", "/marketplace/create"), ("PATCH", "/marketplace/wizardapps")]
    rt.calls.clear()
    cli.ensure_store(_Cfg(), rt)
    assert rt.calls == []


def test_unknown_store_with_same_url_is_not_deleted():
    rt = _FakeStores([{"slug": "owner-catalog", "url": _Cfg.index}])
    before = rt.stores()
    with pytest.raises(RuntimeError, match="409"):
        cli.ensure_store(_Cfg(), rt)
    assert rt.stores() == before
    assert rt.calls == [("POST", "/marketplace/create")]


def test_legacy_slug_with_unrelated_url_is_preserved():
    rt = _FakeStores([{"slug": "wizard-apps", "url": "https://example.com/owner-catalog"}])
    cli.ensure_store(_Cfg(), rt)
    assert rt.stores()[0]["url"] == "https://example.com/owner-catalog"
    assert all(method != "DELETE" for method, _ in rt.calls)


@pytest.mark.parametrize("reason", ["APP_STORE_DELETE_ERROR_APPS_EXIST", "APP_STORE_DELETE_ERROR_LAST_STORE"])
def test_backend_deletion_guard_stops_migration(reason):
    class GuardedStores(_FakeStores):
        def req(self, method, path, body=None, **kwargs):
            if method == "DELETE":
                self.calls.append((method, path))
                raise RuntimeError(reason)
            return super().req(method, path, body, **kwargs)

    rt = GuardedStores([{"slug": "wizard-apps", "url": _Cfg.index}])
    before = rt.stores()
    with pytest.raises(RuntimeError, match=reason):
        cli.ensure_store(_Cfg(), rt)
    assert rt.stores() == before
    assert rt.calls == [("DELETE", "/marketplace/wizard-apps")]


@pytest.mark.parametrize("enabled", [True, False])
def test_canonical_store_is_never_removed(enabled):
    rt = _FakeStores([{"slug": "wizardapps", "name": "Custom label", "url": _Cfg.index, "enabled": enabled},
                      {"slug": "wizard-apps", "url": _Cfg.index}])
    cli.ensure_store(_Cfg(), rt)
    assert rt.calls == ([] if enabled else [("PATCH", "/marketplace/wizardapps")])
    assert {s["slug"] for s in rt.stores()} == {"wizardapps", "wizard-apps"}
