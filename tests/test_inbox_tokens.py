"""#804: every installed app whose recipe asks for a `*_AGENT_INBOX_TOKEN` gets its own Agent Service app token
(app-tokens/<app id> in the Service's WIZARD_OS_HOME) in its user-config app.env, so its calls to the agent inbox
stop answering 403. Created once (0600, the Service's user), kept across updates, re-synced after a restore."""
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import cli, inbox_tokens as it  # noqa: E402

MAIL = """services:
  wizard-mail:
    image: ghcr.io/humanitylabs-org/wizard-mail:1
    environment:
      MAIL_AGENT_SERVICE: ${MAIL_AGENT_SERVICE:-http://wizard-agent-service:8787}
      MAIL_AGENT_INBOX_TOKEN: ${MAIL_AGENT_INBOX_TOKEN:-}
"""
MESSENGER = """services:
  messenger:
    environment:
      MESSENGER_AGENT_INBOX_TOKEN: ${MESSENGER_AGENT_INBOX_TOKEN:-}
      MESSENGER_AGENT_SERVICE: ${MESSENGER_AGENT_SERVICE:-http://wizard-agent-service:8787}
"""
PLAIN = "services:\n  notes:\n    environment:\n      NOTES_PORT: ${APP_PORT}\n"


class LocalFiles:
    """RuntipiFiles on the test's own disk: records each write's (name, mode, uid, gid) instead of chowning."""

    def __init__(self, owners=None):
        self.writes = []
        self.owners = owners or {}

    def read(self, p):
        return Path(p).read_bytes() if Path(p).is_file() else None

    def stat(self, p):
        p = Path(p)
        if not p.exists() or p.is_symlink():
            return None
        st = p.stat()
        uid, gid = self.owners.get(str(p), (st.st_uid, st.st_gid))
        return stat.S_IMODE(st.st_mode), uid, gid

    def write(self, p, data, mode, uid, gid):
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.writes.append((p.name, mode, uid, gid))
        p.write_bytes(data)
        os.chmod(p, mode)


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "runtipi"
    os_home = root / "app-data" / "wizardapps" / "wizard-agent-service" / "data" / "wizard-os"
    os_home.mkdir(parents=True)
    for app_id, text in (("wizard-mail", MAIL), ("messenger", MESSENGER), ("wizard-notes", PLAIN)):
        d = root / "apps" / "wizardapps" / app_id
        d.mkdir(parents=True)
        (d / "docker-compose.yml").write_text(text)
    files = LocalFiles({str(os_home): (1000, 1001)})   # the Service runs as 1000:1001 here
    return SimpleNamespace(root=root, os_home=os_home, tokens=os_home / "app-tokens", files=files)


def user_env(box, app_id):
    p = box.root / "user-config" / "wizardapps" / app_id / "app.env"
    return dict(l.split("=", 1) for l in p.read_text().splitlines() if "=" in l) if p.is_file() else {}


def reg(*ids):
    return {"apps": [{"id": i, "urn": f"{i}:wizardapps"} for i in ("wizard-agent-service", *ids)]}


def test_token_vars_reads_only_agent_inbox_tokens():
    assert it.token_vars(MAIL) == ["MAIL_AGENT_INBOX_TOKEN"]
    assert it.token_vars(MESSENGER + MESSENGER) == ["MESSENGER_AGENT_INBOX_TOKEN"]
    assert it.token_vars(PLAIN) == []
    assert it.token_vars("X: ${MESSENGER_AGENT_INBOX_TOKEN_FILE:-}") == []


