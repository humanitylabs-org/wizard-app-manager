"""QA observer regressions; execution belongs only in the disposable root guest."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile

import pytest

REPO = Path(__file__).resolve().parents[1]


def observer(base, root):
    s = importlib.util.spec_from_file_location('contract_a_observer_test',
                                             REPO/'qa/privsep-storage/contract_a_guest.py')
    assert s and s.loader
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    m.BASE = base; m.ROOT = root; m.report = {}
    return m


def members(checkout, root):
    rows = [{'path': 'source', 'kind': 'file',
             'sha256': hashlib.sha256((checkout/'source').read_bytes()).hexdigest()},
            {'path': 'link', 'kind': 'symlink',
             'sha256': hashlib.sha256(os.fsencode(os.readlink(checkout/'link'))).hexdigest()}]
    (root/'source-members.json').write_text(json.dumps(rows))


def test_fetched_revision_uses_exact_manager_identity_and_root_hashes_every_member(tmp_path, monkeypatch):
    m = observer(tmp_path, tmp_path)
    checkout = tmp_path/'wizard-app-manager'; checkout.mkdir()
    (checkout/'source').write_bytes(b'exact root-fetched bytes\n')
    (checkout/'link').symlink_to('source'); members(checkout, tmp_path)
    revision = 'a'*40; commands = []
    def command(argv, **kwargs):
        assert argv == ['runuser', '-u', 'wizard-system', '--', 'git', '-C',
                        str(checkout), 'rev-parse', 'HEAD']
        assert kwargs == {'timeout': 30}
        commands.append(argv)
        return (revision+'\n').encode()
    monkeypatch.setattr(m.subprocess, 'check_output', command)
    m.fetched_source_closure(revision)
    assert m.report == {'root_fetched_complete_source_closure': True}
    # Correct HEAD is insufficient: root must still check file AND symlink bytes.
    for target in ('source', 'link'):
        m.report = {}
        if target == 'source': (checkout/target).write_bytes(b'tampered\n')
        else:
            (checkout/'source').write_bytes(b'exact root-fetched bytes\n')
            (checkout/target).unlink(); (checkout/target).symlink_to('other')
        with pytest.raises(AssertionError): m.fetched_source_closure(revision)
        assert not m.report
    assert len(commands) == 3


@pytest.mark.skipif(os.geteuid() != 0, reason='real UID proof runs in disposable root guest')
def test_real_foreign_checkout_retains_git_guard_and_ownership(tmp_path, monkeypatch):
    import shutil

    assert os.geteuid() == 0
    account = pwd.getpwnam('nobody'); assert account.pw_uid != 0
    trusted_path = '/usr/sbin:/usr/bin:/sbin:/bin'
    # Fail as a missing prerequisite, not as a false ownership refusal.
    for binary in ('runuser', 'git'):
        assert shutil.which(binary, path=trusted_path), 'missing trusted system prerequisite: ' + binary
    # Only this freshly created /run fixture becomes searchable. Never chmod
    # pytest's private ancestors, /root, /run itself, or any production checkout.
    with tempfile.TemporaryDirectory(prefix='storage-contract-a-git-', dir='/run') as name:
        base = Path(name); base.chmod(0o711)
        checkout = base/'wizard-app-manager'; checkout.mkdir(mode=0o700)
        env = {'PATH': trusted_path, 'HOME': str(base/'unused-home'),
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_CONFIG_SYSTEM': '/dev/null', 'LANG': 'C'}
        def git(*args):
            return subprocess.run(['git', '-C', str(checkout), *args], env=env,
                                  capture_output=True, check=True, timeout=30).stdout
        git('init', '-q')
        (checkout/'source').write_bytes(b'synthetic owned repository\n')
        (checkout/'link').symlink_to('source')
        git('add', 'source', 'link')
        git('-c', 'user.name=Synthetic QA', '-c', 'user.email=qa@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'synthetic source')
        revision = git('rev-parse', 'HEAD').decode().strip()
        m = observer(base, tmp_path); members(checkout, tmp_path)
        for p in [*checkout.rglob('*'), checkout]:
            os.chown(p, account.pw_uid, account.pw_gid, follow_symlinks=False)
        def ownership():
            return {str(p.relative_to(base)): (p.lstat().st_uid, p.lstat().st_gid,
                    stat.S_IMODE(p.lstat().st_mode)) for p in [checkout, *checkout.rglob('*')]}
        before = ownership()
        refused = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                 env=env, capture_output=True, timeout=30)
        assert refused.returncode != 0 and b'dubious ownership' in refused.stderr
        real_output = subprocess.check_output; commands = []
        def command(argv, **kwargs):
            assert argv == ['runuser', '-u', 'wizard-system', '--', 'git', '-C',
                            str(checkout), 'rev-parse', 'HEAD']
            assert kwargs == {'timeout': 30}; commands.append(argv)
            # Synthetic account mapping only: wizard-system does not yet exist
            # at the pre-installer checkpoint. Native install uses it unchanged.
            actual = argv[:2]+[account.pw_name]+argv[3:]
            return real_output(actual, env=env, **kwargs)
        monkeypatch.setattr(m.subprocess, 'check_output', command)
        m.fetched_source_closure(revision)
        assert m.report == {'root_fetched_complete_source_closure': True}
        assert len(commands) == 1 and ownership() == before
        refused_again = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                       env=env, capture_output=True, timeout=30)
        assert refused_again.returncode != 0 and b'dubious ownership' in refused_again.stderr
