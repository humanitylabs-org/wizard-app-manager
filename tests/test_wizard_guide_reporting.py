"""Keep guide discovery, developer-mode truth and consent in the shipped bundle."""
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / 'system-skills/wizard-guide/SKILL.md'


def test_guide_keeps_onboarding_and_problem_activation():
    description = next(line for line in GUIDE.read_text().splitlines() if line.startswith('description:'))
    for trigger in ('what you can do', 'first chat', 'Wizard problem', 'change or fix'):
        assert trigger in description


def test_developer_mode_follows_branch_without_promising_automatic_rebases():
    text = GUIDE.read_text()
    developer = next(line for line in text.splitlines() if line.startswith('3. **Developer mode**'))
    for promise in ('named branch or fork', 'manually updated', "owner's explicit yes"):
        assert promise in developer
    assert 'merged onto each update' not in text


def test_guide_checks_installed_tweak_capabilities_before_using_them():
    text = GUIDE.read_text()
    assert 'Check the installed version' in text
    assert 'do not invent a missing API or setting' in text


def test_problem_report_keeps_two_try_stop_consent_and_public_fallback():
    text = GUIDE.read_text().split("### When you can't fix a Wizard problem", 1)[1]
    for required in ('two fix attempts', 'stop trying', 'Send nothing without their OK',
                     'no messages, chat text', 'passwords, keys or tokens',
                     'https://github.com/humanitylabs-org/wizard-ux/issues/new?',
                     'both URL-encoded', '1,500 characters',
                     'whoever set up their Wizard', 'still only after their OK'):
        assert required in text
    assert 'wizard-os/issues/new' not in text
    assert 'not confirmation that an issue was filed' in text
