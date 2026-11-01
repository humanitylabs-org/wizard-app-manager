"""Alongside installs refresh the wizard-os plugin to the App Manager's own pin, not the box's first link."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_install_defaults_plugin_ref_to_the_shipped_pin():
    text = (ROOT / 'install.sh').read_text()
    link = text.index('"$HOME/.local/bin/wizard" link')
    default = text.index('WIZARD_PLUGIN_REF="$(cd "$HERE" && "$PY" -c \'from wam.agent_link import plugin_ref')
    assert default < link, 'the default pin must be set before `wizard link` runs'


def test_pin_expression_prints_the_agent_link_pin():
    out = subprocess.run(['python3', '-c', 'from wam.agent_link import plugin_ref; print(plugin_ref())'],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    assert re.fullmatch(r'[0-9a-f]{40}', out)


def test_plugin_follows_the_release_agent_service_commit(tmp_path, monkeypatch):
    from wam import agent_link
    pins = tmp_path / 'release-pins.json'
    commit = 'a' * 40
    pins.write_text(json.dumps({'components': {'wizard-agent-service': {
        'repo': 'humanitylabs-org/wizard-agent-service', 'commit': commit}}}))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(pins))
    assert agent_link.plugin_ref() == commit


def test_plugin_pin_falls_back_without_a_valid_release(tmp_path, monkeypatch):
    from wam import agent_link
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    assert agent_link.plugin_ref() == agent_link.PLUGIN_REF
    pins = tmp_path / 'release-pins.json'
    pins.write_text(json.dumps({'components': {'wizard-agent-service': {'repo': 'someone/else', 'commit': 'b' * 40}}}))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(pins))
    assert agent_link.plugin_ref() == agent_link.PLUGIN_REF
    pins.write_text('not json')
    assert agent_link.plugin_ref() == agent_link.PLUGIN_REF
