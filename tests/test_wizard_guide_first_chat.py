"""The five-choice first chat (humanitylabs-org/wizard-agent-service#650, switch wizard/first-chat-goals): the guide
holds the section the Agent Service's run note points at, and the old first chat stays as it was for boxes without it."""
import json
import re
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / 'system-skills/wizard-guide/SKILL.md'
NOTE = 'Five-choice first chat is on'
CHOICES = ['Get on top of my inbox', 'Organize my digital life', 'Grow my business', 'Find a job', 'Something else']
CLOSING = ("I can also connect your email, messages, money and calendar, or bring over what ChatGPT knows about you. "
           "It's all in Setup whenever you want, or just ask.")


def section(text, title):
    return text.split(f'## {title}\n', 1)[1].split('\n## ', 1)[0].strip()


def test_old_first_chat_is_kept_and_hands_over_only_on_the_note():
    old = section(GUIDE.read_text(), 'First chat')
    for words in ('Build me an app that tracks my runs.', 'Change my skin so the background is dark green.',
                  'Remind me tomorrow morning to call the bank.', 'wizard_tips'):
        assert words in old
    assert f'If your run instructions say "{NOTE}"' in old


def test_five_choices_are_one_reply_buttons_marker_in_the_issue_order():
    five = section(GUIDE.read_text(), 'First chat: five choices')
    assert five.startswith(f'Only when your run instructions say "{NOTE}"')
    marks = re.findall(r'\[WIZARD_CHOICES_V1\](.*?)\[/WIZARD_CHOICES_V1\]', five)
    assert len(marks) == 1
    assert json.loads(marks[0]) == {'choices': CHOICES}


def test_memory_first_win_closing_line_tips_and_goals_rules():
    five = section(GUIDE.read_text(), 'First chat: five choices')
    assert 'About me (<YYYY-MM-DD>)' in five and 'memory tool' in five
    assert 'no goal method, predictions, check-ins or tracking' in five
    inbox = next(line for line in five.splitlines() if line.strip().startswith('- Get on top of my inbox'))
    assert 'Mail' in inbox and 'last two days' in inbox and 'draft replies' in inbox and 'send nothing without their yes' in inbox
    assert 'only at the step that needs it' in five and 'never a list up front' in five
    assert five.count(CLOSING) == 1 and 'once' in five
    assert '`wizard_tips`' in five and '`pick`' in five and '`about`' in five
    assert 'Never mention a Goals app in the first chat' in five
