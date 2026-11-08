"""A builder bundle newer than the box's App Manager is kept, never called outdated or replaced by older code.

7 Nov 2026: the box's wam was an older App Manager than the installed builder bundle. `wam builder-bundle doctor`
(handed from the bundle to that box wam) said the bundle was "older than (or differs from) this App Manager" and told
the agent to run `wam builder-bundle install`, which installed the box's own older code as the current bundle (no
`--kind`, no `wam verify`); the agent had to roll back by hand.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import builder_bundle as bb, bundle_delegate as bd  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OLD = ('1' * 40, 1_700_000_000)
NEW = ('2' * 40, 1_800_000_000)


def _source(tmp_path, name, stamp):
    """A complete App Manager source tree at a chosen commit (None: code from before bundle ordering)."""
    src = tmp_path / name
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    for part in ('wam', 'templates/hello-wizard', 'templates/list', 'system-skills', 'wizard-views'):
        shutil.copytree(REPO / part, src / part, ignore=ignore)
    shutil.copy2(REPO / 'LICENSE', src / 'LICENSE')
    if stamp is None:
        (src / bb.STAMP).unlink()
    else:
        (src / bb.STAMP).write_text(f'{stamp[0]} {stamp[1]}\n')
    return src


def _current(os_home):
    return os.readlink(os_home / 'builder' / 'current')


def test_stamps_order_commits_and_pre_stamp_code(tmp_path):
    assert bb.order(OLD, NEW) == -1 and bb.order(NEW, OLD) == 1 and bb.order(NEW, NEW) == 0
    assert bb.order('pre', NEW) == -1 and bb.order(NEW, 'pre') == 1
    assert bb.order(None, NEW) is None and bb.order('pre', 'pre') is None and bb.order(None, None) is None
    assert bb.stamp(_source(tmp_path, 'old', None)) == 'pre'
    assert bb.stamp(_source(tmp_path, 'new', NEW)) == NEW
    unfilled = _source(tmp_path, 'unfilled', NEW)
    (unfilled / bb.STAMP).write_text('$Format:%H %ct$\n')
    assert bb.stamp(unfilled) is None                       # an export without git attributes: order unknown
    assert bb.stamp(tmp_path / 'nothing') is None


def test_checkout_stamp_comes_from_git_and_is_written_into_the_release(tmp_path):
    if not (REPO / '.git').exists():
        pytest.skip('needs a git checkout')
    head = subprocess.run(['git', '-C', str(REPO), 'log', '-1', '--format=%H %ct'],
                          capture_output=True, text=True, check=True).stdout.split()
    assert bb.stamp(REPO) == (head[0], int(head[1]))
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home)
    assert (os_home / 'builder/current' / bb.STAMP).read_text() == f'{head[0]} {head[1]}\n'
    assert bb.stamp(os_home / 'builder/current') == (head[0], int(head[1]))


def test_install_never_replaces_a_newer_bundle_without_allow_downgrade(tmp_path):
    os_home = tmp_path / 'wizard-os'
    new = bb.install(os_home, _source(tmp_path, 'new', NEW))
    assert new['state'] == 'installed'
    for older in (_source(tmp_path, 'older', OLD), _source(tmp_path, 'pre', None)):
        kept = bb.install(os_home, older)
        assert kept['state'] == 'kept_newer' and kept['release'] == new['release'], kept
        assert '--allow-downgrade' in kept['note']
        assert _current(os_home) == 'releases/' + new['release']
    down = bb.install(os_home, _source(tmp_path, 'older2', OLD), allow_downgrade=True)
    assert down['state'] == 'installed' and _current(os_home) == 'releases/' + down['release']
    up = bb.install(os_home, _source(tmp_path, 'newer', NEW))     # upgrading is never blocked
    assert up['state'] == 'installed' and _current(os_home) == 'releases/' + new['release']


def test_install_upgrades_a_bundle_from_before_ordering(tmp_path):
    os_home = tmp_path / 'wizard-os'
    legacy = bb.install(os_home, _source(tmp_path, 'legacy', None))
    assert bb.stamp(os_home / 'builder/current') == 'pre'
    out = bb.install(os_home, _source(tmp_path, 'new', NEW))
    assert out['state'] == 'installed' and out['release'] != legacy['release']


def test_cli_install_reports_kept_newer_and_takes_allow_downgrade(tmp_path, monkeypatch, capsys):
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home, _source(tmp_path, 'new', NEW))
    monkeypatch.setenv('WIZARD_OS_HOME', str(os_home))
    monkeypatch.setattr(bb, 'SOURCE', _source(tmp_path, 'older', OLD))
    monkeypatch.setattr(bb.install, '__defaults__', (bb.SOURCE,))
    assert bb.main(['install']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'kept_newer'
    assert bb.main(['install', '--allow-downgrade']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'installed'
    assert bb.stamp(os_home / 'builder/current') == OLD


def test_doctor_keeps_a_bundle_newer_than_this_app_manager(tmp_path, monkeypatch):
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home, _source(tmp_path, 'new', NEW))
    older = _source(tmp_path, 'older', OLD)
    (older / 'wam' / 'links.py').write_text((older / 'wam' / 'links.py').read_text() + '\n# older\n')
    monkeypatch.setattr(bb, 'SOURCE', older)
    ok, detail = bb.health(os_home)[0][1:]
    assert ok is True and 'newer than the App Manager' in detail and 'builder-bundle install' not in detail
    monkeypatch.setattr(bb, 'SOURCE', _source(tmp_path, 'pre', None))   # a box wam from before ordering
    ok, detail = bb.health(os_home)[0][1:]
    assert ok is True and 'builder-bundle install' not in detail


def test_doctor_flags_a_bundle_older_than_this_app_manager(tmp_path, monkeypatch):
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home, _source(tmp_path, 'old', OLD))
    newer = _source(tmp_path, 'newer', NEW)
    (newer / 'wam' / 'links.py').write_text((newer / 'wam' / 'links.py').read_text() + '\n# newer\n')
    monkeypatch.setattr(bb, 'SOURCE', newer)
    ok, detail = bb.health(os_home)[0][1:]
    assert ok is False and 'older than' in detail and 'wam builder-bundle install' in detail
    assert bb.install(os_home, newer)['state'] == 'installed'      # the repair doctor names
    assert bb.health(os_home)[0][1] is True


def test_doctor_does_not_count_the_stamp_file_as_drift(tmp_path, monkeypatch):
    os_home = tmp_path / 'wizard-os'
    src = _source(tmp_path, 'src', NEW)
    bb.install(os_home, src)
    (src / bb.STAMP).write_text('$Format:%H %ct$\n')               # the unfilled checkout copy
    monkeypatch.setattr(bb, 'SOURCE', src)
    ok, detail = bb.health(os_home)[0][1:]
    assert ok is True and 'differs' not in detail


# --- run from the bundle: builder-bundle commands go through the bundle's own (newer) code ---------------------------

def _box(tmp_path, stamp):
    """The box's wam: a launcher that `cd`s into an App Manager source and records what it was asked to run."""
    src = tmp_path / 'box-src'
    (src / 'wam').mkdir(parents=True, exist_ok=True)
    (src / 'wam' / '__init__.py').write_text('')
    (src / bb.STAMP).unlink(missing_ok=True)
    if stamp is not None:
        (src / bb.STAMP).write_text(f'{stamp[0]} {stamp[1]}\n')
    cmd = tmp_path / 'bin' / 'wam'
    cmd.parent.mkdir(exist_ok=True)
    cmd.write_text(f'#!/usr/bin/env bash\ncd "{src}" && exec /usr/bin/python3 -m wam "$@"\n')
    cmd.chmod(0o755)
    return cmd, src


