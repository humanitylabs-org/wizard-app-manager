"""The "app changes ship in the app" rule must keep the publication approval gate and a full readback."""
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / 'system-skills/wizard-builder/SKILL.md'


def _rule() -> str:
    text = SKILL.read_text()
    start = text.index('## App changes ship in the app')
    return text[start:text.index('\n## ', start + 1)]


def test_rule_routes_release_through_the_owner_approval_gate():
    rule = _rule()
    assert "owner's approval" in rule and 'normal release path' in rule
    assert ', released, with installs updated' not in rule
    # the gate it points at still exists in the same skill
    assert "require the owner's explicit approval of that prepared artifact" in SKILL.read_text()


def test_readback_covers_every_changed_file_not_only_skill_md():
    rule = _rule()
    assert 'every file you changed' in rule and 'version' in rule
