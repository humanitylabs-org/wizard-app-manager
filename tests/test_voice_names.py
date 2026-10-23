import json
import shutil
from pathlib import Path

import pytest
import yaml
from wam import registry, wizardmd

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'hello-wizard'

@pytest.mark.parametrize('names', [[], ['Hello', 'hello world'], ['x' * 40] * 8, ['😀' * 40]])
def test_voice_names_valid(names):
    assert wizardmd.voice_names({'voice_names': names}) == names

@pytest.mark.parametrize('names', [None, 'hello', {}, [True], [3], [''], ['  '], ['x' * 41], ['x'] * 9, ['a\nb'], ['a\x7fb']])
def test_voice_names_invalid(names):
    with pytest.raises(ValueError, match='voice_names'):
        wizardmd.voice_names({'voice_names': names})

def test_voice_names_survive_installed_apps_json(tmp_path):
    app = tmp_path / 'repos' / 'wizardapps' / 'apps' / 'hello-wizard'
    shutil.copytree(TEMPLATE, app)
    shutil.copy2(app / 'runtipi' / 'config.json', app / 'config.json')
    wm = app / 'wizard.md'
    names = ['Greeting', 'hello world']
    wm.write_text(wm.read_text().replace('wizard: 0.2', 'voice_names: ' + json.dumps(names) + '\nwizard: 0.2'))
    assert ('voice_names valid', True, '') in wizardmd.check(app)
    rows = [{'app': {'status': 'running', 'port': 8799}, 'info': {'urn': 'hello-wizard:wizardapps'}}]
    reg = registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path)
    output = registry.write(reg, tmp_path / 'owner')
    assert json.loads(output.read_text())['apps'][0]['voice_names'] == names
    wm.write_text(wm.read_text().replace(json.dumps(names), 'null'))
    assert 'voice_names valid' in wizardmd.failures(wizardmd.check(app))
    reg = registry.build(rows, 'wizardapps', '127.0.0.1', tmp_path)
    assert 'voice_names' not in json.loads(registry.write(reg, tmp_path / 'owner2').read_text())['apps'][0]

def test_legacy_manifest_adds_no_names():
    assert wizardmd.voice_names({}) == []