def _bundle(tmp_path):
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home, _source(tmp_path, 'bundle-src', NEW))
    return os_home, (os_home / 'builder' / 'current').resolve()


@pytest.fixture(autouse=True)
def _no_real_box(tmp_path, monkeypatch):
    monkeypatch.setenv('WAM_BOX_BIN', str(tmp_path / 'no-box-wam'))   # never this machine's own box wam


def _delegate(argv, root, cmd):
    calls, printed = [], []
    os.environ['WAM_BOX_BIN'] = str(cmd)                        # restored by _no_real_box
    env = {'WAM_BOX_BIN': str(cmd)}
    out = bd.delegate(argv, source=root, env=env, execv=lambda *a: calls.append(a), out=printed.append)
    return out, calls, printed


def test_box_source_reads_the_box_launcher(tmp_path):
    cmd, src = _box(tmp_path, OLD)
    assert bd.box_source(cmd) == src
    cmd.write_text('#!/bin/sh\nexec /usr/bin/python3 -m wam "$@"\n')
    assert bd.box_source(cmd) is None


@pytest.mark.parametrize('action', ['doctor', 'rollback'])
def test_bundle_runs_doctor_and_rollback_itself(tmp_path, action):
    _, root = _bundle(tmp_path)
    cmd, _ = _box(tmp_path, None)
    assert _delegate(['builder-bundle', action], root, cmd) == (False, [], [])


