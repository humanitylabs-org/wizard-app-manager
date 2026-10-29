"""Fresh mode: the owner's install approves the folder share between two default apps (the Hermes Agent app reads the
Claude app's Claude Code sign-in). Without it a fresh 0.3.83 box's agent ran with no Claude login: every chat failed with
"Claude Code is installed but has no usable login" right after the owner signed in (demo VPS, 30 Oct 2026)."""
from wam import cli


class FakeRuntipi:
    def __init__(self, proposals, stopped=()):
        self.proposals = proposals
        self.stopped = set(stopped)
        self.posts = []

    def app(self, urn):
        return {"app": {"status": "stopped" if urn.split(":")[0] in self.stopped else "running"}}

    def req(self, method, path, body=None):
        if method == "GET":
            app = path.split("/")[2].split(":")[0]
            return [g for g in self.proposals if g["from"] == app]
        self.posts.append((path, body))
        return {"grants": []}


def run(monkeypatch, proposals, installed, stopped=()):
    rt = FakeRuntipi(proposals, stopped)
    monkeypatch.setattr(cli, "installed_ids", lambda _rt: set(installed))
    return rt, cli.approve_default_shares(None, rt)


def test_default_agent_gets_the_default_claude_sign_in(monkeypatch):
    share = {"kind": "share", "from": "hermes-agent", "to": "wizard-claude", "share": "claude-login", "reason": "mounts"}
    rt, done = run(monkeypatch, [share], {"hermes-agent", "wizard-claude", "files"})
    assert done == [{"from": "hermes-agent", "to": "wizard-claude", "share": "claude-login"}]
    assert rt.posts == [("/apps/hermes-agent:wizardapps/share-grants", {"to": "wizard-claude", "share": "claude-login", "approve": True})]


def test_a_non_default_app_or_app_grant_is_never_approved(monkeypatch):
    rows = [
        {"kind": "share", "from": "hermes-agent", "to": "some-store-app", "share": "secrets"},
        {"kind": "share", "from": "some-store-app", "to": "wizard-claude", "share": "claude-login"},
        {"kind": "app", "from": "hermes-agent", "to": "wizard-claude"},
    ]
    rt, done = run(monkeypatch, rows, {"hermes-agent", "wizard-claude", "some-store-app"})
    assert done == [] and rt.posts == []


def test_nothing_before_both_apps_are_installed(monkeypatch):
    share = {"kind": "share", "from": "hermes-agent", "to": "wizard-claude", "share": "claude-login"}
    rt, done = run(monkeypatch, [share], {"hermes-agent"})
    assert done == [] and rt.posts == []


def test_a_stopped_consumer_is_left_stopped(monkeypatch):
    # Approving recreates the consumer (compose down + up --force-recreate); an app the owner stopped must not start.
    share = {"kind": "share", "from": "hermes-agent", "to": "wizard-claude", "share": "claude-login"}
    rt, done = run(monkeypatch, [share], {"hermes-agent", "wizard-claude"}, stopped={"hermes-agent"})
    assert done == [] and rt.posts == []
