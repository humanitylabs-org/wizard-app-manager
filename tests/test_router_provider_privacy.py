"""Router provider privacy must survive the App Manager projection."""
import pytest
from wam import registry
from test_private_fork_registry import fixture

@pytest.mark.parametrize('marker,expected', [(None,False),('true',True),('false',False),('"true"',False)])
def test_only_explicit_boolean_manifest_private_marks_provider(tmp_path, marker, expected):
    row=fixture(tmp_path,'provider','wizardapps',private=True)
    manifest=tmp_path/'apps/wizardapps/provider/wizard.md'
    text=manifest.read_text()
    if marker is not None:
        text=text.replace('version: 0.2.0', 'version: 0.2.0\nprivate: '+marker)
        manifest.write_text(text)
    projected=registry.build([row],'wizardapps','127.0.0.1',tmp_path)['apps'][0]
    assert (projected.get('private') is True) is expected