@pytest.mark.parametrize('action', ['install', 'fresh'])
def test_an_older_box_wam_never_downgrades_the_bundle(tmp_path, action):
    os_home, root = _bundle(tmp_path)
    before = _current(os_home)
    for stamp in (None, OLD):                                   # pre-ordering box code, and an older stamped one
        cmd, _ = _box(tmp_path, stamp)
        out, calls, printed = _delegate(['builder-bundle', action], root, cmd)
        assert out is True and calls == [], 'the older box wam was run'
        receipt = json.loads(printed[0])
        assert receipt['state'] == 'kept_newer' and '--allow-downgrade' in receipt['note']
        assert _current(os_home) == before


def test_allow_downgrade_reaches_the_box_wam(tmp_path):
    _, root = _bundle(tmp_path)
    cmd, _ = _box(tmp_path, None)
    out, calls, _ = _delegate(['builder-bundle', 'install', '--allow-downgrade'], root, cmd)
    assert out is True and calls[0][1][1:] == ['builder-bundle', 'install']   # a pre-flag wam would reject the flag
    cmd, _ = _box(tmp_path, OLD)
    out, calls, _ = _delegate(['builder-bundle', 'install', '--allow-downgrade'], root, cmd)
    assert calls[0][1][1:] == ['builder-bundle', 'install', '--allow-downgrade']


def test_a_newer_box_wam_installs_as_before(tmp_path):
    _, root = _bundle(tmp_path)
    cmd, _ = _box(tmp_path, (NEW[0].replace('2', '3'), NEW[1] + 60))
    out, calls, printed = _delegate(['builder-bundle', 'install'], root, cmd)
    assert out is True and printed == [] and calls[0][1][1:] == ['builder-bundle', 'install']


