"""Bootstrap writes must use Runtipi API, preserve tags and refuse owner conflicts."""
import pytest
import yaml
from wam.private import PrivateError, write_sealed_bootstrap, remove_sealed_bootstrap

class RT:
    def __init__(self, text=None, env=None, corrupt=False):
        self.config={'dockerCompose':text,'appEnv':env};self.calls=[];self.corrupt=corrupt
    def req(self,method,path,body=None):
        assert path=='/user-config/local:wizardapps'
        self.calls.append((method,path,body))
        if method=='PUT':
            self.config=dict(body)
            if self.corrupt:self.config['dockerCompose']=self.config['dockerCompose'].replace('!override','')
        return dict(self.config)

def recipe(network='sealed', internal=True):
    return {'services':{'provider':{'x-runtipi':{'is_main':True},'networks':[network]}},
            'networks':{network:{'internal':internal}}}

def test_exact_api_override_written_and_refreshable():
    rt=RT();write_sealed_bootstrap(rt,'wizardapps','local',recipe())
    text=rt.config['dockerCompose'];assert 'networks: !override' in text
    assert yaml.safe_load(text.replace('!override',''))=={'services':{'provider':{'networks':['sealed']}}}
    assert [x[0] for x in rt.calls]==['GET','PUT','GET']
    assert rt.config['appEnv']==''
    write_sealed_bootstrap(rt,'wizardapps','local',recipe('next'))
    assert '- next' in rt.config['dockerCompose']

@pytest.mark.parametrize('doc',[recipe(internal=False),{'services':{}},
    {'services':{'provider':{'x-runtipi':{'is_main':True},'networks':[]}}}])
def test_public_or_ambiguous_bootstrap_refused(doc):
    rt=RT()
    with pytest.raises(PrivateError):write_sealed_bootstrap(rt,'wizardapps','local',doc)
    assert not rt.calls

@pytest.mark.parametrize('text,env',[('services: {}\n',None),(None,'OWNER=value')])
def test_existing_owner_override_not_clobbered(text,env):
    rt=RT(text,env)
    with pytest.raises(PrivateError):write_sealed_bootstrap(rt,'wizardapps','local',recipe())
    assert [x[0] for x in rt.calls]==['GET']
    assert rt.config=={'dockerCompose':text,'appEnv':env}

def test_tag_stripped_in_storage_refuses_install():
    with pytest.raises(PrivateError,match='readback differs'):
        write_sealed_bootstrap(RT(corrupt=True),'wizardapps','local',recipe())

def test_remove_only_owned_override_via_api():
    rt=RT();write_sealed_bootstrap(rt,'wizardapps','local',recipe())
    remove_sealed_bootstrap(rt,'wizardapps','local')
    assert rt.config=={'dockerCompose':'','appEnv':''}
    remove_sealed_bootstrap(rt,'wizardapps','local')
    rt=RT('services: {}')
    with pytest.raises(PrivateError):remove_sealed_bootstrap(rt,'wizardapps','local')
    assert len(rt.calls)==1
