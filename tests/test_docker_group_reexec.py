"""wam picks up the docker group when its systemd user manager predates the group grant (prepared VPS rehearsal,
29 Oct 2026: Store installs failed with 'sudo: a password is required' until the user manager was restarted)."""
import os
import sys

from wam import docker_group as dg


def test_member_without_the_gid_needs_it():
    assert dg.needs_docker_group(groups=[1000], user="wizard", members=["wizard"], docker_gid=988)


def test_process_with_the_gid_or_non_member_does_not():
    assert not dg.needs_docker_group(groups=[1000, 988], user="wizard", members=["wizard"], docker_gid=988)
    assert not dg.needs_docker_group(groups=[1000], user="wizard", members=["dana"], docker_gid=988)


def test_reexecs_once_under_sg(monkeypatch):
    calls = []
    monkeypatch.setattr(dg, "needs_docker_group", lambda: True)
    monkeypatch.setattr(dg.shutil, "which", lambda name: "/usr/bin/sg" if name == "sg" else None)
    monkeypatch.setattr(dg.os, "getuid", lambda: 1000)
    env = {}
    assert dg.reexec_with_docker_group(["serve"], env=env, execvp=lambda f, a: calls.append((f, a)))
    assert env[dg.GUARD] == "1"
    (f, a), = calls
    assert f == "/usr/bin/sg" and a[:3] == ["/usr/bin/sg", "docker", "-c"]
    assert a[3].startswith("exec ") and a[3].endswith(" -m wam serve") and sys.executable in a[3]
    # The guard stops a second re-exec (sg failed to add the group, or it is already the child).
    assert not dg.reexec_with_docker_group(["serve"], env=env, execvp=lambda f, a: calls.append((f, a)))
    assert len(calls) == 1


def test_no_sg_or_root_leaves_wam_alone(monkeypatch):
    monkeypatch.setattr(dg, "needs_docker_group", lambda: True)
    monkeypatch.setattr(dg.shutil, "which", lambda name: None)
    monkeypatch.setattr(dg.os, "getuid", lambda: 1000)
    assert not dg.reexec_with_docker_group(["serve"], env={}, execvp=lambda f, a: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(dg.os, "getuid", lambda: 0)
    assert not dg.reexec_with_docker_group(["serve"], env={}, execvp=lambda f, a: (_ for _ in ()).throw(AssertionError))