def test_mail_and_messenger_each_get_their_own_token_the_service_accepts(box):
    res = it.sync(reg("wizard-mail", "messenger", "wizard-notes"), box.root, files=box.files)
    assert set(res["apps"]) == {"wizard-mail", "messenger"}   # Notes asks for none: untouched
    mail = (box.tokens / "wizard-mail").read_text().strip()
    msg = (box.tokens / "messenger").read_text().strip()
    # the Service's appForToken: >= 24 chars; ensureToken makes 32 random bytes base64url (43 chars)
    assert len(mail) == 43 and len(msg) == 43 and mail != msg
    assert user_env(box, "wizard-mail") == {"MAIL_AGENT_INBOX_TOKEN": mail}
    assert user_env(box, "messenger") == {"MESSENGER_AGENT_INBOX_TOKEN": msg}
    assert not (box.root / "user-config" / "wizardapps" / "wizard-notes").exists()
    assert ("wizard-mail", 0o600, 1000, 1001) in box.files.writes   # owned by the Service's user, private
    p = box.root / "user-config" / "wizardapps" / "wizard-mail" / "app.env"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert res["apps"]["wizard-mail"] == {"vars": ["MAIL_AGENT_INBOX_TOKEN"], "token": "created", "changed": True}


def test_an_existing_service_token_is_kept_and_a_second_sync_changes_nothing(box):
    box.tokens.mkdir()
    (box.tokens / "wizard-mail").write_text("delivery-token-the-service-made-0123456789\n")
    (box.root / "user-config" / "wizardapps" / "wizard-mail").mkdir(parents=True)
    (box.root / "user-config" / "wizardapps" / "wizard-mail" / "app.env").write_text("OWNER_SETTING=1\n")
    first = it.sync_app(box.root, "wizard-mail", files=box.files)
    assert first == {"vars": ["MAIL_AGENT_INBOX_TOKEN"], "token": "kept", "changed": True}
    assert box.files.writes == []   # never replaced
    assert user_env(box, "wizard-mail") == {"OWNER_SETTING": "1",
                                            "MAIL_AGENT_INBOX_TOKEN": "delivery-token-the-service-made-0123456789"}
    again = it.sync_app(box.root, "wizard-mail", files=box.files)
    assert again["changed"] is False and box.files.writes == []


def test_after_a_restore_brings_back_another_token_the_app_gets_that_one(box):
    it.sync_app(box.root, "messenger", files=box.files)
    (box.tokens / "messenger").write_text("restored-token-from-the-backup-abcdefghij\n")
    r = it.sync_app(box.root, "messenger", files=box.files)
    assert r["changed"] is True
    assert user_env(box, "messenger")["MESSENGER_AGENT_INBOX_TOKEN"] == "restored-token-from-the-backup-abcdefghij"


def test_a_lost_service_file_adopts_the_token_the_app_already_holds(box):
    it.sync_app(box.root, "messenger", files=box.files)
    held = user_env(box, "messenger")["MESSENGER_AGENT_INBOX_TOKEN"]
    (box.tokens / "messenger").unlink()   # e.g. the Agent Service was reinstalled
    r = it.sync_app(box.root, "messenger", files=box.files)
    assert r == {"vars": ["MESSENGER_AGENT_INBOX_TOKEN"], "token": "adopted", "changed": False}
    assert (box.tokens / "messenger").read_text().strip() == held


def test_a_file_the_service_would_refuse_is_left_alone(box):
    box.tokens.mkdir()
    (box.tokens / "wizard-mail").write_text("short\n")
    r = it.sync_app(box.root, "wizard-mail", files=box.files)
    assert "skipped" in r and box.files.writes == [] and user_env(box, "wizard-mail") == {}


def test_nothing_happens_before_the_service_has_a_data_folder(tmp_path):
    root = tmp_path / "runtipi"
    (root / "apps" / "wizardapps" / "wizard-mail").mkdir(parents=True)
    (root / "apps" / "wizardapps" / "wizard-mail" / "docker-compose.yml").write_text(MAIL)
    r = it.sync_app(root, "wizard-mail", files=LocalFiles())
    assert r["skipped"].startswith("no Agent Service")
    assert not (root / "user-config").exists()


