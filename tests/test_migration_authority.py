"""Root migration policy query never executes a command as the agent."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def checker(monkeypatch):
    path = Path(__file__).parents[1] / 'scripts/check-migration-authority.py'
    spec = importlib.util.spec_from_file_location('migration_authority', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.os, 'getuid', lambda: 0)
    monkeypatch.setattr(mod.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    monkeypatch.setattr(mod.os, 'getgrouplist', lambda *a: [1000, 123])
    monkeypatch.setattr(mod.grp, 'getgrgid', lambda gid: SimpleNamespace(gr_name='docker' if gid == 123 else 'hermes'))
    return mod


def test_policy_denial_is_a_root_listing_not_agent_elevation(checker, monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=1, stderr='')
    monkeypatch.setattr(checker.subprocess, 'run', run)
    checker.check('hermes')
    assert calls == [(['/usr/bin/sudo', '-n', '-l', '-U', 'hermes', '--', '/usr/bin/true'],
                      {'capture_output': True, 'text': True, 'timeout': 30,
                       'env': {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}})]


@pytest.mark.parametrize('code,stderr', [(0, ''), (1, 'sudo: policy plugin failed'), (2, ''), (-9, '')])
def test_policy_allow_or_unknown_error_requires_owner_review(checker, monkeypatch, code, stderr):
    monkeypatch.setattr(checker.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=code, stderr=stderr))
    with pytest.raises(ValueError, match='owner review'):
        checker.check('hermes')


@pytest.mark.parametrize('group', ['sudo', 'wheel', 'admin'])
def test_admin_membership_is_refused_before_policy_query(checker, monkeypatch, group):
    monkeypatch.setattr(checker.grp, 'getgrgid', lambda gid: SimpleNamespace(gr_name=group))
    monkeypatch.setattr(checker.subprocess, 'run', lambda *a, **kw: pytest.fail('must refuse first'))
    with pytest.raises(ValueError, match='administrator groups'):
        checker.check('hermes')


def test_authority_checker_requires_root(checker, monkeypatch):
    monkeypatch.setattr(checker.os, 'getuid', lambda: 1000)
    with pytest.raises(ValueError, match='as root'):
        checker.check('hermes')
