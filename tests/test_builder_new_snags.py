"""Snags a timed builder run hit on 7 Nov 2026 (a To-dos app): the id rule, a colon in the summary, the inline command."""
from pathlib import Path

import pytest
import yaml

from wam import builder, wizardmd
from wam.builder_assets import package_inline

REPO = Path(__file__).resolve().parents[1]


def test_new_names_the_kebab_case_id_when_id_and_name_disagree(tmp_path):
    with pytest.raises(ValueError) as e:
        builder.new_app('todos', tmp_path, 'a to-do list', name='To-dos', kind='list')
    assert "`wam new to-dos --name To-dos`" in str(e.value) and 'kebab-case' in str(e.value)
    assert not (tmp_path / 'todos').exists()
    assert (builder.new_app('to-dos', tmp_path, 'a to-do list', name='To-dos', kind='list') / 'wizard.md').is_file()


def _summary(root, line):
    wm = root / 'wizard.md'
    text = wm.read_text()
    start = text.index('summary:')
    end = text.index('\nlicense:', start)
    wm.write_text(text[:start] + line + text[end:])
    return wm


def test_a_colon_is_rejected_until_package_inline_repairs_the_source(tmp_path):
    root = builder.new_app('to-dos', tmp_path, 'a to-do list', name='To-dos', kind='list')
    wm = _summary(root, 'summary: To-dos: one list for the house, with groups')
    original = wm.read_bytes()
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(wm.read_text().split('---', 2)[1])
    with pytest.raises(yaml.YAMLError):
        wizardmd.front_matter(wm.read_text())
    assert any(name == 'front matter parses' and not ok for name, ok, _ in wizardmd.check(root))
    assert not builder.check_app(root, proposal=True)['ok']
    with pytest.raises(ValueError, match='Source failed checker'):
        builder.prepare_publish(root, 'example/to-dos')
    assert wm.read_bytes() == original, 'checking and publication preparation must not repair source'
    package_inline(root)
    doc = yaml.safe_load(wm.read_text().split('---', 2)[1])
    assert doc['summary'] == 'To-dos: one list for the house, with groups'
    assert wizardmd.front_matter(wm.read_text()) == doc
    assert builder.check_app(root, proposal=True)['ok']
    assert builder.prepare_publish(root, 'example/to-dos')['state'] == 'prepared_requires_human_approval'


def test_quote_colons_folds_continuations_and_leaves_real_errors_alone():
    text = '---\nid: a-b\nsummary: Lists: groups and items\n  across lines\nprovides: [lists]\n---\n\nBody: here\n'
    fixed = wizardmd.quote_colons(text)
    assert yaml.safe_load(fixed.split('---', 2)[1]) == {
        'id': 'a-b', 'summary': 'Lists: groups and items across lines', 'provides': ['lists']}
    assert fixed.endswith('---\n\nBody: here\n')
    good = '---\nid: a-b\nsummary: "Lists: quoted"\n---\n'
    assert wizardmd.quote_colons(good) == good
    broken = '---\nid: [a-b\n---\n'
    assert wizardmd.quote_colons(broken) == broken
    with pytest.raises(yaml.YAMLError):
        wizardmd.front_matter(broken)


def test_skill_and_generated_wizard_md_name_the_same_inline_command(tmp_path):
    skill = (REPO / 'system-skills/wizard-builder/SKILL.md').read_text()
    assert 'wam package-inline <source>' in skill
    root = builder.new_app('to-dos', tmp_path, 'a to-do list', name='To-dos', kind='list')
    body = (root / 'wizard.md').read_text()
    assert 'wam package-inline .' in body and 'wam builder package-inline' not in body
    assert "`wam new to-dos --name 'To-dos'`" in skill                # the id rule, with its example
    assert 'bin/wam verify <source>' in skill                       # verify on another worker: the bundle by path
