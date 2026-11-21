"""Box overrides written by hand are retired by the update (wizard-os#805)."""
import json
from pathlib import Path

import pytest

from wam import hand_overrides as ho

STORE = "wizardapps"


class FakeRt:
    def __init__(self, installed=(), status="running"):
        self.rows = [{"app": {"appName": a, "appStoreSlug": STORE, "status": "running"}} for a in installed]
        self.status = status
        self.calls = []

    def installed(self):
        return self.rows

    def app(self, urn):
        return {"app": {"status": self.status}}

    def stop(self, urn):
        self.calls.append(("stop", urn))
        self.status = "stopped"

    def wait_status(self, urn, want, timeout=0):
        return self.status

    def start(self, urn):
        self.calls.append(("start", urn))
        self.status = "running"


def _override(root, app, text):
    p = ho.override_path(root, STORE, app)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _asides(p):
    return sorted(x.name for x in p.parent.iterdir() if x.name.startswith("docker-compose.yml.hand-"))


SHELL = """# Lock the shell down on the backup box: no Hermes home, no chat database read
services:
  wizard-shell:
    volumes: !override
      - /dev/null:/hermes/.keep:ro
"""


def test_the_bundle_override_moves_aside_when_the_bundle_is_not_installed(tmp_path):
    p = _override(tmp_path, "wizard-shell", SHELL)
    out = ho.retire(tmp_path, STORE, FakeRt(installed=["wizard-ux", "hermes-agent"]), tmp_path / "home")
    assert not p.exists()
    assert [r["app"] for r in out] == ["wizard-shell"]
    (kept,) = _asides(p)
    assert (p.parent / kept).read_text() == SHELL   # nothing lost: put it back to undo
    hist = json.loads((tmp_path / "home" / ho.STATE_FILE).read_text())
    assert hist[-1]["steps"][0]["moved_aside"].endswith(kept)


def test_the_bundle_override_stays_while_the_bundle_is_installed(tmp_path):
    p = _override(tmp_path, "wizard-shell", SHELL)
    assert ho.retire(tmp_path, STORE, FakeRt(installed=["wizard-shell"])) == []
    assert p.read_text() == SHELL


def test_unreadable_installed_list_never_moves_the_bundle_override(tmp_path):
    p = _override(tmp_path, "wizard-shell", SHELL)

    class Broken(FakeRt):
        def installed(self):
            raise OSError("runtipi down")
    out = ho.retire(tmp_path, STORE, Broken())
    assert p.read_text() == SHELL
    assert out[0]["skipped"].startswith("installed apps not readable")


AGENT = """services:
  hermes-agent-engine:
    ports:
      - 127.0.0.1:18642:8642
      - "127.0.0.1:9119:9119"
    volumes:
      - type: bind
        source: /srv/runtipi/app-data/wizardapps/wizard-agent-service/data/wizard-os/skills
        target: /opt/wizard-os/skills
        read_only: true
    networks: !override
      - wizard-core
"""


def test_the_published_hermes_api_port_is_removed_and_everything_else_kept(tmp_path):
    p = _override(tmp_path, "hermes-agent", AGENT)
    out = ho.retire(tmp_path, STORE, FakeRt(installed=["hermes-agent"]))
    assert out[0]["removed_ports"] == ["hermes-agent-engine: 127.0.0.1:18642:8642"]
    from wam.network import load_yaml
    svc = load_yaml(p.read_text())["services"]["hermes-agent-engine"]
    assert svc["ports"] == ["127.0.0.1:9119:9119"]
    assert svc["volumes"][0]["target"] == "/opt/wizard-os/skills"
    assert "!override" in p.read_text()            # the App Manager's network block keeps its tag
    (kept,) = _asides(p)
    assert (p.parent / kept).read_text() == AGENT
    assert ho.retire(tmp_path, STORE, FakeRt(installed=["hermes-agent"])) == []   # once


@pytest.mark.parametrize("entry", ["18642:8642", "8642", "127.0.0.1:18642:8642/tcp",
                                   "{target: 8642, published: '18642', host_ip: 127.0.0.1}"])
def test_every_form_of_the_api_port_counts(tmp_path, entry):
    p = _override(tmp_path, "hermes-agent", f"services:\n  hermes-agent-engine:\n    ports:\n      - {entry}\n")
    out = ho.retire(tmp_path, STORE, FakeRt())
    assert out[0]["app"] == "hermes-agent"
    assert not p.exists() and len(_asides(p)) == 1   # nothing else was in it: the whole file goes aside


def test_an_agent_override_without_the_api_port_is_untouched(tmp_path):
    text = "services:\n  hermes-agent-engine:\n    ports: !reset []\n"
    p = _override(tmp_path, "hermes-agent", text)
    assert ho.retire(tmp_path, STORE, FakeRt()) == []
    assert p.read_text() == text and _asides(p) == []


def _box(tmp_path, override):
    ledger = tmp_path / "home" / "marketplace"
    (ledger / "chats").mkdir(parents=True)
    (ledger / "ledger.json").write_text('{"items": [{"id": "a"}]}')
    (ledger / "ledger.lock").write_text("")
    (ledger / "chats" / "c1.json").write_text("[]")
    root = tmp_path / "runtipi"
    p = _override(root, "second-hand", override.replace("@HOST@", str(ledger)))
    return root, ledger, p


