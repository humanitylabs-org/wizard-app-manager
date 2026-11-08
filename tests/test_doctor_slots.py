"""An owner-installed alternative provider of a single-app slot is a supported state (install the alternative,
then remove the default; the saved slots.<s>.default is the active one). `wam doctor` must not FAIL on it, or a
strict WizardOS install/rollback aborts on a box where the owner merely added Wizard Files next to Files
(wizard-os#389 rehearsal: "slot 'files' has 2 apps (files, wizard-files)")."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_doctor_updates import box  # noqa: F401,E402  (shared doctor fixture)
from wam import cli, registry  # noqa: E402


def two_files(box, active='files'):
    reg = {'apps': [{'id': 'files', 'provides': ['files']}, {'id': 'wizard-files', 'provides': ['files']}],
           'slots': {'files': {'default': active, 'candidates': ['files', 'wizard-files']}}}
    (box.home / 'apps.json').write_text(json.dumps(reg))
    return reg


def test_doctor_passes_with_an_alternative_files_provider(box, monkeypatch, capsys):
    two_files(box)
    checks = cli.doctor(box)
    assert all(ok for _, ok, _ in checks), [c for c in checks if not c[1]]
    row = next(c for c in checks if c[0] == "slot 'files': active app is chosen")
    assert row[1] and row[2].startswith('files (also installed: wizard-files)')
    monkeypatch.setattr(sys, 'argv', ['wam', 'doctor'])
    assert cli.main() == 0
    assert 'FAIL' not in capsys.readouterr().out


def test_doctor_follows_the_owner_choice(box):
    two_files(box, active='wizard-files')
    row = next(c for c in cli.doctor(box) if c[0].startswith("slot 'files'"))
    assert row[1] and row[2].startswith('wizard-files (also installed: files)')


def test_a_choice_naming_no_installed_provider_still_fails(box):
    two_files(box, active='gone-app')
    row = next(c for c in cli.doctor(box) if c[0].startswith("slot 'files'"))
    assert not row[1] and 'gone-app' in row[2]


def test_multi_slots_and_single_providers_add_no_row():
    reg = {'slots': {'ai-connection': {'default': 'wizard-claude', 'candidates': ['wizard-chatgpt', 'wizard-claude']},
                     'files': {'default': 'files', 'candidates': ['files']}}}
    assert registry.slot_choices(reg) == []