def test_an_update_uses_the_recipe_it_is_about_to_apply(box):
    repo = box.root / "repos" / "wizardapps" / "apps" / "wizard-notes"
    repo.mkdir(parents=True)
    (repo / "docker-compose.yml").write_text(PLAIN + "      NOTES_AGENT_INBOX_TOKEN: ${NOTES_AGENT_INBOX_TOKEN:-}\n")
    assert "skipped" in it.sync_app(box.root, "wizard-notes", files=box.files)   # installed recipe: none
    r = it.sync_app(box.root, "wizard-notes", files=box.files, prefer="store")
    assert r["vars"] == ["NOTES_AGENT_INBOX_TOKEN"] and r["changed"] is True


class FakeRt:
    def __init__(self, status):
        self.status, self.restarts = status, []

    def app(self, urn):
        return {"app": {"status": self.status.get(urn.split(":")[0])}}

    def restart(self, urn):
        self.restarts.append(urn)


def test_registry_hook_restarts_a_running_app_once_when_its_token_is_new(box):
    rt = FakeRt({"wizard-mail": "running", "messenger": "stopped"})
    c = SimpleNamespace(runtipi_root=box.root)
    res = it.sync_registry(c, rt, reg("wizard-mail", "messenger"), files=box.files)
    assert rt.restarts == ["wizard-mail:wizardapps"]   # a stopped app picks it up on its next start
    assert res["apps"]["wizard-mail"]["restarted"] is True
    it.sync_registry(c, rt, reg("wizard-mail", "messenger"), files=box.files)
    assert rt.restarts == ["wizard-mail:wizardapps"]   # nothing new: no second restart


