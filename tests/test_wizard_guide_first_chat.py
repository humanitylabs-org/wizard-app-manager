"""The five-choice first chat (humanitylabs-org/wizard-agent-service#650, on for everyone; the wizard/first-chat-goals
switch was removed): it is the guide's only first chat
(still titled "First chat: five choices", which the Service's run note names), with no run-note condition."""
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


def test_five_choices_are_the_only_first_chat():
    text = GUIDE.read_text()
    assert '## First chat\n' not in text, 'one first chat section: the five choices'
    first = section(text, 'First chat: five choices')
    for words in ('Build me an app that tracks my runs.', 'Change my skin so the background is dark green.',
                  'Remind me tomorrow morning to call the bank.', NOTE, 'Only when your run instructions'):
        assert words not in text
    assert first.startswith('When this is the owner\'s first conversation')


def test_five_choices_are_one_reply_buttons_marker_in_the_issue_order():
    five = section(GUIDE.read_text(), 'First chat: five choices')
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
