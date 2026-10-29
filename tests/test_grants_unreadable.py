import json
from types import SimpleNamespace
from wam import cli


class RT:
    def __init__(self, grants=None, boom=False):
        self.grants, self.boom = grants, boom

    def req(self, method, path, body=None):
        assert (method, path) == ("GET", "/apps/wizard/grants")
        if self.boom:
            raise RuntimeError("down")
        return self.grants


def _cfg(tmp_path, monkeypatch, readable):
    state = tmp_path / "runtipi" / "state"; state.mkdir(parents=True)
    f = state / "wizard-grants.json"
    f.write_text(json.dumps({"schema": 1, "grants": [{"kind": "app", "from": "file", "to": "x"}]}))
    home = tmp_path / "home"; home.mkdir()
    if not readable:
        real = type(f).read_text
        def read_text(self, *a, **k):
            if self.name == "wizard-grants.json":
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *a, **k)
        monkeypatch.setattr(type(f), "read_text", read_text)
    return SimpleNamespace(runtipi_root=tmp_path / "runtipi", home=home)


def test_readable_file_wins(tmp_path, monkeypatch):
    c = _cfg(tmp_path, monkeypatch, True)
    assert cli.read_grants(c, RT(boom=True))[0]["from"] == "file"


def test_root_only_file_reads_grants_from_the_app_manager_api(tmp_path, monkeypatch):
    c = _cfg(tmp_path, monkeypatch, False)
    assert cli.read_grants(c, RT([{"kind": "app", "from": "api", "to": "y"}])) == [{"kind": "app", "from": "api", "to": "y"}]


def test_root_only_file_and_api_down_keeps_mirrored_grants(tmp_path, monkeypatch):
    c = _cfg(tmp_path, monkeypatch, False)
    (c.home / "apps.json").write_text(json.dumps({"apps": [], "grants": [{"from": "mirror"}]}))
    assert cli.read_grants(c, RT(boom=True)) == [{"from": "mirror"}]
    (c.home / "apps.json").unlink()
    assert cli.read_grants(c, RT(boom=True)) == []
