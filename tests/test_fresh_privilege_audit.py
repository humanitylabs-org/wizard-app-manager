"""Fresh-box sudo audit regressions. No root, Docker daemon or network needed."""
import ast
from pathlib import Path
import subprocess
import sys

import pytest

from wam import agent_link, builder_bundle, links

ROOT = Path(__file__).resolve().parents[1]


def test_fresh_install_has_no_passwordless_sudo():
    bootstrap = (ROOT / 'get.sh').read_text()
    prep = (ROOT / 'scripts/prepare-fresh-user.sh').read_text()
    assert 'NOPASSWD:ALL' not in bootstrap
    assert 'usermod -aG docker "$WUSER"' in bootstrap
    assert '/root/.ssh/authorized_keys' in bootstrap
    assert 'prepare_fresh_user\n' in bootstrap
    # Literal empty argv in sudoers; both targets installed before revoking old grant.
    assert 'NOPASSWD: /usr/local/libexec/wizard-os/os-patch ""' in prep
    assert prep.index('visudo -cf') < prep.index('"$sudoers/wizard-os-helpers"') < prep.index('rm -f "$legacy"')
    assert 'install -o root -g root -m 0755' in prep
    assert 'install -o root -g root -m 0440' in prep
    assert 'bash "$work/prepare.sh" "$owner"' in prep  # sets Tailscale operator as root


def test_wam_modules_only_call_sudo_for_the_fixed_server_access_helper():
    # Slice 5 adds the spec's one named root operation, not a generic sudo escape.
    # Allow only the exact argv expression inside developer.helper; a second sudo
    # site, other command, variable argv or extra argument still fails this audit.
    offending = []
    allowed = []
    for path in (ROOT / 'wam').rglob('*.py'):
        tree = ast.parse(path.read_text())
        approved = None
        if path == ROOT / 'wam/developer.py':
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'helper')
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and
                     isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and
                     n.func.value.id == 'subprocess' and n.func.attr == 'run']
            assert len(calls) == 1
            argv = calls[0].args[0]
            assert ast.dump(argv, include_attributes=False) == ast.dump(ast.parse("['sudo', '-n', HELPER, action]", mode='eval').body, include_attributes=False)
            approved = argv.elts[0]
            from wam import developer
            assert developer.HELPER == '/usr/local/libexec/wizard-os/server-access'
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (node.value == 'sudo' or 'sudo -n' in node.value):
                if node is approved:
                    allowed.append(path)
                else:
                    offending.append(f'{path.relative_to(ROOT)}:{node.lineno}')
    assert allowed == [ROOT / 'wam/developer.py']
    assert not offending, offending


@pytest.mark.parametrize('args', [['true'], ['--help'], ['apply'], ['/etc/passwd'], [';id'], ['--', 'x']])
def test_os_patch_refuses_all_arguments(args):
    p = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/root-helpers/os-patch'), *args],
                       capture_output=True, text=True, timeout=5)
    assert p.returncode != 0
    assert 'takes no arguments' in p.stderr


def test_os_patch_ignores_caller_environment():
    source = (ROOT / 'scripts/root-helpers/os-patch').read_text()
    assert source.startswith('#!/usr/bin/python3 -I\n')
    assert 'os.environ' not in source
    assert 'shell=True' not in source
    assert 'origin=Ubuntu,archive=${distro_codename}-security' in source
    assert 'origin=Debian,label=Debian-Security' in source
    assert "'/run/wizard-os-patch.lock'" in source
    assert "policy.write_text('#!/bin/sh\\nexit 101\\n')" in source
    assert "'NEEDRESTART_SUSPEND': '1'" in source


def test_agent_link_docker_failure_has_clear_message_and_no_sudo(monkeypatch):
    calls = []
    def fail(argv, **kw):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv, stderr='permission denied')
    monkeypatch.setattr(agent_link.subprocess, 'run', fail)
    with pytest.raises(ValueError, match='administrator.*host preparation'):
        agent_link._docker()
    assert calls == [['docker', 'ps', '-q']]


def test_tailscale_failure_never_elevates(monkeypatch):
    calls = []
    def fail(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', 'Access denied')
    monkeypatch.setattr(links.subprocess, 'run', fail)
    out = links._run(['tailscale', 'serve', 'status', '--json'])
    assert out.returncode == 1 and 'administrator' in out.stderr
    assert len(calls) == 1


def test_foreign_file_is_not_a_general_root_file_operation(tmp_path):
    with pytest.raises(PermissionError, match='ask its owner'):
        builder_bundle._agent_config(tmp_path / 'config.yaml', 'not a shell command')
