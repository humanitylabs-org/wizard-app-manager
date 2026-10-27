"""Fresh configuration must not replace immutable releases with old Mac tags."""
from wam import fresh_agent as fresh

PIN = 'ghcr.io/example/engine@sha256:' + 'a' * 64


def test_generated_stock_overlays_defer_to_recipe_images():
    assert 'image:' not in fresh.AGENT_OVERLAY
    assert 'image:' not in fresh.SERVICE_OVERLAY
    assert 'wizard-shell:mac-mvp' not in fresh.SERVICE_OVERLAY


def test_immutable_security_pin_survives_fresh_regeneration(tmp_path):
    cp = tmp_path / 'user-config/wizardapps/hermes-agent/docker-compose.yml'
    cp.parent.mkdir(parents=True)
    cp.write_text(f'{fresh.MARK} (fresh mode)\nservices:\n  engine:\n    image: {PIN}\n    environment:\n      STALE: old\n  front:\n    image: {PIN}\n')
    text = f'{fresh.MARK} (fresh mode)\nservices:\n  engine:\n    environment:\n      LIVE: new\n'
    fresh._write_overlay(tmp_path, 'wizardapps', 'hermes-agent', text, {})
    out = cp.read_text()
    assert out.count('image: ' + PIN) == 2
    assert 'STALE' not in out and 'LIVE: new' in out
    assert not fresh._write_overlay(tmp_path, 'wizardapps', 'hermes-agent', text, {})


def test_legacy_mutable_tag_is_not_reintroduced():
    previous = 'services:\n  engine:\n    image: wizard-shell:mac-mvp-20261013\n'
    new = 'services:\n  engine:\n    environment: {}\n'
    assert fresh.preserve_digest_images(previous, new) == new


def test_reset_tags_survive_without_loss_of_routing():
    new = 'services:\n  shell:\n    ports: !reset []\n    networks: !reset []\n'
    old = f'services:\n  shell:\n    image: {PIN}\n    ports: !reset []\n'
    result = fresh.preserve_digest_images(old, new)
    assert 'ports: !reset []' in result and 'networks: !reset []' in result
    assert 'image: ' + PIN in result


def test_explicit_generated_pin_wins_over_previous():
    old = f'services:\n  engine:\n    image: {PIN}\n'
    new = old.replace('a' * 64, 'b' * 64)
    assert fresh.preserve_digest_images(old, new) == new
