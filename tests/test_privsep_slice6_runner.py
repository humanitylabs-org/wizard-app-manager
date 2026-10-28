"""Runner contract without QEMU, network or a live host."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.fixture
def runner():
    path=Path(__file__).parents[1]/'qa/privsep-slice6/run.py'
    spec=importlib.util.spec_from_file_location('slice6runner',path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_runner_argument_prelude_requires_the_staged_manifest(runner,tmp_path):
    source=tmp_path/'source'; (source/'qa/privsep-slice6').mkdir(parents=True)
    (source/'qa/privsep-slice6/guest.py').write_text('synthetic')
    sha='a'*40
    manifest=tmp_path/'source-export.json'
    manifest.write_text(json.dumps({'head_sha':sha,'baseline_sha':runner.BASELINE_SHA}))
    args,root,got=runner.arguments(['/srv/work/privsep/slice6/test-evidence-nonexistent',str(source),sha,'--preflight'])
    assert args.preflight and got==source
    manifest.write_text(json.dumps({'head_sha':'b'*40,'baseline_sha':runner.BASELINE_SHA}))
    with pytest.raises(AssertionError): runner.arguments([str(root),str(source),sha])


def test_skeleton_reuses_real_main_install_with_bus_workaround_and_finally_stop(runner,tmp_path,monkeypatch):
    source=Path(__file__).parents[1]
    runner.load_skeleton(source)
    runner.root=tmp_path; runner.source=source
    runner.receipt={'steps':[],'vms':[],'failures':[],'hermes_commit':'b'*40}
    calls=[]
    def run(name,argv,**kw):
        calls.append((name,argv,kw)); return 0
    monkeypatch.setattr(runner,'run',run); monkeypatch.setattr(runner,'save',lambda:None)
    class VM:
        pid=123; returncode=None
        def poll(self): return self.returncode
        def wait(self,**kw): self.returncode=0; return 0
    vm=VM()
    monkeypatch.setattr(runner.subprocess,'Popen',lambda *a,**kw:vm)
    monkeypatch.setattr(runner.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    key=tmp_path/'key'; key.with_suffix('.pub').write_text('ssh-ed25519 synthetic')
    runner.guest_vm('candidate','a'*40,tmp_path/'image',key)
    commands={name:kw.get('stdin',b'').decode() for name,argv,kw in calls}
    assert 'dbus-user-session' in commands['candidate-tools']
    assert 'systemctl start user@$uid.service' in commands['candidate-stock-hermes']
    assert runner.BASELINE_SHA in commands['candidate-legacy-install']
    assert 'WIZARD_PRIVATE_ACCESS=none' in commands['candidate-legacy-install']
    assert "SLICE6_SCRIPT'\n#!/usr/bin/env python3\n" in commands['candidate-slice6-script']
    for name in ('checkpoint-migrate','checkpoint','migrate','migrated','rollback','rolled-back','remigrate','again'):
        assert 'candidate-'+name in commands
    assert '--stop-after relocate' in commands['candidate-checkpoint-migrate']
    assert runner.receipt['vms'][0]['stopped']


def test_missing_migration_or_rollback_cannot_be_green(runner):
    value={'steps':[],'vms':[{'stopped':True}],'candidate_doctor_passed':True,'smoke':True,'failures':[]}
    runner.finalize(value); assert not value['passed']
