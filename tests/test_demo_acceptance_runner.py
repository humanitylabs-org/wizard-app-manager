"""Safety checks for the opt-in, demo-bound acceptance helper (no real host writes)."""
import importlib.util
from pathlib import Path
import sys

import pytest


def runner():
    spec = importlib.util.spec_from_file_location('demo_acceptance', Path(__file__).parents[1]/'scripts/demo-host-acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owner_environment_hash_excludes_only_wizard_link_credentials(tmp_path, monkeypatch):
    r = runner()
    monkeypatch.setattr(r, 'OWNER', tmp_path)
    p=tmp_path/'.hermes/.env'; p.parent.mkdir()
    p.write_text('PROVIDER_FIXTURE=unchanged\nAPI_SERVER_KEY=synthetic-old\nWIZARD_MCP_TOKEN=synthetic-old\n')
    before=r.owner_env_hash()
    p.write_text('PROVIDER_FIXTURE=unchanged\nAPI_SERVER_KEY=synthetic-new\nWIZARD_MCP_TOKEN=synthetic-new\n')
    assert r.owner_env_hash()==before
    p.write_text('PROVIDER_FIXTURE=changed\nAPI_SERVER_KEY=synthetic-new\nWIZARD_MCP_TOKEN=synthetic-new\n')
    assert r.owner_env_hash()!=before


def test_runner_refuses_nonroot_before_any_side_effect(monkeypatch):
    r=runner()
    monkeypatch.setattr(sys,'argv',['demo-host-acceptance.py','no-docker','--authorize-reset'])
    monkeypatch.setattr(r.os,'geteuid',lambda:1000)
    monkeypatch.setattr(r,'output',lambda *a:pytest.fail('Nonroot guard must precede subprocess calls'))
    with pytest.raises(RuntimeError,match='bound to root@demo'):
        r.main()


@pytest.mark.parametrize('authorize',[False,True])
def test_no_docker_requires_authorization_and_verified_backup(tmp_path,monkeypatch,authorize):
    r=runner()
    monkeypatch.setattr(sys,'argv',['demo-host-acceptance.py','no-docker']+(['--authorize-reset'] if authorize else []))
    monkeypatch.setattr(r.os,'geteuid',lambda:0)
    monkeypatch.setattr(r,'OWNER',tmp_path)
    monkeypatch.setattr(r,'P',tmp_path/'evidence')
    monkeypatch.setattr(r,'output',lambda args:'demo\n' if args==['hostname'] else 'active\n')
    monkeypatch.setattr(r,'command',lambda *a,**k:pytest.fail('No destructive command before authorization and backup'))
    with pytest.raises(RuntimeError,match='Verified backup'):
        r.main()