def test_bundle_doctor_end_to_end_with_an_older_box_wam(tmp_path):
    """The launcher an agent runs: doctor is ok, names no install, and install keeps the newer bundle."""
    os_home, root = _bundle(tmp_path)
    cmd, src = _box(tmp_path, None)
    shutil.rmtree(src / 'wam')
    shutil.copytree(REPO / 'wam', src / 'wam', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (src / bb.STAMP).unlink()                                   # the box runs code from before ordering
    (src / 'wam' / 'links.py').write_text((src / 'wam' / 'links.py').read_text() + '\n# older\n')
    env = {**os.environ, 'WAM_BOX_BIN': str(cmd), 'HOME': str(tmp_path), 'WIZARD_OS_HOME': str(os_home)}
    env.pop(bd.GUARD, None)
    launcher = os_home / 'builder' / 'current' / 'bin' / 'wam'
    p = subprocess.run([str(launcher), 'builder-bundle', 'doctor'], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    row = json.loads(p.stdout)[0]
    assert row[1] is True and 'newer than the App Manager' in row[2] and 'install' not in row[2]
    before = _current(os_home)
    p = subprocess.run([str(launcher), 'builder-bundle', 'install'], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['state'] == 'kept_newer' and _current(os_home) == before


@pytest.mark.parametrize('offered', ['unresolved', 'equal-time'])
def test_unknown_order_keeps_installed_bytes_until_explicit_override(tmp_path, monkeypatch, offered):
    os_home, root = _bundle(tmp_path)
    before = _current(os_home)
    before_bytes = (root / 'wam' / 'links.py').read_bytes()
    stamp = ('3' * 40, NEW[1]) if offered == 'equal-time' else NEW
    source = _source(tmp_path, 'unknown-source', stamp)
    if offered == 'unresolved':
        (source / bb.STAMP).write_text('$Format:%H %ct$\n')
    (source / 'wam' / 'links.py').write_bytes(before_bytes + b'\n# distinct offered bytes\n')
    assert bb.order(bb.stamp(source), bb.stamp(root)) is None
    receipt = bb.install(os_home, source)
    assert receipt['state'] == 'kept_unknown', receipt
    assert '--allow-downgrade' in receipt['note']
    assert _current(os_home) == before
    assert (os_home / 'builder/current/wam/links.py').read_bytes() == before_bytes
    monkeypatch.setattr(bb, 'SOURCE', source)
    ok, detail = bb.health(os_home)[0][1:]
    assert ok and 'order unknown, kept' in detail and 'builder-bundle install' not in detail
    explicit = bb.install(os_home, source, allow_downgrade=True)
    assert explicit['state'] == 'installed'
    assert (os_home / 'builder/current/wam/links.py').read_bytes() != before_bytes


def test_unknown_box_source_is_not_delegated_without_explicit_override(tmp_path):
    os_home, root = _bundle(tmp_path)
    before = _current(os_home)
    cmd, _ = _box(tmp_path, OLD)
    cmd.write_text('#!/bin/sh\nexec /usr/bin/python3 -m wam "$@"\n')
    out, calls, printed = _delegate(['builder-bundle', 'install'], root, cmd)
    assert out is True and calls == []
    assert json.loads(printed[0])['state'] == 'kept_unknown'
    assert _current(os_home) == before
    out, calls, printed = _delegate(['builder-bundle', 'install', '--allow-downgrade'], root, cmd)
    assert out is True and printed == [] and len(calls) == 1
    assert calls[0][1][1:] == ['builder-bundle', 'install', '--allow-downgrade']


@pytest.mark.parametrize('offered', ['unresolved', 'equal-time', 'older', 'same', 'newer'])
@pytest.mark.parametrize('damage', ['missing', 'modified'])
def test_damaged_bundle_repair_guidance_preserves_version_admission(tmp_path, monkeypatch, offered, damage):
    os_home, root = _bundle(tmp_path)
    before = _current(os_home)
    path = root / 'templates/hello-wizard/web/boot.js'
    if damage == 'missing':
        path.unlink()
    else:
        path.write_text('owner-modified bytes\n')
    damaged = bb._files(root)
    versions = {'unresolved': NEW, 'equal-time': ('3' * 40, NEW[1]),
                'older': OLD, 'same': NEW, 'newer': ('3' * 40, NEW[1] + 60)}
    source = _source(tmp_path, 'repair-source', versions[offered])
    if offered == 'unresolved':
        (source / bb.STAMP).write_text('$Format:%H %ct$\n')
    monkeypatch.setattr(bb, 'SOURCE', source)
    needs_override = offered in ('unresolved', 'equal-time', 'older')
    ok, detail = bb.health(os_home)[0][1:]
    assert not ok and 'bundle missing or modified files' in detail
    assert ('--allow-downgrade' in detail) is needs_override, detail
    if needs_override:
        assert 'explicit replacement' in detail.lower() and 'older code' in detail
        assert str(source) in detail
    assert _current(os_home) == before and bb._files(root) == damaged
    result = bb.install(os_home, source)
    if needs_override:
        assert result['state'] in ('kept_unknown', 'kept_newer')
        assert _current(os_home) == before and bb._files(root) == damaged
        result = bb.install(os_home, source, allow_downgrade=True)
    assert result['state'] == 'installed'
    assert bb.health(os_home)[0][1]
    assert root.exists() and bb._files(root) == damaged
    if offered == 'same':
        assert Path(result['preserved_modified_release']) == root


@pytest.mark.parametrize('identified', [False, True])
def test_damaged_bundle_doctor_uses_offered_box_source(tmp_path, monkeypatch, identified):
    os_home, root = _bundle(tmp_path)
    source = _source(tmp_path, 'box-source', OLD) if identified else None
    (root / 'templates/hello-wizard/web/boot.js').unlink()
    monkeypatch.setattr(bb, 'SOURCE', root)
    monkeypatch.setattr(bd, 'box_source', lambda: source)
    ok, detail = bb.health(os_home)[0][1:]
    assert not ok and '--allow-downgrade' in detail, detail
    assert (str(source) if identified else 'unidentified box source') in detail
    assert bb._keep_newer(os_home / 'builder/current', bb.stamp(source) if source else None, False)


def test_tooling_failure_does_not_recommend_explicit_downgrade(tmp_path, monkeypatch):
    os_home, _ = _bundle(tmp_path)
    monkeypatch.setattr(bb, 'SOURCE', _source(tmp_path, 'older', OLD))
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    ok, detail = bb.health(os_home)[0][1:]
    assert not ok and 'git is required' in detail
    assert '--allow-downgrade' not in detail
