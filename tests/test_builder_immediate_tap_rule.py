"""Instruction/package regression, not an automatic cross-app UX checker."""
from pathlib import Path
import shutil

from wam import builder_bundle as bb


ROOT = Path(__file__).resolve().parents[1]
SKILL = Path('system-skills/wizard-builder/SKILL.md')
REFERENCE = Path('system-skills/wizard-builder/references/system-functions.md')


def test_builder_requires_feedback_before_io_and_no_false_completion():
    text = (ROOT / SKILL).read_text()
    assert '## Every tap registers before I/O' in text
    assert 'right after the write succeeds' not in text
    assert 'before awaiting the write' in text
    assert 'irreversible or external' in text
    assert 'not an automatic cross-app lint or release blocker' in text
    assert 'wam verify' in text and 'not a reason to write a per-app harness' in ' '.join(text.split())


def test_behavioral_acceptance_names_real_surfaces_and_adversarial_outcomes():
    text = (ROOT / REFERENCE).read_text()
    assert '## First-frame action acceptance' in text
    for requirement in (
        'widget/ready-card', 'standalone', 'in-shell', 'two requestAnimationFrame',
        '100 ms', 'unresolved', 'stale poll', 'double tap', 'readback',
        'late failure', 'retry', 'not an automatic cross-app lint or release blocker',
    ):
        assert requirement in text
    assert 'not a per-app test plan' in text and 'Never write new test harnesses' in text


def test_bundle_upgrade_delivers_both_instruction_files_and_rollback(tmp_path):
    """Real content-addressed install/update; mutate only disposable source/home."""
    source = tmp_path / 'source'
    for name in ('wam', 'templates', 'system-skills', 'wizard-views'):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy(ROOT / 'LICENSE', source / 'LICENSE')
    # Explicit synthetic versions keep this lifecycle fixture about delivery and
    # rollback, not the separate unknown-version preservation policy.
    (source / bb.STAMP).write_text(f'{"1" * 40} 1700000000\n')
    for relative in (SKILL, REFERENCE):
        (source / relative).write_text('previous instruction\n')
    home = tmp_path / 'os'
    first = bb.install(home, source)
    for relative in (SKILL, REFERENCE):
        shutil.copy(ROOT / relative, source / relative)
    (source / bb.STAMP).write_text(f'{"2" * 40} 1800000000\n')
    second = bb.install(home, source)
    assert second['state'] == 'installed'
    assert second['sha256'] != first['sha256']
    assert Path(second['skill']).is_file()
    for relative in (SKILL, REFERENCE):
        assert (home / relative).read_bytes() == (ROOT / relative).read_bytes()
    assert all(row[1] for row in bb.health(home))
    assert bb.install(home, source)['release'] == second['release']
    bb.rollback(home, first['release'])
    for relative in (SKILL, REFERENCE):
        assert (home / relative).read_text() == 'previous instruction\n'
    assert all(row[1] for row in bb.health(home))
