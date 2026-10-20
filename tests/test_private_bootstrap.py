"""No public shared-network interval before runtime seals a provisioned provider."""
import pytest
import yaml
from wam.private import PrivateError, write_sealed_bootstrap


def recipe(network='sealed', internal=True):
    return {'services':{'provider':{'x-runtipi':{'is_main':True},'networks':[network]}},
            'networks':{network:{'internal':internal}}}


def test_exact_internal_override_written_and_refreshable(tmp_path):
    write_sealed_bootstrap(tmp_path,'wizardapps','local',recipe())
    path=tmp_path/'user-config/wizardapps/local/docker-compose.yml'
    text=path.read_text()
    assert 'networks: !override' in text
    assert yaml.safe_load(text.replace('!override','')) == {'services':{'provider':{'networks':['sealed']}}}
    write_sealed_bootstrap(tmp_path,'wizardapps','local',recipe('next'))
    assert '- next' in path.read_text()


@pytest.mark.parametrize('doc',[recipe(internal=False),{'services':{}},
    {'services':{'provider':{'x-runtipi':{'is_main':True},'networks':[]}}}])
def test_public_or_ambiguous_bootstrap_refused(tmp_path,doc):
    with pytest.raises(PrivateError): write_sealed_bootstrap(tmp_path,'wizardapps','local',doc)
    assert not (tmp_path/'user-config/wizardapps/local/docker-compose.yml').exists()


def test_existing_owner_override_not_clobbered(tmp_path):
    path=tmp_path/'user-config/wizardapps/local/docker-compose.yml'
    path.parent.mkdir(parents=True);path.write_text('services: {}\n')
    with pytest.raises(PrivateError):write_sealed_bootstrap(tmp_path,'wizardapps','local',recipe())
    assert path.read_text() == 'services: {}\n'
