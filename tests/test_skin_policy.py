from pathlib import Path
from wam import builder


def test_shipped_builder_instructions_keep_the_accepted_skin_set():
    root = Path(builder.REPO)
    skill = (root / 'system-skills/wizard-builder/SKILL.md').read_text()
    appearance = (root / 'system-skills/wizard-builder/references/appearance.md').read_text()
    for name in ('Nebula', 'Grove', 'Tide', 'Ember', 'Signal'):
        assert name in skill and name in appearance
    assert 'Paper is retired' in skill and 'Paper is retired' in appearance
    assert 'shell registry' in skill and 'shell registry' in appearance


def test_builder_skin_qa_uses_the_current_registry_policy():
    source = (Path(builder.REPO) / 'qa/builder/skin-browser.cjs').read_text()
    assert "const SKINS=['nebula','grove','tide','ember','signal'];" in source
    assert 'Paper is retired from the shell registry' in source
    assert 'Paper is absent from the skin controls' in source
    assert '.pv-skin[data-skin="paper"]' in source
