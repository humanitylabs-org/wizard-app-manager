"""Synthetic installation identity and token/account association regressions; no GitHub calls."""
import json
import stat
import threading
from pathlib import Path

import pytest

from wam import github_signin as gs, private, registry

OLD = "fixture-only-old-credential"
NEW = "fixture-only-new-credential"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "owner"))
    monkeypatch.delenv("WIZARD_OS_HOME", raising=False)
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gs, "_flow", None)
    monkeypatch.setattr(gs, "_gh_cache", None)
    monkeypatch.setattr(gs, "_gh_token", lambda: "")
    monkeypatch.setattr(private.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(gs, "switch_on", lambda: False)
    monkeypatch.setattr(gs, "_user_login", lambda tok: {OLD: "old-account", NEW: "new-account"}.get(tok, ""))
    gs._login_cache.clear()
    yield
    gs._login_cache.clear()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)


def finish(tok):
    flow = gs.Flow(state="pending", stop=threading.Event())
    gs._flow = flow
    gs._finish(flow, tok)
    return flow


def select(kind, tmp_path, monkeypatch):
    if kind == "custom":
        monkeypatch.setenv("WIZARD_OS_HOME", str(tmp_path / "custom"))
    elif kind == "tilde":
        monkeypatch.setenv("WIZARD_OS_HOME", "~/custom")
    elif kind == "registry":
        monkeypatch.setattr(registry, "home", lambda: tmp_path / "registry-home")
    return registry.home()


@pytest.mark.parametrize("kind", ["default", "custom", "tilde", "registry"])
def test_selected_install_reads_writes_and_signs_out_without_foreign_mutation(kind, tmp_path, monkeypatch):
    selected = select(kind, tmp_path, monkeypatch)
    legacy = Path.home() / ".wizard-os/github-token"
    if selected != legacy.parent:
        put(legacy, OLD)
        put(legacy.with_name("github-login"), "old-account\n")
        before = [(p, p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                  for p in (legacy, legacy.with_name("github-login"))]
    else:
        before = []
    assert private.token_file() == selected / "github-token"
    finish(NEW)
    assert (selected / "github-token").read_text().strip() == NEW
    assert gs.current() == ("file", NEW) and private.token() == NEW
    assert gs.status()["login"] == "new-account"
    gs.signout()
    assert not (selected / "github-token").exists()
    assert not (selected / "github-login").exists()
    for path, content, mode in before:
        assert path.read_bytes() == content
        assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("kind", ["default", "custom"])
def test_existing_hand_placed_owner_token_and_legacy_sidecar_are_preserved_until_signout(kind, tmp_path, monkeypatch):
    selected = select(kind, tmp_path, monkeypatch)
    tok, label = selected / "github-token", selected / "github-login"
    put(tok, OLD + "\n")
    put(label, "unverified-previous-account\n")
    before = (tok.read_bytes(), label.read_bytes())
    assert private.token() == OLD
    assert gs.status() == {"state": "signed_in", "login": "old-account", "source": "file"}
    assert (tok.read_bytes(), label.read_bytes()) == before  # status does not migrate owner files
    gs.signout()
    assert not tok.exists() and not label.exists()


def test_legacy_default_read_is_explicitly_nonremovable_in_custom_install(tmp_path, monkeypatch):
    select("custom", tmp_path, monkeypatch)
    legacy = Path.home() / ".wizard-os/github-token"
    put(legacy, OLD)
    put(legacy.with_name("github-login"), "foreign-account\n")
    assert private.token() == OLD
    assert gs.current() == ("env", OLD)  # existing external/nonremovable source, not this install's file
    assert gs.status() == {"state": "signed_in", "login": "old-account", "source": "env"}
    assert gs.signout()["source"] == "env"
    assert legacy.read_text() == OLD and legacy.with_name("github-login").read_text() == "foreign-account\n"
    assert not registry.home().exists()


@pytest.mark.parametrize("key", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_environment_override_agrees_with_private_reader_and_survives_signout(key, tmp_path, monkeypatch):
    selected = select("custom", tmp_path, monkeypatch)
    put(selected / "github-token", OLD)
    monkeypatch.setenv(key, NEW)
    assert private.token() == NEW and gs.current() == ("env", NEW)
    assert gs.status()["login"] == "new-account"
    assert gs.signout() == {"state": "signed_in", "login": "new-account", "source": "env"}
    assert not (selected / "github-token").exists()
    assert private.token() == NEW


@pytest.mark.parametrize("old_sidecar", [False, True])
def test_manual_token_replacement_cannot_display_previous_account(old_sidecar):
    finish(OLD)
    if old_sidecar:
        put(gs.login_path(), "old-account\n")
    put(gs.token_path(), NEW)
    assert gs.status() == {"state": "signed_in", "login": "new-account", "source": "file"}
    assert private.token() == NEW


def test_failed_token_publication_after_sidecar_publication_cannot_mislabel_old_token(monkeypatch):
    finish(OLD)
    real = gs._write_private
    published = []

    def fail_token(path, text):
        if path == gs.token_path():
            raise OSError("synthetic token publication failure")
        real(path, text)
        published.append(path)

    monkeypatch.setattr(gs, "_write_private", fail_token)
    flow = finish(NEW)
    assert published == [gs.login_path()]  # fault is specifically after sidecar publication
    assert flow["state"] == "error" and flow["stop"].is_set()
    assert private.token() == OLD
    assert gs.status() == {"state": "signed_in", "login": "old-account", "source": "file"}


def test_matching_bound_label_does_not_require_network_and_never_contains_token(monkeypatch):
    finish(OLD)
    sidecar = json.loads(gs.login_path().read_text())
    assert sidecar["login"] == "old-account" and OLD not in gs.login_path().read_text()
    monkeypatch.setattr(gs, "_user_login", lambda tok: pytest.fail("matching label must not call GitHub"))
    assert gs.status()["login"] == "old-account"


@pytest.mark.parametrize("value", ["not-json", "[]", "null", '{"login":"old-account","token_sha256":17}'])
def test_unbound_or_malformed_label_is_not_trusted(value):
    put(gs.token_path(), NEW)
    put(gs.login_path(), value)
    assert gs.status()["login"] == "new-account"


def test_legacy_label_is_hidden_when_active_token_cannot_be_verified(monkeypatch):
    put(gs.token_path(), OLD)
    put(gs.login_path(), "unverified-account\n")
    monkeypatch.setattr(gs, "_user_login", lambda tok: "")
    assert gs.status() == {"state": "signed_in", "login": "", "source": "file"}
