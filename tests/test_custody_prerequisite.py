"""Synthetic dependency/retirement faults. No real daemon, account or host writes.

The native official signed-out evidence is separate; these shims prove only the
real shell's dependency/provider split and the exact custody apply refusal paths.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['missing', 'present', 'package-failure', 'daemon-failure'])
def test_none_retains_dependency_without_operator_or_enrollment(tmp_path, scenario):
    home = tmp_path/'home'; (home/'runtipi').mkdir(parents=True)
    (home/'runtipi/.env').touch()  # dependency seam, not upstream provisioning
    binary = tmp_path/'bin'; binary.mkdir()
    log = tmp_path/'calls'
    def tool(name, body):
        p = binary/name; p.write_text('#!/bin/bash\nset -eu\n'+body+'\n'); p.chmod(0o755)
    tool('id', 'case "${1:-}" in -u) echo 0;; -g) echo 1234;; *) exit 0;; esac')
    tool('getent', 'printf "manager:x:1234:1234::%s:/bin/false\\n" "$SYNTH_HOME"')
    tool('usermod', 'printf "usermod %s\\n" "$*" >> "$CALLS"')
    tool('loginctl', 'printf "loginctl %s\\n" "$*" >> "$CALLS"')
    tool('systemctl', 'printf "systemctl %s\\n" "$*" >> "$CALLS"; exit "$DAEMON_EXIT"')
    tool('curl', 'printf "curl %s\\n" "$*" >> "$CALLS"; /usr/bin/touch "${@: -1}"')
    # A synthetic package installer, not an official-package positive proof.
    tool('sh', 'printf "official-installer\\n" >> "$CALLS"; [ "$PACKAGE_EXIT" = 0 ] || exit "$PACKAGE_EXIT"; /bin/cp "$TEMPLATE" "$BIN/tailscale"')
    template = tmp_path/'tailscale'; template.write_text('#!/bin/bash\nprintf "tailscale %s\\n" "$*" >> "$CALLS"\n'); template.chmod(0o755)
    if scenario == 'present':
        (binary/'tailscale').write_bytes(template.read_bytes()); (binary/'tailscale').chmod(0o755)
    # No system binary search: a missing CLI really is absent in this fixture.
    for name in ('cut', 'mktemp', 'rm'):
        (binary/name).symlink_to('/usr/bin/'+name)
    env = {'PATH': str(binary), 'HOME': str(home), 'WIZARD_PRIVATE_ACCESS': 'none',
           'SYNTH_HOME': str(home), 'CALLS': str(log), 'TEMPLATE': str(template), 'BIN': str(binary),
           'PACKAGE_EXIT': '9' if scenario == 'package-failure' else '0',
           'DAEMON_EXIT': '8' if scenario == 'daemon-failure' else '0', 'TMPDIR': str(tmp_path)}
    p = subprocess.run(['/bin/bash', str(REPO/'scripts/prepare-named-user.sh'), 'manager'],
                       env=env, capture_output=True, text=True, timeout=20)
    calls = log.read_text()
    assert not any('tailscale '+op in calls for op in ('up', 'login', 'set', 'serve', 'ssh', 'funnel', 'debug'))
    if scenario == 'package-failure':
        assert p.returncode == 9 and 'systemctl' not in calls
    else:
        assert 'systemctl enable --now tailscaled' in calls
        assert p.returncode == (8 if scenario == 'daemon-failure' else 0)
    assert ('official-installer' in calls) == (scenario != 'present')
    assert ('https://tailscale.com/install.sh' in calls) == (scenario != 'present')


@pytest.fixture
def atomic():
    spec = importlib.util.spec_from_file_location('atomic_fault_fixture', REPO/'tests/test_custody_install_atomic.py')
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    t = m.CustodyInstallAtomicTests(); t.setUp()
    # The test module deliberately replaces routes in setUp; restore ONLY its
    # actual function from exact reviewed bytes to test real CLI/JSON inspection.
    s = importlib.util.spec_from_file_location('real_route_inspection', REPO/'scripts/install-approval-custody.py')
    assert s and s.loader
    original = importlib.util.module_from_spec(s); s.loader.exec_module(original)
    t.installer.routes = original.routes
    yield t, original
    t.doCleanups()


@pytest.mark.parametrize('action', ['install', 'remove'])
@pytest.mark.parametrize('fault', ['missing-cli', 'failed-cli', 'malformed-json', 'unknown-null',
                                  'unknown-list', 'unknown-web-type'])
def test_inspection_fault_preserves_existing_contract_and_origin(atomic, monkeypatch, action, fault):
    t, actual = atomic
    t.seed_contract()
    origin = t.state/'approval-origin.json'
    origin.write_text('{"custody":1,"origin":"https://fixture.example.ts.net:8446","retired":["https://old.example.ts.net:8447"]}')
    before, old_origin = t.snapshot(), origin.read_bytes()
    def inspect(*args):
        assert args == ('tailscale', 'serve', 'status', '--json')
        if fault == 'missing-cli': raise FileNotFoundError()
        if fault == 'failed-cli': raise subprocess.CalledProcessError(1, ['tailscale'])
        value = {'malformed-json':'{', 'unknown-null':'null', 'unknown-list':'[]', 'unknown-web-type':'{"Web":[]}'}[fault]
        return SimpleNamespace(stdout=value)
    monkeypatch.setattr(actual, 'run', inspect)
    with pytest.raises((OSError, subprocess.CalledProcessError, ValueError, AttributeError)):
        t.installer.apply(action, t.user, 'synthetic-manager', t.state, Path('/opt/synthetic'), t.custody)
    assert t.snapshot() == before and origin.read_bytes() == old_origin
    assert t.active[t.custody.SOCKET] == 'active' and t.calls == []


@pytest.mark.parametrize('partial', ['socket-fragment', 'dependency-fragment', 'enabled-link', 'loaded-only'])
def test_partial_contract_never_becomes_first_install(atomic, monkeypatch, partial):
    t, _ = atomic
    t.installer.routes = lambda: []
    if partial == 'socket-fragment': t.socket.write_text('malformed retained socket')
    elif partial == 'dependency-fragment':
        t.dep.parent.mkdir(); t.dep.write_text('retained dependency')
    elif partial == 'enabled-link': t.link(t.custody.SOCKET, 'custom.target.wants')
    else:
        old = t.installer.loaded
        monkeypatch.setattr(t.installer, 'loaded', lambda u: {'LoadState':'loaded'} if u == t.custody.SOCKET else old(u))
    before = t.snapshot()
    t.custody.require_custody.side_effect = ValueError('partial custody refused')
    with pytest.raises(ValueError, match='partial custody refused'): t.apply()
    assert t.snapshot() == before
    assert not any(c[:2] in (('systemctl','enable'), ('systemctl','stop')) for c in t.calls)


@pytest.mark.parametrize('fault', ['off-failed', 'still-routed', 'final-inspect-failed'])
def test_retirement_failure_never_releases_existing_listener(atomic, monkeypatch, fault):
    t, actual = atomic
    t.seed_contract()
    authority = 'fixture.example.ts.net:8446'
    origin = t.state/'approval-origin.json'
    origin.write_text(json.dumps({'custody':1, 'origin':'https://'+authority, 'retired':[]}))
    before, saved = t.snapshot(), origin.read_bytes()
    t.installer.routes = lambda: [authority]
    if fault == 'off-failed':
        monkeypatch.setattr(t.installer, 'run', lambda *a: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ['tailscale'])))
    else:
        monkeypatch.setattr(t.installer, 'status', lambda: {'Web':{authority:{}}} if fault == 'still-routed' else {'Web':{}})
        if fault == 'final-inspect-failed':
            count = 0
            def routes():
                nonlocal count
                count += 1
                if count == 1: return [authority]
                raise RuntimeError('final inspect failed')
            monkeypatch.setattr(t.installer, 'routes', routes)
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        t.installer.apply('remove', t.user, 'synthetic-manager', t.state, Path('/opt/synthetic'), t.custody)
    assert t.snapshot() == before and origin.read_bytes() == saved
    assert t.active[t.custody.SOCKET] == 'active'
    assert not any(c[:2] in (('systemctl','disable'), ('systemctl','stop')) for c in t.calls)
