"""Factory bootstrap must keep WAM ownership and accept the real pinned UX recipe."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from wam import agent_link, builder_bundle, fresh_agent

FIXTURE = Path(__file__).parent / "fixtures/shell-recipe"


def test_shell_recipe_fixture_matches_pinned_blobs():
    source = json.loads((FIXTURE / "SOURCE.json").read_text())
    for name, digest in source["sha256"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest
    # Admission can point this at the release's selected UX source, not a shaped fake.
    recipe = Path(os.environ["WAM_SHELL_RECIPE"]) if os.environ.get("WAM_SHELL_RECIPE") else \
        Path(__file__).resolve().parents[2] / "ux/packaging"
    if recipe.is_dir():
        for name in source["sha256"]:
            assert (recipe / name).read_bytes() == (FIXTURE / name).read_bytes()


@pytest.mark.parametrize("drift", [None, "template", "framing", "mismatched-framing"])
def test_factory_shell_adapter_runs_against_pinned_recipe_and_refuses_drift(tmp_path, drift):
    etc, dst, out = tmp_path / "recipe", tmp_path / "adapted", tmp_path / "out"
    shutil.copytree(FIXTURE, etc)
    out.mkdir()
    template = (etc / "nginx.conf.template").read_text()
    static = (etc / "static.conf").read_text()
    if drift == "template":
        (etc / "nginx.conf.template").write_text(template.replace("    server_name _;", "    server_name changed;"))
    elif drift == "framing":
        (etc / "static.conf").write_text(static.replace("frame-ancestors 'self'", "frame-ancestors *"))
    elif drift == "mismatched-framing":
        (etc / "static.conf").write_text(static.replace('X-Frame-Options "SAMEORIGIN"', 'X-Frame-Options "DENY"'))
    # Stop at the real entrypoint boundary and inspect the files it will consume.
    # Existing fresh-agent tests cover the old DENY/'none' recipe too.
    entry = tmp_path / "entry"
    entry.write_text(f'cp {etc}/static.conf {out}/static\ncp {etc}/nginx.conf.template {out}/template\n')
    script = (fresh_agent.SHELL_ADAPTER_SH.replace("/etc/wizard-shell", str(etc))
              .replace("/tmp/wizard-shell-adapted", str(dst))
              .replace("/usr/local/bin/wizard-shell-entrypoint", str(entry)))
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            env={**os.environ, "WIZARD_SHELL_LISTEN": "127.0.0.1:8796"})
    if drift:
        assert result.returncode == 3 and "regenerate the adapter" in result.stderr
        assert not (out / "template").exists()
        return
    assert result.returncode == 0, result.stderr
    adapted = (out / "template").read_text()
    assert "    allow 127.0.0.1;\n    deny all;" in adapted
    assert adapted.count("location ^~ /wizard-os-home/store-data/") == 1
    assert "limit_except GET { deny all; }" in adapted
    assert template.replace("    server_name _;", "    server_name _;\n    allow 127.0.0.1;\n    deny all;").replace(
        "    location /wizard-os-home/ {",
        "    location ^~ /wizard-os-home/store-data/ {\n      limit_except GET { deny all; }\n"
        "      proxy_pass $wizard_api$request_uri; include /tmp/wizard-proxy.conf;\n    }\n    location /wizard-os-home/ {") == adapted
    assert (out / "static").read_text() == static
    assert (etc / "nginx.conf.template").read_text() == template
