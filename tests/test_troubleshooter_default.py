"""Defaults are installation intent, not proof that host recovery is configured."""
import pytest
from wam import slots, release_pins


@pytest.fixture(autouse=True)
def developer_defaults(monkeypatch):
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)


def test_new_box_gets_independent_recovery_component():
    assert 'wizard-troubleshooter' in slots.to_install({'apps': []})


def test_existing_chat_stack_still_needs_recovery():
    reg = {'apps': [{'id': 'wizard-agent-service', 'provides': ['agent-service']},
                    {'id': 'wizard-ux', 'provides': ['shell']},
                    {'id': 'hermes-agent', 'provides': ['agent']}]}
    assert 'wizard-troubleshooter' in slots.to_install(reg)


def test_existing_recovery_provider_is_not_replaced():
    assert 'wizard-troubleshooter' not in slots.to_install({'apps': [{'id': 'owner-recovery', 'provides': ['troubleshooter']}]})


def test_default_repo_alias_and_owner_removal():
    assert slots.repo_for('wizard-troubleshooter') == 'humanitylabs-org/wizard-troubleshooter'
    reg = {'apps': [{'id': 'wizard-troubleshooter', 'provides': ['troubleshooter']}]}
    assert not slots.blocks_uninstall(reg, 'wizard-troubleshooter')
    assert 'wizard-troubleshooter' not in slots.to_install(reg)
    assert slots.load_defaults()['troubleshooter']['required'] is False


def test_pinned_release_still_controls_its_default_set(monkeypatch):
    monkeypatch.setattr(release_pins, 'load', lambda: {'default_apps': {'shell': {'app': 'wizard-ux'}}})
    assert 'wizard-troubleshooter' not in slots.to_install({'apps': []})
