"""The VM driver must not turn its environment into a product failure."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'qa/maintain' / (name + '.py'))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_suite_command_uses_ordinary_owner_and_clean_environment(monkeypatch, tmp_path):
    vm = load('quiet_restart_vm')
    monkeypatch.setenv('WIZARD_OS_HOME', '/root/unrelated-owner')
    monkeypatch.setenv('HERMES_HOME', '/root/unrelated-agent')
    monkeypatch.setenv('PATH', '/fixture/node/bin:/usr/bin:/bin')
    argv = vm.suite_command(tmp_path, tmp_path / 'home', tmp_path / 'tmp')
    assert argv[:7] == ['runuser', '-u', 'wam-suite-qa', '--', 'env', '-i', 'HOME=' + str(tmp_path / 'home')]
    assert 'PATH=/fixture/node/bin:/usr/bin:/bin' in argv
    assert 'PYTHONPATH=' + str(tmp_path) in argv
    assert argv[-5:] == ['python3', '-m', 'pytest', '-q', str(tmp_path / 'tests')]
    assert not any(arg.startswith(('WIZARD_OS_HOME=', 'HERMES_HOME=')) for arg in argv)


@pytest.mark.parametrize('active', ['inactive', 'failed', 'active', 'activating'])
def test_native_apt_both_timers_and_services_are_masked_and_quiescent(monkeypatch, active):
    vm = load('quiet_restart_vm')
    calls, saved = [], []
    def cmd(*args, **kwargs):
        calls.append(args)
        return active if 'is-active' in args else 'masked'
    monkeypatch.setattr(vm, 'cmd', cmd)
    monkeypatch.setattr(vm, 'save', lambda *a: saved.append(a))
    if active in ('active', 'activating'):
        with pytest.raises(AssertionError):
            vm.isolate_native_apt()
        assert saved == []
    else:
        vm.isolate_native_apt()
        assert saved[0][0] == 'native-apt-isolation'
    assert calls[0] == ('systemctl', 'mask', '--now', *vm.NATIVE_APT)
    assert {'apt-daily.timer', 'apt-daily-upgrade.timer', 'apt-daily.service',
            'apt-daily-upgrade.service', 'unattended-upgrades.service'} == set(vm.NATIVE_APT)


def test_failed_suite_keeps_its_real_status_and_log(monkeypatch, tmp_path):
    vm = load('quiet_restart_vm')
    monkeypatch.setattr(vm, 'OUT', tmp_path)
    monkeypatch.setattr(vm.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout='failed\n', stderr='diagnostic'))
    assert vm.run_suite(tmp_path, 'suite', 'suite.log') == 1
    assert (tmp_path / 'suite.log').read_text() == 'failed\ndiagnostic'
    assert json.loads((tmp_path / 'suite.json').read_text())['returncode'] == 1


@pytest.mark.parametrize('collect_fails', [False, True])
def test_cleanup_stops_and_reads_back_vm_even_if_collection_fails(monkeypatch, tmp_path, collect_fails):
    host = load('quiet_restart_thales')
    monkeypatch.setattr(host, 'OUT', tmp_path)
    calls = []
    def collect():
        if collect_fails:
            raise RuntimeError('collection failed')
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='{"status":"Stopped"}\n' if 'list' in args else '', stderr='', returncode=0)
    monkeypatch.setattr(host, 'collect', collect)
    monkeypatch.setattr(host, 'run', run)
    if collect_fails:
        with pytest.raises(RuntimeError, match='collection failed'):
            host.cleanup()
    else:
        host.cleanup()
    assert calls == [[host.LIMA, 'stop', '--force', host.VM], [host.LIMA, 'list', '--json', host.VM]]
    assert json.loads((tmp_path / 'vm-final-state.jsonl').read_text())['status'] == 'Stopped'


def test_existing_vm_refusal_does_not_stop_it(monkeypatch, tmp_path):
    host = load('quiet_restart_thales')
    monkeypatch.setattr(host, 'ROOT', tmp_path)
    monkeypatch.setattr(host, 'OUT', tmp_path / 'out')
    monkeypatch.setenv('WIZARD_HEAVY_ROOT', str(tmp_path))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='{"name":"wos-maint-qa","status":"Running"}', returncode=0)
    monkeypatch.setattr(host, 'run', run)
    monkeypatch.setattr(host, 'collect', lambda: pytest.fail('Must not touch existing VM'))
    with pytest.raises(AssertionError, match='already exists'):
        host.main()
    assert calls == [[host.LIMA, 'list', '--json', host.VM]]


def test_fresh_vm_template_has_pinned_image_and_no_owner_mounts():
    doc = yaml.safe_load((ROOT / 'qa/maintain/quiet_restart_lima.yaml').read_text())
    assert doc['mounts'] == [] and doc['upgradePackages'] is False
    assert doc['containerd'] == {'user': False, 'system': False}
    assert len(doc['images']) == 1 and doc['images'][0]['digest'].startswith('sha256:')
    assert 'systemctl mask --now' in doc['provision'][0]['script']
    assert 'apt-daily-upgrade.service' in doc['provision'][0]['script']
