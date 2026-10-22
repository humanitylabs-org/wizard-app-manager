"""wizard-os#138: the Store lists and installs first-party apps only until wizard-os#133."""
import pytest

from wam import index


def fake(apps):
    return lambda url: {"apps": apps}


APPS = [{"repo": "humanitylabs-org/wisdom", "publisher": "humanitylabs-org", "id": "wisdom"},
        {"repo": "oscarhenrycollins/browser", "publisher": "oscarhenrycollins", "id": "browser"},
        {"repo": "stranger/evil", "publisher": "stranger", "id": "evil"}]


def test_search_drops_third_party(monkeypatch):
    monkeypatch.delenv("WAM_PUBLISHERS", raising=False)
    monkeypatch.setattr(index, "_get", fake(APPS))
    assert [a["id"] for a in index.search("x")] == ["wisdom", "browser"]


def test_all_listed_drops_third_party(monkeypatch):
    monkeypatch.delenv("WAM_PUBLISHERS", raising=False)
    monkeypatch.setattr(index, "_get", fake(APPS))
    assert [a["id"] for a in index.all_listed("x")] == ["wisdom", "browser"]


def test_resolve_by_repo_refuses_third_party_before_fetching(monkeypatch):
    monkeypatch.delenv("WAM_PUBLISHERS", raising=False)
    monkeypatch.setattr(index, "_get", lambda url: pytest.fail("fetched a third-party record"))
    with pytest.raises(index.IndexError_, match="first-party"):
        index.resolve("x", "Stranger/evil")


def test_resolve_by_id_never_picks_third_party(monkeypatch):
    monkeypatch.delenv("WAM_PUBLISHERS", raising=False)
    monkeypatch.setattr(index, "_get", fake([APPS[2]]))
    with pytest.raises(index.IndexError_, match="no app"):
        index.resolve("x", "evil")


def test_owner_can_widen_or_disable(monkeypatch):
    monkeypatch.setattr(index, "_get", fake(APPS))
    monkeypatch.setenv("WAM_PUBLISHERS", "*")
    assert len(index.search("x")) == 3
    monkeypatch.setenv("WAM_PUBLISHERS", "stranger")
    assert [a["id"] for a in index.search("x")] == ["evil"]
