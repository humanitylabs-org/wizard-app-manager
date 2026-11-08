"""#400: `wam install` publishes links like Store install does, behind feature switch wizard/cli-install-links (off)."""
from types import SimpleNamespace

from wam import cli, feature_switches as fs


class FakePages:
    def __init__(self, doc):
        self.doc = doc

    def get(self):
        if isinstance(self.doc, Exception):
            raise self.doc
        return self.doc, "v1"


def _on(doc):
    return FakePages({"version": 1, "tweaks": {cli.CLI_INSTALL_LINKS: {"on": True, "at": 3}}, **doc})


def test_is_on_reads_the_look_switches(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "_channel", lambda home: "latest")
    assert fs.is_on(cli.CLI_INSTALL_LINKS, tmp_path, pages=_on({})) is True
    assert fs.is_on(cli.CLI_INSTALL_LINKS, tmp_path, pages=FakePages({"version": 1})) is False      # off by default
    off = FakePages({"version": 1, "tweaks": {cli.CLI_INSTALL_LINKS: {"on": False, "at": 4}}})
    assert fs.is_on(cli.CLI_INSTALL_LINKS, tmp_path, pages=off) is False
    assert fs.is_on(cli.CLI_INSTALL_LINKS, tmp_path, pages=FakePages(fs.UNREADABLE)) is False
    assert fs.is_on(cli.CLI_INSTALL_LINKS, tmp_path, pages=FakePages(fs.SwitchError("down", 503))) is False
    assert fs.is_on("not an id", tmp_path, pages=_on({})) is False


def test_is_on_is_locked_off_for_everyone(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "_channel", lambda home: "stable")
    assert fs.is_on(cli.CLI_INSTALL_LINKS, tmp_path, pages=_on({})) is False


def _box(tmp_path):
    return SimpleNamespace(home=tmp_path)


def test_off_publishes_nothing(tmp_path):
    calls, asked = [], []
    res = {"id": "search", "status": "running"}
    out = cli.cli_install_links(_box(tmp_path), res, publish=calls.append,
                                switch_on=lambda tid, home: asked.append(tid) or False)
    assert out == res and calls == [] and asked == [cli.CLI_INSTALL_LINKS]


def test_on_publishes_after_a_good_install(tmp_path):
    c = _box(tmp_path)
    for status in ("running", "stopped", "already installed"):
        calls = []
        out = cli.cli_install_links(c, {"id": "search", "status": status}, publish=calls.append,
                                    switch_on=lambda tid, home: True)
        assert calls == [c] and out["links"] == "refreshed", status


def test_on_skips_a_failed_or_pending_install(tmp_path):
    c = _box(tmp_path)
    for status in ("installing", "missing", "already handled"):
        calls = []
        out = cli.cli_install_links(c, {"id": "search", "status": status}, publish=calls.append,
                                    switch_on=lambda tid, home: True)
        assert "links" not in out and calls == [], status


def test_cli_install_routes_through_it():
    src = open(cli.__file__).read()
    assert "print(json.dumps(cli_install_links(c, install_with_retry(" in src
