import json
import pytest
from test_wam import _fake_store_app
from wam import registry

@pytest.mark.parametrize('raw,expected', [('true',True),('false',False),('"true"',False),('1',False),('null',False)])
def test_registry_voice_opt_in(tmp_path, raw, expected):
    d=_fake_store_app(tmp_path)
    p=d/'wizard.md'
    p.write_text(p.read_text().replace('---\n','---\nreceives_voice_input: '+raw+'\n',1))
    r=registry.build([{'app':{'status':'running','port':8799},'info':{'urn':'hello-wizard:wizardapps'}}], 'wizardapps','127.0.0.1',tmp_path)
    assert r['apps'][0]['receives_voice_input'] is expected
    assert registry.public_projection(r)['apps'][0]['receives_voice_input'] is expected
