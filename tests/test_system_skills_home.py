"""wizard-os#760: this repo's system-skills/ is the one home for WizardOS system skills."""
from pathlib import Path

HOME = Path(__file__).resolve().parents[1] / 'system-skills'


def test_one_home_lists_the_shipped_skills():
    skills = sorted(p.name for p in HOME.iterdir() if p.is_dir())
    assert skills == ['wizard-builder', 'wizard-guide']
    for name in skills:
        assert (HOME / name / 'SKILL.md').is_file()
    readme = (HOME / 'README.md').read_text()
    assert 'only place' in readme and 'wizard-os#760' in readme


def test_builder_carries_the_retired_app_builder_pointer():
    text = (HOME / 'wizard-builder/SKILL.md').read_text()
    assert 'is there a component here other apps could share?' in text
    assert 'APP-DESIGN-RULES.md' in text
