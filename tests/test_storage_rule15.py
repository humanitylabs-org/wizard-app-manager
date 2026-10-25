from pathlib import Path
import json
import pytest
from wam import wizardmd, claude_login, registry

@pytest.mark.parametrize('opts', [{'name':'fixed'}, {'external': True}, {'driver':'nfs'}, {'driver_opts':{'type':'none'}}])
def test_storage_refuses_nonportable_volumes(opts):
    assert wizardmd.storage_problems({}, {'volumes':{'login':opts}})

@pytest.mark.parametrize('p', ['../outside','/absolute','a/../b','a//b','a\\b','$HOME','a:b','a,b','a\nb',''])
def test_storage_share_path_safety(p):
    assert wizardmd.storage_problems({'shares':[{'id':'login','path':p}]},{})

def test_valid_shares_and_mounts():
    assert not wizardmd.storage_problems({'shares':[{'id':'login','path':'claude'}],
        'mounts':[{'app':'provider','share':'login','path':'/claude'}]}, {'volumes':{'state':None}})

def test_share_aware_claude_does_not_create_host_binding(tmp_path):
    app = tmp_path/'apps/wizardapps/wizard-claude'
    app.mkdir(parents=True)
    (app/'wizard.md').write_text('---\nid: wizard-claude\nshares: [{id: claude-login, path: claude}]\n---\n')
    out=claude_login.ensure(tmp_path,'wizardapps','wizard-claude',[],tmp_path/'hermes',1000,1000,False,
        {'model':{'provider':'claude-subscription'}})
    assert out['state']=='shared'
    assert not (tmp_path/'user-config/wizardapps/wizard-claude/docker-compose.yml').exists()

def test_share_aware_claude_keeps_existing_host_binding(tmp_path):
    app = tmp_path/'apps/wizardapps/wizard-claude'
    app.mkdir(parents=True)
    (app/'wizard.md').write_text('---\nid: wizard-claude\nshares: [{id: claude-login, path: claude}]\n---\n')
    override=tmp_path/'user-config/wizardapps/wizard-claude/docker-compose.yml'
    claude_login.apply(override,['engine'],tmp_path/'host-login','1000:1000')
    before=override.read_bytes()
    out=claude_login.ensure(tmp_path,'wizardapps','wizard-claude',[],tmp_path/'hermes',1000,1000,False,
        {'model':{'provider':'claude-subscription'}})
    assert out['state']=='legacy'
    assert override.read_bytes()==before

def test_registry_mirror_preserves_share_grants(tmp_path):
    root=tmp_path/'wizard-os'
    source=tmp_path/'wizard-grants.json'
    grant={'kind':'share','from':'hermes-agent','to':'wizard-claude','share':'claude-login','approved':'owner'}
    source.write_text(json.dumps({'schema':1,'grants':[grant]}))
    reg={'schema':2,'apps':[],'slots':{}}
    registry.write(reg,root)
    registry.write(reg,root)
    assert json.loads((root/'apps.json').read_text())['grants']==[grant]


def _grants_config(tmp_path):
    from types import SimpleNamespace
    (tmp_path/'state').mkdir()
    return SimpleNamespace(runtipi_root=tmp_path), tmp_path/'state'/'wizard-grants.json'

def test_cli_reads_readable_grants_file_directly(tmp_path):
    from wam import cli
    c,f=_grants_config(tmp_path);grant={'kind':'share','from':'a','to':'b','share':'s'}
    f.write_text(json.dumps({'schema':1,'grants':[grant]}))
    class NoApi:
        def req(self,*a,**k):raise AssertionError('readable file must not need the backend')
    assert cli.read_grants(c,NoApi())==[grant]

def test_cli_missing_grants_file_is_empty(tmp_path):
    from wam import cli
    c,_=_grants_config(tmp_path)
    assert cli.read_grants(c,None)==[]

def test_cli_asks_backend_when_root_only_grants_file_is_unreadable(tmp_path,monkeypatch):
    from wam import cli
    c,f=_grants_config(tmp_path);f.write_text('{}');grant={'kind':'share','from':'a','to':'b','share':'s'}
    real=Path.read_text
    def denied(self,*a,**k):
        if self==f:raise PermissionError(13,'Permission denied',str(f))
        return real(self,*a,**k)
    monkeypatch.setattr(Path,'read_text',denied)
    calls=[]
    class Api:
        def req(self,method,path,body=None,timeout=60):calls.append((method,path));return [grant]
    assert cli.read_grants(c,Api())==[grant]
    assert calls==[('GET','/apps/wizard/grants')]
