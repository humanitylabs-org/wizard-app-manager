from pathlib import Path
import json
from wam import registry

def test_files_internal_port_is_main_compose_port_not_published_view_port(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_OS_HOME', str(tmp_path/'home'))
    d=tmp_path/'repos/wizardapps/apps/files'; d.mkdir(parents=True)
    (d/'wizard.md').write_text('---\nview: {path: /, port: 8794}\nprovides: [files]\n---\n')
    (d/'config.json').write_text('{"port":8794}')
    (d/'docker-compose.yml').write_text('services:\n  files:\n    x-runtipi: {is_main: true, internal_port: 8080}\n')
    e=registry._entry({'port':8794,'status':'running'}, {}, 'files:wizardapps', 'files','wizardapps','127.0.0.1',tmp_path,'internal')
    assert e['_view_upstream']=='http://files:8080/'
    e=registry._entry({'port':8794,'status':'running'}, {}, 'files:wizardapps', 'files','wizardapps','127.0.0.1',tmp_path,'host')
    assert e['_view_upstream']=='http://127.0.0.1:8794/'