def test_registry_hook_never_raises(box, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("docker is down")
    monkeypatch.setattr(it, "sync", boom)
    assert it.sync_registry(SimpleNamespace(runtipi_root=box.root), FakeRt({}), reg()) is None


def test_write_registry_and_update_call_the_hooks():
    src = Path(cli.__file__).read_text()
    assert "inbox_tokens.sync_registry(c, rt, reg)" in src
    assert src.count('inbox_tokens.before_start(c, app, prefer="store")') == 2   # app_update + bulk passthrough
    assert 'inbox_tokens.before_start(c, app_id, prefer="store")' in src            # install


def test_concurrent_first_syncs_hand_out_one_token(box):
    """Codex P1: two registry writers syncing one app at once must agree on one token."""
    import threading
    out = []
    ts = [threading.Thread(target=lambda: out.append(it.sync_app(box.root, "messenger", files=LocalFiles(
        {str(box.os_home): (1000, 1001)})))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r["token"] for r in out) == ["created"] + ["kept"] * 5
    assert user_env(box, "messenger")["MESSENGER_AGENT_INBOX_TOKEN"] == (box.tokens / "messenger").read_text().strip()


# #931: the Agent Service runs on the host (no Runtipi Service app), its WIZARD_OS_HOME holds app-tokens/.

@pytest.fixture
def host_box(tmp_path):
    root = tmp_path / "host-runtipi"
    for app_id, text in (("wizard-mail", MAIL), ("messenger", MESSENGER), ("wizard-notes", PLAIN)):
        d = root / "apps" / "wizardapps" / app_id
        d.mkdir(parents=True)
        (d / "docker-compose.yml").write_text(text)
    home = tmp_path / "wizard-os-home"
    (home / "app-tokens").mkdir(parents=True)
    (home / "app-tokens" / "mizel").write_text("r" * 43 + "\n")   # another app's token, untouched
    return SimpleNamespace(root=root, home=home, tokens=home / "app-tokens")


def test_a_host_service_home_with_app_tokens_is_found(host_box, tmp_path):
    assert it.service_home(host_box.root) is None                       # no Wizard home given: as before
    assert it.service_home(host_box.root, home=host_box.home) == host_box.home
    bare = tmp_path / "bare-home"
    bare.mkdir()
    assert it.service_home(host_box.root, home=bare) is None            # a Wizard home without app-tokens/ is not one


def test_a_runtipi_service_still_wins_over_the_host_home(box, host_box):
    assert it.service_home(box.root, home=host_box.home) == box.os_home


def test_host_service_apps_get_their_tokens_0600_and_running_ones_restart_once(host_box, capsys):
    rt = FakeRt({"wizard-mail": "running", "messenger": "running"})
    c = SimpleNamespace(runtipi_root=host_box.root, home=host_box.home)
    (host_box.tokens / "messenger").write_text("m" * 43 + "\n")      # Messenger's file already there: kept
    res = it.sync_registry(c, rt, reg("wizard-mail", "messenger", "wizard-notes"))   # real HostFiles
    assert res["apps"]["messenger"]["token"] == "kept"
    assert res["apps"]["wizard-mail"]["token"] == "created"
    assert "wizard-notes" not in res["apps"]
    mail = (host_box.tokens / "wizard-mail").read_text().strip()
    assert it.TOKEN.fullmatch(mail) and stat.S_IMODE((host_box.tokens / "wizard-mail").stat().st_mode) == 0o600
    for app_id, var, want in (("wizard-mail", "MAIL_AGENT_INBOX_TOKEN", mail),
                              ("messenger", "MESSENGER_AGENT_INBOX_TOKEN", "m" * 43)):
        env = host_box.root / "user-config" / "wizardapps" / app_id / "app.env"
        assert user_env(SimpleNamespace(root=host_box.root), app_id)[var] == want
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert sorted(rt.restarts) == ["messenger:wizardapps", "wizard-mail:wizardapps"]
    assert (host_box.tokens / "mizel").read_text() == "r" * 43 + "\n"
    assert not list(host_box.tokens.glob("*.wam-it.*"))
    it.sync_registry(c, rt, reg("wizard-mail", "messenger"))
    assert len(rt.restarts) == 2                                         # nothing new: no second restart
    err = capsys.readouterr().err
    assert mail not in err and "m" * 43 not in err                       # no token ever printed


def test_before_start_uses_the_host_service_home(host_box):
    it.before_start(SimpleNamespace(runtipi_root=host_box.root, home=host_box.home), "messenger", prefer="installed")
    assert it.TOKEN.fullmatch((host_box.tokens / "messenger").read_text().strip())


def test_no_service_home_says_so_once_on_stderr_and_in_doctor(host_box, tmp_path, capsys):
    bare = tmp_path / "bare-home"
    bare.mkdir()
    c = SimpleNamespace(runtipi_root=host_box.root, home=bare)
    res = it.sync_registry(c, FakeRt({}), reg("wizard-mail", "messenger", "wizard-notes"))
    assert res["apps"]["messenger"]["skipped"] == it.NO_SERVICE
    err = capsys.readouterr().err
    assert err.count("note: agent inbox tokens not handed to messenger, wizard-mail") == 1
    assert not (host_box.root / "user-config" / "wizardapps" / "messenger").exists()
    row = it.check(reg("wizard-mail", "messenger", "wizard-notes"), host_box.root, bare)
    assert row[0] == it.LABEL and row[1] is False and "messenger, wizard-mail" in row[2]


def test_doctor_row_turns_green_once_every_asking_app_has_its_token(host_box):
    r = reg("wizard-mail", "messenger", "wizard-notes")
    assert it.check(r, host_box.root, host_box.home)[1] is False          # tokens not handed out yet
    it.sync(r, host_box.root, home=host_box.home)
    row = it.check(r, host_box.root, host_box.home)
    assert row == (it.LABEL, True, "messenger, wizard-mail")
    assert it.check(reg("wizard-notes"), host_box.root, host_box.home) is None   # no app asks: no row


def test_doctor_lists_the_inbox_token_row():
    src = Path(cli.__file__).read_text()
    assert "out.extend(doctor_inbox_tokens(c, json.loads(regp.read_text())))" in src
