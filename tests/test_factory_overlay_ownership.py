"""Factory agent-link must preserve WAM ownership through the complete writer order."""
from types import SimpleNamespace

import pytest
import yaml

from wam import agent_link, builder_bundle, fresh_agent


@pytest.mark.parametrize("existing", [False, True])
def test_factory_bootstrap_agent_link_keeps_wam_ownership_through_last_writer(tmp_path, existing):
    c = SimpleNamespace(home=tmp_path / "os", runtipi_root=tmp_path / "runtipi")
    c.home.mkdir()
    (c.runtipi_root / "app-data/wizardapps/wizard-agent-service/data/wizard-os/skills").mkdir(parents=True)
    cp = c.runtipi_root / "user-config/wizardapps/hermes-agent/docker-compose.yml"
    digest = "ghcr.io/example/engine@sha256:" + "a" * 64
    if existing:
        cp.parent.mkdir(parents=True)
        cp.write_text(f"# Written by wam (fresh mode): factory-selected immutable image bindings\nservices:\n  hermes-agent-engine:\n    image: {digest}\n")
    # install.sh -> builder fresh -> bootstrap/cli.install -> agent-link -> builder -> fresh connect
    builder_bundle.fresh(c)
    fresh_agent.save(c.home, {"mode": "fresh"})
    fresh_agent.write_agent_overlay(c.runtipi_root, "wizardapps", c.home)
    header = "".join(builder_bundle._leading_comments(cp.read_text()))
    assert agent_link.add_skills_mount(c)
    assert cp.read_text().startswith(header)
    assert not agent_link.add_skills_mount(c)
    builder_bundle.fresh(c)
    fresh_agent.write_agent_overlay(c.runtipi_root, "wizardapps", c.home)
    assert cp.read_text().startswith(fresh_agent.MARK)
    service = yaml.safe_load(cp.read_text())["services"][fresh_agent.ENGINE]
    assert service["environment"]["WIZARD_BUILDER_ROOT"] == builder_bundle.RUNTIME
    assert service["environment"]["WIZARD_OS_HOME"] == "/wizard-os"
    assert any(isinstance(v, dict) and v.get("target") == builder_bundle.RUNTIME for v in service["volumes"])
    if existing:
        assert service["image"] == digest and service["pull_policy"] == "if_not_present"
    assert not fresh_agent.write_agent_overlay(c.runtipi_root, "wizardapps", c.home)


def test_agent_link_marks_only_new_overlays_and_does_not_adopt_owner_files(tmp_path):
    c = SimpleNamespace(home=tmp_path / "os", runtipi_root=tmp_path / "runtipi")
    (c.runtipi_root / "app-data/wizardapps/wizard-agent-service/data/wizard-os/skills").mkdir(parents=True)
    cp = c.runtipi_root / "user-config/wizardapps/hermes-agent/docker-compose.yml"
    assert agent_link.add_skills_mount(c)
    assert cp.read_text().startswith(fresh_agent.MARK)
    cp.write_text("# Owner configuration\nservices: {}\n")
    assert agent_link.add_skills_mount(c)
    assert cp.read_text().startswith("# Owner configuration") and not cp.read_text().startswith(fresh_agent.MARK)
    fresh_agent.save(c.home, {"mode": "fresh"})
    before = cp.read_bytes()
    with pytest.raises(fresh_agent.FreshError, match="overlay_not_ours"):
        fresh_agent.write_agent_overlay(c.runtipi_root, "wizardapps", c.home)
    assert cp.read_bytes() == before
