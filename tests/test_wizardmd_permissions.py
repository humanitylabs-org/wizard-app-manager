"""wizard.md `permissions` (same rule as wizard-os tools/check_wizard_md.py, wizard-os#731)."""
import pytest
from wam import wizardmd

RULE = 'permissions are known (geolocation)'


def _rules(tmp_path, line):
    (tmp_path / 'wizard.md').write_text('---\nwizard: 0.2\nid: maps-test\ntype: view\n' + line + '---\n')
    return {n: ok for n, ok, _ in wizardmd.check(tmp_path) if 'permission' in n}


def test_absent_adds_no_rule(tmp_path):
    assert _rules(tmp_path, '') == {}


@pytest.mark.parametrize('line', ['permissions: [geolocation]\n', 'permissions: []\n'])
def test_geolocation_and_empty_pass(tmp_path, line):
    assert _rules(tmp_path, line) == {RULE: True}


@pytest.mark.parametrize('line', ['permissions: [camera]\n', 'permissions: [geolocation, microphone]\n',
                                  'permissions: [Geolocation]\n', 'permissions: geolocation\n',
                                  'permissions: [geolocation, geolocation]\n', 'permissions:\n', 'permissions: {}\n',
                                  'permissions: [1]\n'])
def test_other_values_fail(tmp_path, line):
    assert _rules(tmp_path, line) == {RULE: False}
