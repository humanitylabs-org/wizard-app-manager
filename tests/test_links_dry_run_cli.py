"""#1106: `wam links --dry-run` crashed with UnboundLocalError 'registry' in main(). A bug fix, always on."""
import ast
import json
from pathlib import Path

from wam import cli


class _FakeRuntipi:
    @classmethod
    def from_install(cls, root):
        return cls()

    def installed(self):
        return []


def test_links_dry_run_json_prints_the_plan_and_exits_0(tmp_path, monkeypatch, capsys):
    rt = tmp_path / "runtipi"
    rt.mkdir()
    (rt / ".env").write_text("INTERNAL_IP=127.0.0.1\nNGINX_PORT=1\nJWT_SECRET=x\n")
    home = tmp_path / "home"
    monkeypatch.setenv("RUNTIPI_ROOT", str(rt))
    monkeypatch.setenv("WIZARD_OS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "Runtipi", _FakeRuntipi)
    rc = cli.main(["links", "--dry-run", "--json"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0, res
    assert res["errors"] == {} or not res["errors"]
    assert not (home / "links.json").exists()      # a dry run writes nothing


def test_main_never_reimports_a_module_level_name():
    """An inner `from . import x` of a name cli.py imports at the top makes `x` local to the whole function,
    so every branch that uses it before that line crashes (the #1106 trap)."""
    tree = ast.parse(Path(cli.__file__).read_text())
    top = {a.asname or a.name.split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
           for a in n.names}
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    inner = {a.asname or a.name.split(".")[0] for n in ast.walk(main) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names}
    assert not (top & inner), f"main() re-imports module-level names: {sorted(top & inner)}"
