"""Affected root/manager seams only; synthetic files, no host commands or daemon.

Run the root/UID cases in the disposable root guest, never against owner data.
"""
import importlib.util
import os
from pathlib import Path
import stat
import signal
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def prepare():
    spec = importlib.util.spec_from_file_location('alongside_boundary_test', REPO / 'scripts/prepare-alongside.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('pin', ['privsep-wam:595523fb', 'registry.example:5000/team/wam@sha256:' + 'a'*64,
                                  'sha256:' + 'b'*64, 'team/wam:Release_1'])
def test_literal_image_pins_survive_clean_manager_environment(pin):
    m = prepare()
    source = {key: pin for key in m.IMAGE_KEYS}
    source.update(BASH_ENV='/agent/hook', PYTHONPATH='/agent', LD_PRELOAD='/agent.so',
                  GIT_CONFIG_GLOBAL='/agent/git', DOCKER_HOST='tcp://agent', TOKEN='not-forwarded')
    clean = m.manager_environment('none', m.image_overrides(source))
    assert {key: clean[key] for key in m.IMAGE_KEYS} == {key: pin for key in m.IMAGE_KEYS}
    assert not (set(source) - set(m.IMAGE_KEYS)) & clean.keys()


@pytest.mark.parametrize('pin', ['-v', 'repo;id', 'repo\nOTHER=x', 'repo$(id)', 'repo image',
                                  'repo@sha256:BAD', '/agent/image', ''])
def test_image_override_rejects_unsafe_values_without_inventing_fallback(pin):
    m = prepare()
    if pin:
        with pytest.raises(SystemExit, match='invalid explicit image reference'):
            m.image_overrides({'WAM_IMAGE': pin})
    else:
        assert m.image_overrides({'WAM_IMAGE': pin}) == {}


@pytest.mark.skipif(os.geteuid() != 0, reason='real UID proof runs in disposable root guest')
@pytest.mark.parametrize('pinned', [True, False])
def test_real_root_entry_hands_only_literal_pins_to_manager(tmp_path, monkeypatch, pinned):
    assert os.geteuid() == 0, 'schedule this affected check in the disposable root guest'
    m = prepare()
    m.BASE = tmp_path / 'manager'
    manager = SimpleNamespace(pw_uid=12340, pw_gid=12340, pw_dir=str(m.BASE))
    agent = SimpleNamespace(pw_uid=12341, pw_gid=12341, pw_dir='/synthetic-hermes')
    monkeypatch.setattr(m.pwd, 'getpwnam', lambda name: manager if name == 'wizard-system' else agent)
    monkeypatch.setattr(m.grp, 'getgrgid', lambda gid: SimpleNamespace(gr_name='synthetic-agent'))
    monkeypatch.setattr(m.grp, 'getgrnam', lambda name: SimpleNamespace(gr_gid=12342))
    monkeypatch.setattr(m.os, 'getgrouplist', lambda *a: [12341])
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(m.runpy, 'run_path', lambda path: {'require_mapping': lambda *a: None})
    monkeypatch.setattr(sys, 'argv', ['prepare', 'hermes', '/home/hermes/.hermes', 'branch'])
    dirty = {'WIZARD_PRIVATE_ACCESS': 'none', 'BASH_ENV': '/agent/hook', 'PYTHONPATH': '/agent',
             'DOCKER_HOST': 'tcp://untrusted', 'LD_PRELOAD': '/agent.so'}
    pins = {'WAM_IMAGE': 'privsep-wam:595523fb', 'WIZARD_AGENT_SERVICE_IMAGE': 'privsep-service:e1ba0d0564f1'}
    if pinned:
        dirty.update(pins)
    monkeypatch.setattr(m.os, 'environ', dirty)
    captured = {}
    events = []
    class ReachedInstaller(Exception):
        pass
    def command(argv, **kwargs):
        if argv[:2] == ['git', 'clone']:
            tree = Path(argv[-1])
            (tree / 'scripts').mkdir(parents=True)
            (tree / 'wam').mkdir()
            (tree / 'wam/approval_custody.py').write_bytes((REPO/'wam/approval_custody.py').read_bytes())
        if 'scripts/install-approval-custody.py' in str(argv):
            events.append('root-custody')
        if 'scripts/prepare-named-user.sh' in str(argv):
            # The trusted upstream setup changes these two dirs to group-write.
            for p in (m.BASE/'runtipi', m.BASE/'runtipi/user-config'):
                p.chmod(0o775)
            (m.BASE/'runtipi/.env').touch()
        if argv[:2] == ['runuser', '-u'] and argv[-1] == m.BASE/'wizard-app-manager/install.sh':
            assert events == ['root-custody'], 'ordinary installer requires custody to exist already'
            events.append('ordinary-install')
            assert argv[4:6] == ['env', '-i']
            captured.update(v.split('=', 1) for v in argv[6:-2])
            raise ReachedInstaller()
    m.run = command
    with pytest.raises(ReachedInstaller):
        m.main()
    for key in ('BASH_ENV', 'PYTHONPATH', 'DOCKER_HOST', 'LD_PRELOAD'):
        assert key not in captured and key not in dirty
    if pinned:
        assert all(captured[key] == value for key, value in pins.items())
    else:
        assert not set(m.IMAGE_KEYS) & captured.keys()
        assert '${WAM_IMAGE:-ghcr.io/humanitylabs-org/wizard-app-manager:latest}' in (REPO/'install.sh').read_text()
    assert stat.S_IMODE((m.BASE/'runtipi/user-config').stat().st_mode) == 0o755