SECOND = """services:
  second-hand:
    environment:
      LEDGER_DIR: /ledger
      KEEP: '1'
    volumes:
      - @HOST@:/ledger
      - /etc/localtime:/etc/localtime:ro
"""


def test_second_hand_gets_its_own_ledger_and_host_scripts_keep_working(tmp_path):
    root, ledger, p = _box(tmp_path, SECOND)
    rt = FakeRt(installed=["second-hand"])
    (out,) = ho.retire(root, STORE, rt, tmp_path / "state")
    own = root / "app-data" / STORE / "second-hand" / "data" / "private" / "ledger"
    assert out["moved"] is True and out["to"] == str(own)
    assert json.loads((own / "ledger.json").read_text()) == {"items": [{"id": "a"}]}
    assert (own / "chats" / "c1.json").is_file()
    assert ledger.is_symlink() and ledger.resolve() == own.resolve()   # the host path still reaches the ledger
    assert Path(out["host_folder_kept_as"]).is_dir()                   # the original folder, kept
    from wam.network import load_yaml
    svc = load_yaml(p.read_text())["services"]["second-hand"]
    assert svc["volumes"] == ["/etc/localtime:/etc/localtime:ro"]
    assert svc["environment"] == {"KEEP": "1"}                         # LEDGER_DIR goes: the app's default is it
    assert rt.calls == [("stop", "second-hand:wizardapps"), ("start", "second-hand:wizardapps")]
    assert ho.retire(root, STORE, FakeRt(installed=["second-hand"])) == []   # once


def test_second_hand_mount_over_the_default_ledger_moves_the_whole_file_aside(tmp_path):
    root, ledger, p = _box(tmp_path, "services:\n  second-hand:\n    volumes:\n      - @HOST@:/data/private/ledger:rw\n")
    rt = FakeRt(status="stopped")
    (out,) = ho.retire(root, STORE, rt)
    assert out["moved"] is True and "moved_aside" in out and not p.exists()
    assert rt.calls == []    # a stopped app stays stopped


def test_second_hand_never_overwrites_a_ledger_it_already_has(tmp_path):
    root, ledger, p = _box(tmp_path, SECOND)
    own = root / "app-data" / STORE / "second-hand" / "data" / "private" / "ledger"
    own.mkdir(parents=True)
    (own / "ledger.json").write_text("{}")
    rt = FakeRt()
    (out,) = ho.retire(root, STORE, rt)
    assert "skipped" in out and rt.calls == []
    assert ledger.is_dir() and not ledger.is_symlink() and (own / "ledger.json").read_text() == "{}"
    assert _asides(p) == []


def test_second_hand_failure_puts_everything_back(tmp_path, monkeypatch):
    root, ledger, p = _box(tmp_path, SECOND)
    before = p.read_text()
    real = ho._rewrite

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(ho, "_rewrite", boom)
    rt = FakeRt()
    (out,) = ho.retire(root, STORE, rt)
    monkeypatch.setattr(ho, "_rewrite", real)
    assert out["rolled_back"] is True and "disk full" in out["error"]
    assert ledger.is_dir() and not ledger.is_symlink() and (ledger / "ledger.json").is_file()
    assert not (root / "app-data" / STORE / "second-hand" / "data" / "private" / "ledger").exists()
    assert not list(ledger.parent.glob("*.moved-to-second-hand-*"))
    assert p.read_text() == before
    assert rt.calls[-1] == ("start", "second-hand:wizardapps")


def test_second_hand_that_will_not_stop_is_left_alone(tmp_path):
    root, ledger, p = _box(tmp_path, SECOND)

    class Stuck(FakeRt):
        def wait_status(self, urn, want, timeout=0):
            return "stopping"
    rt = Stuck()
    (out,) = ho.retire(root, STORE, rt)
    assert "could not stop" in out["error"]
    assert ledger.is_dir() and not ledger.is_symlink()


def test_dry_run_changes_nothing(tmp_path):
    root, ledger, p = _box(tmp_path, SECOND)
    _override(root, "hermes-agent", AGENT)
    _override(root, "wizard-shell", SHELL)
    out = ho.retire(root, STORE, FakeRt(), tmp_path / "state", dry_run=True)
    assert [r["app"] for r in out] == ["wizard-shell", "hermes-agent", "second-hand"]
    assert all("would" in r for r in out)
    assert ledger.is_dir() and not ledger.is_symlink()
    assert not (tmp_path / "state").exists()


def test_a_broken_override_is_reported_and_kept(tmp_path):
    p = _override(tmp_path, "hermes-agent", "services: [unclosed\n")
    (out,) = ho.retire(tmp_path, STORE, FakeRt())
    assert "does not parse" in out["error"] and p.read_text() == "services: [unclosed\n"


def test_bootstrap_runs_the_step_before_the_release_moves_apps():
    from wam import cli
    src = Path(cli.__file__).read_text()
    assert src.index("hand = hand_overrides_step(c, rt)") < src.index("updated = release_updates(c, rt, failed)")


def test_the_step_never_fails_the_installer(monkeypatch):
    from types import SimpleNamespace
    from wam import cli, hand_overrides

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(hand_overrides, "retire", boom)
    assert cli.hand_overrides_step(SimpleNamespace(runtipi_root=Path("/nonexistent"), home=None), FakeRt()) == []
