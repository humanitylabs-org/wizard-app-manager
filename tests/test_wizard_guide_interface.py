"""The guide sends interface questions to the generated manual and names this release's update choices.

wizard-ux ships `wizard-interface` (skill/SKILL.md, generated from the interface at each release); the
hand-written guide must not compete with it, and its Updates words must be the ones on screen."""
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / 'system-skills/wizard-guide/SKILL.md'


def test_guide_points_interface_questions_at_the_generated_manual():
    text = GUIDE.read_text()
    section = text.split('## Where things are in Wizard', 1)[1].split('\n## ', 1)[0]
    assert '`wizard-interface` is in your skills, load it with skill_view' in section
    # Without the manual (switch wizard/interface-manual off, or an older shell) the guide answers as before.
    assert "If `wizard-interface` isn't in your skills" in section
    assert 'wins over the names in this guide' in section
    assert text.index('## Where things are in Wizard') < text.index('## First chat')


def test_updates_section_uses_the_settings_words():
    updates = GUIDE.read_text().split('### Updates (Settings › About & updates)', 1)[1].split('\n### ', 1)[0]
    for words in ('Automatic', 'Ask me', 'For everyone', 'For testers', 'Try new features'):
        assert words in updates
    for retired in ('**Version**', 'Early access', 'Standard only'):
        assert retired not in updates