def control_fixture(tmp_path):
    m = prepare()
    base = tmp_path/'manager'; base.mkdir(mode=0o755)
    root = base/'runtipi'; root.mkdir(mode=0o755)
    control = root/'user-config'; control.mkdir(mode=0o755)
    return m, base, root, control


def test_new_setup_protects_only_pinned_control_directories(tmp_path):
    m, base, root, control = control_fixture(tmp_path)
    payload = root/'app-data'; payload.mkdir(mode=0o777); payload.chmod(0o777)
    sentinel = payload/'sentinel'; sentinel.write_text('unchanged'); sentinel.chmod(0o666)
    with m.protected_runtipi_setup(base, os.getuid(), os.getgid()):
        root.chmod(0o775); control.chmod(0o775)
        (root/'.env').touch()
    assert stat.S_IMODE(root.stat().st_mode) == stat.S_IMODE(control.stat().st_mode) == 0o755
    assert stat.S_IMODE(payload.stat().st_mode) == 0o777
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o666 and sentinel.read_text() == 'unchanged'


@pytest.mark.parametrize('target', ['parent', 'runtipi', 'control', 'symlink', 'endpoint', 'lock',
                                   'endpoint-symlink', 'lock-symlink', 'hardlink'])
def test_unsafe_retained_control_refuses_before_token_or_authority_write(tmp_path, target):
    m, base, root, control = control_fixture(tmp_path)
    selected = {'parent': base, 'runtipi': root, 'control': control}.get(target)
    if selected:
        selected.chmod(0o775)
    elif target == 'symlink':
        control.rename(root/'elsewhere'); control.symlink_to(root/'elsewhere', target_is_directory=True)
    elif target.endswith('-symlink'):
        source = root/'endpoint-target'; source.write_text('unchanged')
        (control/('wizard-host-approvals.json' if target == 'endpoint-symlink' else '.wizard-host-authority.lock')).symlink_to(source)
    elif target == 'hardlink':
        source = root/'endpoint-target'; source.write_text('unchanged')
        os.link(source, control/'wizard-host-approvals.json')
    else:
        file = control/('wizard-host-approvals.json' if target == 'endpoint' else '.wizard-host-authority.lock')
        file.write_text('unsafe'); file.chmod(0o666)
    with pytest.raises((ValueError, OSError)):
        with m.protected_runtipi_setup(base, os.getuid(), os.getgid()):
            (base/'new-token').write_text('must not be written')
            pytest.fail('unsafe control reached the token-writing phase')
    assert not (base/'new-token').exists()


@pytest.mark.skipif(os.geteuid() != 0, reason='real UID proof runs in disposable root guest')
def test_foreign_control_owner_refuses_with_real_metadata(tmp_path):
    assert os.geteuid() == 0, 'requires disposable root guest'
    m, base, root, control = control_fixture(tmp_path)
    os.chown(control, 12345, 12345)
    with pytest.raises(ValueError, match='unsafe retained'):
        with m.protected_runtipi_setup(base, 12340, 12340):
            pytest.fail('foreign owner reached mutation')


@pytest.mark.skipif(os.geteuid() != 0, reason='real UID proof runs in disposable root guest')
def test_actual_manager_uid_can_sync_after_protected_setup(tmp_path):
    assert os.geteuid() == 0, 'requires disposable root guest'
    m, base, root, control = control_fixture(tmp_path)
    uid, gid = 12340, 12340
    for p in (base, root, control):
        os.chown(p, uid, gid)
    with m.protected_runtipi_setup(base, uid, gid):
        root.chmod(0o775); control.chmod(0o775); (root/'.env').touch()
    # Preload code before dropping UID. Inherit only the synthetic directory FD;
    # do not chmod pytest ancestors, /root, or any other owner's directories.
    from wam import final_host_tokens as f
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        child = os.fork()
        if child == 0:
            try:
                signal.signal(signal.SIGALRM, signal.SIG_DFL); signal.alarm(10)
                os.fchdir(directory)
                os.setgroups([]); os.setgid(gid); os.setuid(uid)
                c = SimpleNamespace(runtipi_root='.')
                assert f.write(c, {'wizardapps/synthetic': ['network:synthetic']})
                assert f._read(c)['apps'] == {'wizardapps/synthetic': ['network:synthetic']}
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(directory)
    assert (control/'wizard-host-approvals.json').stat().st_uid == uid


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_setup_directory_replacement_refuses_without_chmodding_replacement(tmp_path, replacement):
    m, base, root, control = control_fixture(tmp_path)
    with pytest.raises(ValueError, match='replaced'):
        with m.protected_runtipi_setup(base, os.getuid(), os.getgid()):
            control.rename(root/'old-control')
            if replacement == 'directory':
                control.mkdir(); control.chmod(0o777)
            else:
                control.symlink_to(root/'old-control', target_is_directory=True)
    if replacement == 'directory':
        assert stat.S_IMODE(control.stat().st_mode) == 0o777


def test_existing_setup_cannot_normalize_new_writable_controls(tmp_path):
    m, base, root, control = control_fixture(tmp_path)
    (root/'.env').touch()
    with pytest.raises(ValueError, match='became writable'):
        with m.protected_runtipi_setup(base, os.getuid(), os.getgid()):
            control.chmod(0o775)
    assert stat.S_IMODE(control.stat().st_mode) == 0o775
