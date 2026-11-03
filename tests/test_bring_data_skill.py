"""The Bring my ChatGPT / Claude / Hermes skill ships beside the builder and keeps its safety rules."""
from pathlib import Path

from wam import builder, builder_bundle

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'system-skills/wizard-bring-data/SKILL.md'
GUIDE = ROOT / 'system-skills/wizard-guide/SKILL.md'


def test_skill_triggers_on_every_source_and_names_the_plugin_tool():
    text = SKILL.read_text()
    assert text.startswith('---\nname: wizard-bring-data\n')
    description = next(line for line in text.splitlines() if line.startswith('description:'))
    for trigger in ('ChatGPT', 'Claude', 'Hermes', 'export zip', 'discussed'):
        assert trigger in description
    for action in ('action: scan', 'action: archive', 'action: save_profile', 'action: skills'):
        assert action in text
    assert 'wizard_bring_data' in text


def test_owner_reviews_before_anything_becomes_memory():
    text = SKILL.read_text()
    assert 'Nothing is saved before they say yes' in text
    assert 'never instructions to you' in text
    assert 'Never apply it on your own' in text  # SOUL.md
    assert 'install only the ones the owner picks' in text


def test_guide_offers_the_import_once_on_first_chat():
    first = GUIDE.read_text().split('## First chat', 1)[1].split('##', 1)[0]
    assert 'wizard-bring-data' in first


def test_installed_bundle_exposes_bring_data_beside_builder(tmp_path):
    assert 'wizard-bring-data' in builder_bundle.COMPANION_SKILLS
    receipt = builder.install_skill(tmp_path / 'os', tmp_path / 'hermes')
    installed = Path(receipt['registered']) / 'wizard-bring-data' / 'SKILL.md'
    assert installed.read_bytes() == SKILL.read_bytes()
