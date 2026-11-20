"""wizard-app-manager#858: an exec argument over the Linux 128 KiB limit never starts, so the checker fails it."""
import base64
import json
import re
import subprocess
import sys
import zlib

import pytest
import yaml

from wam import wizardmd
from wam.builder_assets import package_inline


def compose(command=None, entrypoint=None, environment=None):
    s = {"image": "python:3.12-alpine"}
    if command is not None:
        s["command"] = command
    if entrypoint is not None:
        s["entrypoint"] = entrypoint
    if environment is not None:
        s["environment"] = environment
    return yaml.safe_dump({"services": {"front": s}}, width=10**7)


def test_a_command_argument_over_120_kb_fails():
    risks = wizardmd.compose_risks(compose(command=["python", "-c", "x" * 154_132]))
    assert risks == ["front command argument is 154 KB; Linux refuses one argument over 128 KiB. "
                     "Pack it (zlib+base64 in mcp/inline.py) or ship a file."]


@pytest.mark.parametrize("kw", [
    {"command": "python -c " + "y" * 125_000},
    {"entrypoint": ["sh", "-c", "z" * 125_000]},
    {"entrypoint": "z" * 125_000},
    {"environment": {"BIG": "v" * 125_000}},
    {"environment": ["BIG=" + "v" * 125_000]},
])
def test_every_exec_string_is_measured(kw):
    risks = wizardmd.compose_risks(compose(**kw))
    assert len(risks) == 1 and "Linux refuses one argument over 128 KiB" in risks[0]


def test_between_100_and_120_kb_warns_but_passes():
    text = compose(command=["python", "-c", "x" * 110_000])
    assert wizardmd.compose_risks(text) == []
    assert wizardmd.compose_arg_warnings(text) == [
        "front command argument is 110 KB; Linux refuses one argument over 128 KiB. "
        "Pack it (zlib+base64 in mcp/inline.py) or ship a file."]


def test_small_and_odd_composes_stay_quiet():
    for text in (compose(command=["python", "-c", "print(1)"], environment={"A": None, "B": 1}),
                 compose(command="python -c 'unbalanced", environment=["A"]),
                 yaml.safe_dump({"services": {"a": "not a mapping"}})):
        assert wizardmd.compose_risks(text) == [] and wizardmd.compose_arg_warnings(text) == []


def test_the_limit_is_real_on_linux():
    """The kernel refuses one argument of 154,132 bytes (what Photos 0.4.0 shipped), and starts one of 120,000."""
    if not sys.platform.startswith("linux"):
        pytest.skip("MAX_ARG_STRLEN is Linux")
    with pytest.raises(OSError) as e:
        subprocess.run([sys.executable, "-c", "#" + "x" * 154_131], check=True)
    assert e.value.errno == 7   # E2BIG: Argument list too long
    subprocess.run([sys.executable, "-c", "#" + "x" * (wizardmd.ARG_ERROR_BYTES - 1)], check=True)


def _app(tmp_path, big=0):
    root = tmp_path / "big-app"
    (root / "app").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "runtipi").mkdir()
    (root / "wizard.md").write_text("---\nid: big-app\nname: Big\n---\n\n# Big\n")
    (root / "openapi.json").write_text("{}")
    (root / "app/server.py").write_text(
        "import pathlib\nr = pathlib.Path(__file__).resolve().parent.parent\n"
        "(r / 'ran').write_text(str(len((r / 'web/big.js').read_text())))\n")
    # Repetitive like real pages, so it packs well below the limit.
    (root / "web/big.js").write_text(("// a line of a real page, repeated\nconst a = 1;\n" * (big // 48 + 1))[:big])
    (root / "web/index.html").write_text("<!doctype html>")
    return root


def test_package_inline_packs_a_big_app_so_it_still_starts(tmp_path):
    root = _app(tmp_path, big=200_000)
    package_inline(root)
    text = (root / "runtipi/docker-compose.yml").read_text()
    code = yaml.safe_load(text)["services"]["big-app"]["command"][2]
    assert len(code.encode()) < wizardmd.ARG_WARN_BYTES
    assert "zlib.decompress" in code
    assert wizardmd.compose_risks(text) == [] and wizardmd.compose_arg_warnings(text) == []
    blob = re.search(r"b64decode\('([^']+)'\)", code).group(1)
    files = json.loads(zlib.decompress(base64.b64decode(blob)))
    assert len(files["web/big.js"]) == 200_000
    # And the packed program really runs as one argument and unpacks every file.
    work = tmp_path / "run"
    subprocess.run([sys.executable, "-c", code.replace("/tmp/wizard-app", str(work))], check=True, timeout=30)
    assert (work / "ran").read_text() == "200000"


def test_package_inline_keeps_small_apps_plain(tmp_path):
    root = _app(tmp_path, big=1_000)
    package_inline(root)
    code = yaml.safe_load((root / "runtipi/docker-compose.yml").read_text())["services"]["big-app"]["command"][2]
    assert "zlib" not in code and "files=json.loads(base64.b64decode('" in code


def test_package_inline_refuses_a_payload_too_big_to_unpack(tmp_path):
    """A huge repetitive asset packs into a small argument but would not fit the 128 MB container (Codex P2)."""
    root = _app(tmp_path, big=5_000_000)
    with pytest.raises(ValueError, match=r"unpacks to 5\.\d MB; inline apps carry at most 4 MB"):
        package_inline(root)
    assert not (root / "runtipi/docker-compose.yml").exists()
