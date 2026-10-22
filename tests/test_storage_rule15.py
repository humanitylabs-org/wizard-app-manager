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
