"""The guide names the update choices that are on screen (wizard-app-manager#1017): Updates Automatic / Ask me,
Channel For everyone / For testers; the retired Standard / Early access names are gone."""
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / 'system-skills/wizard-guide/SKILL.md'


def test_updates_section_uses_the_settings_words():
    updates = GUIDE.read_text().split('### Updates (Settings › About & updates)', 1)[1].split('\n### ', 1)[0]
    for words in ('Automatic', 'Ask me', 'For everyone', 'For testers', 'Try new features'):
        assert words in updates
    for retired in ('**Version**', 'Early access', 'Standard only'):
        assert retired not in updates
