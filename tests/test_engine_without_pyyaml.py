"""#971: on a Hermes v0.23+ engine (ruamel.yaml only, no PyYAML) the gateway link failed with `No module named
'yaml'` and the builder bundle's `wam` (run by the engine's Python) failed at `import yaml` in wam/cli.py."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from wam.agent_link import EDIT

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "wam" / "_vendor"

# A Python that has no PyYAML: `-S` leaves out site-packages, where the test environment's PyYAML lives.
NO_SITE = [sys.executable, "-S"]


def test_wam_runs_on_a_python_without_pyyaml():
    p = subprocess.run(NO_SITE + ["-c", textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        import wam.cli, wam.builder_bundle, wam.agent_link, wam.network
        import yaml
        print(yaml.__file__)
        doc = wam.network.load_yaml("services:\\n  a:\\n    networks: !override\\n      x: {{}}\\n")
        print(type(doc["services"]["a"]["networks"]).__name__)
    """)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    path, tagged = p.stdout.split()
    assert Path(path).resolve() == (VENDOR / "yaml" / "__init__.py").resolve()
    assert tagged == "_Tagged"


def test_the_box_keeps_its_own_pyyaml():
    import yaml
    assert VENDOR not in [Path(p) for p in sys.path]
    assert not Path(yaml.__file__).resolve().is_relative_to(VENDOR.resolve())


def test_vendored_pyyaml_is_the_unmodified_release():
    assert "__version__ = '6.0.3'" in (VENDOR / "yaml" / "__init__.py").read_text()
    assert "PyYAML" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    assert (VENDOR / "yaml" / "LICENSE").is_file()


def _run_edit(tmp_path, extra_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("# owner comment\nmodel:\n  default: x\n")
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(extra_path)}
    p = subprocess.run(NO_SITE + ["-c", "import sys; sys.path[:0] = __import__('os').environ['PYTHONPATH'].split(':');"
                                  " sys.argv = sys.argv[1:]; exec(compile(open(sys.argv[0]).read(), 'edit', 'exec'))",
                                  str(tmp_path / "edit.py"), "http://svc:8787/wizard/mcp", "wizard", "/opt/wizard-os/skills"],
                       input="tok\n", capture_output=True, text=True, timeout=60, env=env)
    return p, home


def test_gateway_link_edit_uses_ruamel_when_the_engine_has_no_pyyaml(tmp_path):
    """Stand-in for the engine's ruamel.yaml (round-trip YAML().load/dump), built on the vendored PyYAML so the test
    needs no extra package. The point: EDIT must not import PyYAML there."""
    (tmp_path / "edit.py").write_text(EDIT)
    fake = tmp_path / "fake"
    (fake / "ruamel").mkdir(parents=True)
    (fake / "ruamel" / "__init__.py").write_text("")
    (fake / "ruamel" / "yaml.py").write_text(textwrap.dedent(f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "_pyyaml", {str(VENDOR / "yaml" / "__init__.py")!r},
            submodule_search_locations=[{str(VENDOR / "yaml")!r}])
        _y = importlib.util.module_from_spec(spec); sys.modules["_pyyaml"] = _y; spec.loader.exec_module(_y)
        USED = []
        class YAML:
            def load(self, raw):
                USED.append("load"); return _y.safe_load(raw)
            def dump(self, doc, out):
                USED.append("dump"); out.write(_y.safe_dump(doc, sort_keys=False))
    """))
    p, home = _run_edit(tmp_path, fake)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["config_changed"] and out["env_changed"]
    text = (home / "config.yaml").read_text()
    assert "http://svc:8787/wizard/mcp" in text and "/opt/wizard-os/skills" in text and "default: x" in text
    assert "WIZARD_MCP_TOKEN=tok" in (home / ".env").read_text()


def test_gateway_link_edit_without_any_yaml_says_so(tmp_path):
    (tmp_path / "edit.py").write_text(EDIT)
    p, _ = _run_edit(tmp_path, tmp_path / "nothing")
    assert p.returncode != 0 and "ruamel" in p.stderr
