"""Fail-closed installer review regressions; all lifecycle commands are sandbox stubs."""
import pytest
from test_get_sh_any_user import Box, _local_wam, _install_alongside_block

def test_real_inner_install_delegates_scheduling_to_outer(tmp_path):
    p, calls = _install_alongside_block(tmp_path, "1", outer="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "systemd-run" not in calls
    assert "pending the outer" in p.stdout
    assert "Hermes will restart in" not in p.stdout
    assert (tmp_path / "h/.wizard-os/hermes-restart.pending").read_text().strip() == "deferred"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)

@pytest.mark.parametrize('selector', [{'WIZARD_HERMES_USER': 'missing'}, {'HERMES_HOME': '/not/a/hermes'}])
def test_bad_selector_stops_before_mutation(box, selector):
    box.hermes('root')
    p = box.run('root', **selector)
    assert p.returncode == 2, p.stdout + p.stderr
    assert not box.log.read_text()
    assert 'stop:' in box.run('root', '--check', **selector).stdout


def test_conflicting_explicit_owner_and_home_stop(box):
    hh = box.hermes('root')
    box.add_user('alice', 1001)
    box.hermes('alice')
    p = box.run('root', WIZARD_HERMES_USER='alice', HERMES_HOME=str(hh))
    assert p.returncode == 2, p.stdout + p.stderr
    assert not box.log.read_text()


def test_existing_config_without_supported_binary_stops(box):
    hh = box.hermes('root')
    (box.users['root'][1] / '.local/bin/hermes').unlink()
    p = box.run('root')
    assert p.returncode == 2, p.stdout + p.stderr
    assert not box.log.read_text()
    assert 'stop:' in box.run('root', '--check').stdout


def test_unverified_gateway_stops_without_new_agent(box):
    box.gateway_process('root', 9090, '/not/readable')
    p = box.run('root')
    assert p.returncode == 2, p.stdout + p.stderr
    assert not box.log.read_text()


def test_existing_user_is_not_granted_passwordless_sudo(box):
    box.add_user('alice', 1001)
    box.hermes('alice')
    (box.bin / 'sudo').write_text('#!/bin/sh\n[ "$1" = -iu ] || exit 1\necho "sudo $*" >> "$FAKE_LOG"\n')
    p = box.run('root')
    assert p.returncode == 0, p.stdout + p.stderr
    assert 'scripts/prepare-named-user.sh' in box.log.read_text()
    assert box.handoff().startswith('sudo -iu alice ')
    assert not list(box.sudoers.iterdir())


@pytest.mark.parametrize('user', ['root', 'alice'])
def test_custom_home_is_forwarded_to_old_path_only_install(box, tmp_path, user):
    if user != 'root':
        box.add_user(user, 1001)
    hh = box.hermes(user, home_dir=tmp_path / 'custom', where='venv')
    src = _local_wam(tmp_path, 'command -v hermes >> "$FAKE_LOG"\n')
    p = box.run(user, HERMES_HOME=str(hh), FAKE_STAT_OWNER=user,
                WAM_REPO=str(src), WAM_REF='main', WAM_DIR=str(tmp_path / 'wam'))
    assert p.returncode == 0, p.stdout + p.stderr
    assert str(hh / 'hermes-agent/venv/bin/hermes') in box.log.read_text()


@pytest.mark.parametrize('modern', [False, True])
def test_failed_install_never_schedules_restart(box, tmp_path, modern):
    box.add_user('alice', 1001)
    box.hermes('alice')
    body = ('# wizard-install: defers-hermes-restart\n'
            'echo deferred > "$HOME/.wizard-os/hermes-restart.pending"\n' if modern else
            'hermes gateway restart\n')
    src = _local_wam(tmp_path, body + 'exit 9\n')
    p = box.run('alice', WIZARD_AGENT_SAFE='1', WAM_REPO=str(src), WAM_REF='main', WAM_DIR=str(tmp_path / 'wam'))
    assert p.returncode == 9, p.stdout + p.stderr
    assert 'systemd-run' not in box.log.read_text()
    assert 'real-hermes gateway restart' not in box.log.read_text()
    assert not (box.users['alice'][1] / '.wizard-os/agent-safe.active').exists()


def test_modern_outer_installer_schedules_after_inner_completion(box, tmp_path):
    box.add_user('alice', 1001)
    box.hermes('alice')
    src = _local_wam(tmp_path, '# wizard-install: defers-hermes-restart\n'
        'test "$WIZARD_OUTER_INSTALL" = 1\n'
        'echo deferred > "$HOME/.wizard-os/hermes-restart.pending"\n'
        'echo inner-finished >> "$FAKE_LOG"\n')
    p = box.run('alice', WIZARD_AGENT_SAFE='1', WAM_REPO=str(src), WAM_REF='main', WAM_DIR=str(tmp_path / 'wam'))
    assert p.returncode == 0, p.stdout + p.stderr
    log = box.log.read_text()
    assert log.index('inner-finished') < log.index('systemd-run')


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('mismatch', [False, True])
def test_pinned_restart_waits_for_successful_release_receipt(box, tmp_path, modern, mismatch):
    import json
    from test_release_pins import FAKE_DOCKER, WAM_DIGEST, SHELL_DIGEST, OTHER, git
    box.add_user('alice', 1001)
    box.hermes('alice')
    body = ('# wizard-install: defers-hermes-restart\n'
            'echo deferred > "$HOME/.wizard-os/hermes-restart.pending"\n' if modern else
            'hermes gateway restart\n')
    src = _local_wam(tmp_path, body)
    sha = git('rev-parse', 'HEAD', cwd=src)
    rel = tmp_path / 'release'; rel.mkdir()
    (rel / 'stable').write_text('9.9.9\n')
    (rel / '9.9.9.json').write_text(json.dumps({
        'version': '9.9.9', 'components': {'wizard-app-manager': {'commit': sha}},
        'images': {
            'wizard-app-manager': {'ref': 'ghcr.io/humanitylabs-org/wizard-app-manager:latest', 'digest': WAM_DIGEST},
            'wizard-shell': {'ref': 'ghcr.io/humanitylabs-org/wizard-shell:latest', 'digest': SHELL_DIGEST}},
        'default_apps': {}}))
    (box.bin / 'docker').write_text(FAKE_DOCKER)
    (box.bin / 'systemd-run').write_text('#!/bin/sh\n'
        'test -f "$HOME/.wizard-os/release.json" || exit 8\n'
        'echo receipt-before-schedule >> "$FAKE_LOG"\n')
    p = box.run('alice', WIZARD_AGENT_SAFE='1', WIZARD_MODE='alongside', WAM_REPO=str(src),
        WAM_DIR=str(tmp_path / 'wam'), WIZARD_RELEASE_BASE=f'file://{rel}',
        RUNTIPI_DIGEST=OTHER if mismatch else WAM_DIGEST, SHELL_DIGEST=SHELL_DIGEST)
    assert p.returncode == (1 if mismatch else 0), p.stdout + p.stderr
    rec = json.loads((box.users['alice'][1] / '.wizard-os/release.json').read_text())
    assert rec['status'] == ('mismatch' if mismatch else 'pinned')
    assert ('receipt-before-schedule' in box.log.read_text()) is not mismatch
    assert 'real-hermes gateway restart' not in box.log.read_text()
