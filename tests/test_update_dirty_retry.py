"""Recover only the installer's own interrupted clean checkout, never developer edits."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wam import updates
from test_release_pins import box, git, STUB_INSTALL
from test_updates import _base, _pin, _ok_runner


def cut_checkout(box):
    script = box['src'] / 'install.sh'
    # The first run zeroes tracked data then hard-kills only its sandbox parent.
    script.write_text('''#!/bin/bash
if [ ! -f "$HOME/cut-once" ]; then
  touch "$HOME/cut-once"
  : > "$WAM_DIR/install.sh"
  kill -KILL "$PPID"
  exit 0
fi
''' + STUB_INSTALL)
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'cut fixture', cwd=box['src'])
    release = box['rel'] / '9.9.9.json'
    rel = json.loads(release.read_text())
    rel['components']['wizard-app-manager']['commit'] = git('rev-parse', 'HEAD', cwd=box['src'])
    release.write_text(json.dumps(rel))
    p, _ = box['run']()
    assert p.returncode == -9, p.stdout + p.stderr
    assert (box['home'] / 'wam/install.sh').read_bytes() == b''
    receipt = box['home'] / '.wizard-os/managed-checkout.json'
    return receipt


@pytest.mark.parametrize('reboot', [False, True])
def test_killed_pinned_installer_repairs_tracked_files_and_finishes(box, reboot):
    receipt = cut_checkout(box)
    if reboot:
        st = json.loads(receipt.read_text())
        st.update(boot_id='previous-boot', pid=os.getpid())  # PID reuse on another boot
        receipt.write_text(json.dumps(st))
    keep = box['home'] / 'wam/owner-note.txt'
    keep.write_text('must survive')
    runtime = keep.parent / 'runtime'; runtime.mkdir()
    (runtime / 'state').write_text('ignored owner data')
    (keep.parent / '.git/info/exclude').write_text('runtime/\n')
    p, rec = box['run']()
    assert p.returncode == 0, p.stdout + p.stderr
    assert 'Recovered tracked App Manager files' in p.stdout
    assert rec['status'] == 'pinned' and rec['release'] == '9.9.9'
    assert git('status', '--porcelain', '--untracked-files=no', cwd=keep.parent) == ''
    assert keep.read_text() == 'must survive'
    assert (runtime / 'state').read_text() == 'ignored owner data'
    assert not receipt.exists()


@pytest.mark.parametrize('bad', ['missing', 'malformed', 'live', 'wrong-path', 'wrong-target', 'wrong-repo',
                                 'unpinned', 'dev-sha'])
def test_recovery_never_erases_unproven_or_developer_changes(box, bad):
    receipt = cut_checkout(box)
    st = json.loads(receipt.read_text())
    extra = {}
    if bad == 'missing': receipt.unlink()
    elif bad == 'malformed': receipt.write_text('not-json')
    elif bad == 'unpinned':
        (receipt.parent / 'release.json').write_text('{"status":"unpinned","reason":"WAM_REF=main"}')
    elif bad == 'dev-sha':
        extra['WAM_REF'] = git('rev-parse', 'HEAD', cwd=box['src'])
    else:
        st.update({'live': {'pid': os.getpid()}, 'wrong-path': {'directory': '/not-this-checkout'},
                   'wrong-target': {'target': 'a' * 40}, 'wrong-repo': {'repo': 'other'}}[bad])
        receipt.write_text(json.dumps(st))
    p, _ = box['run'](**extra)
    assert p.returncode != 0 and 'has local changes' in p.stderr
    assert (box['home'] / 'wam/install.sh').read_bytes() == b''


def test_completed_run_retires_proof_so_later_developer_edits_are_refused(box):
    p, _ = box['run']()
    assert p.returncode == 0
    receipt = box['home'] / '.wizard-os/managed-checkout.json'
    assert not receipt.exists()
    script = box['home'] / 'wam/install.sh'
    script.write_text('developer work\n')
    p, _ = box['run']()
    assert p.returncode == 1 and 'has local changes' in p.stderr
    assert script.read_text() == 'developer work\n'


def test_unreadable_git_index_is_refused_without_overwriting_checkout(box):
    p, _ = box['run']()
    assert p.returncode == 0
    checkout = box['home'] / 'wam'
    (checkout / '.git/index').write_bytes(b'corrupt-index')
    p, _ = box['run']()
    assert p.returncode == 1 and 'Cannot verify App Manager checkout' in p.stderr
    assert (checkout / '.git/index').read_bytes() == b'corrupt-index'
    assert not (box['home'] / '.wizard-os/managed-checkout.json').exists()


@pytest.mark.parametrize('have', [None, '0.3.32', '0.3.33'])
def test_failed_retry_before_readback_is_visible_with_reason_and_retry_target(tmp_path, have):
    h = tmp_path / 'wos'; h.mkdir()
    if have: _pin(h, have)
    (h / 'install-status.json').write_text(json.dumps({'state': 'failed', 'exit_code': 1,
        'message': 'App Manager checkout has local changes; pinned install refused.'}))
    (h / 'release-pins.json').write_text(json.dumps({'version': '0.3.33',
        'components': {'wizard-app-manager': {'commit': 'a' * 40}}}))
    st = updates.status(h, (tmp_path / 'offline').as_uri())
    assert st['failed'] is True and 'local changes' in st['failure_reason']
    assert st['installed'] == have and st['interrupted'] is False and st['installing'] is False
    calls = []
    assert updates.apply(h, (tmp_path / 'offline').as_uri(), _ok_runner(calls))['started'] is True
    assert 'WIZARD_RELEASE=0.3.33' in calls[0][-1]
    _pin(h, '0.3.33')
    (h / 'install-status.json').write_text('{"state":"done"}')
    st = updates.status(h, _base(tmp_path, '0.3.33'))
    assert st['failed'] is False and st['failure_reason'] is None


def test_failed_status_without_message_still_has_exit_reason(tmp_path):
    h = tmp_path / 'wos'; h.mkdir()
    _pin(h, '0.3.32')
    (h / 'install-status.json').write_text('{"state":"failed","exit_code":7}')
    st = updates.status(h, _base(tmp_path, '0.3.33'))
    assert st['failed'] and 'exit 7' in st['failure_reason']


def test_detached_status_records_real_failure_line_and_clears_it_on_success(tmp_path):
    h = tmp_path / 'wos'; h.mkdir()
    _pin(h, '0.3.32')
    status = h / 'install-status.json'
    status.write_text('{"state":"running"}')
    log = h / 'install.log'
    log.write_text('Installing Wizard OS 0.3.33\n\x1b[31mcheckout has local changes; refusing pinned install.\x1b[0m\n')
    source = (Path(__file__).resolve().parents[1] / 'get.sh').read_text()
    code = source.split("STATUS_PY='\n", 1)[1].split("\n'\ninstall_status()", 1)[0]
    subprocess.run([sys.executable, '-c', code, str(status), str(log), 'end', '1'], check=True)
    st = updates.status(h, _base(tmp_path, '0.3.33'))
    assert st['failed'] and st['failure_reason'] == 'checkout has local changes; refusing pinned install.'
    subprocess.run([sys.executable, '-c', code, str(status), str(log), 'end', '0'], check=True)
    st = updates.status(h, _base(tmp_path, '0.3.33'))
    assert st['failed'] is False and st['failure_reason'] is None
