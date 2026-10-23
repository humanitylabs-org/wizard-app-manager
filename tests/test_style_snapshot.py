"""Shared style snapshot has one source; portable generation stays offline."""
import hashlib
import json
import shutil
import base64
import re
import yaml
from pathlib import Path

import pytest
from wam import builder


def test_reference_inline_package_matches_template_source():
    template = builder.REPO / 'templates/hello-wizard'
    compose = yaml.safe_load((template / 'runtipi/docker-compose.yml').read_text())
    code = next(iter(compose['services'].values()))['command'][2]
    match = re.search(r"base64.b64decode\('([^']+)'\)", code)
    assert match is not None
    blob = match.group(1)
    payload = json.loads(base64.b64decode(blob))
    assert payload['web/index.html'] == (template / 'web/index.html').read_text()
    lock = json.loads((template / 'appearance-provenance.json').read_text())
    for item in lock['files'].values():
        assert payload[item['destination']] == (template / item['destination']).read_text()


def test_snapshot_hashes_and_generated_app(tmp_path):
    template = builder.REPO / 'templates/hello-wizard'
    builder.validate_style_snapshot(template)
    app = builder.new_app('shared-style-hello', tmp_path, 'A fresh shared-style hello')
    lock = json.loads((app / 'appearance-provenance.json').read_text())
    assert lock['schema'] == 3
    assert lock['origin_repo'] == 'https://github.com/humanitylabs-org/wizard-os'
    for src, item in lock['files'].items():
        assert src.startswith('style/')
        assert hashlib.sha256((app / item['destination']).read_bytes()).hexdigest() == item['sha256']
    html = (app / 'web/index.html').read_text()
    assert html.index('boot.js') < html.index('wizard-tokens.css') < html.index('wizard-ui.css') < html.index('styles.css')
    assert html.index('/wizard-os-home/core/app-client.js') < html.index('app.js')
    assert not (app / 'web/theme-client.js').exists()


@pytest.mark.parametrize('asset', ['wizard-tokens.css', 'wizard-ui.css', 'boot.js'])
def test_drifted_distribution_refuses_generation(tmp_path, monkeypatch, asset):
    repo = tmp_path / 'wam'
    shutil.copytree(builder.REPO / 'templates', repo / 'templates')
    target = repo / 'templates/hello-wizard/web' / asset
    target.write_text(target.read_text() + '\n/* accidental independent edit */\n')
    monkeypatch.setattr(builder, 'REPO', repo)
    with pytest.raises(ValueError, match='snapshot drifted'):
        builder.new_app('drifted-app', tmp_path / 'dev')
    assert not (tmp_path / 'dev/drifted-app').exists()


def test_symlinked_snapshot_refuses_generation(tmp_path):
    template = tmp_path / 'template'
    shutil.copytree(builder.REPO / 'templates/hello-wizard', template)
    target = template / 'web/wizard-ui.css'
    source = tmp_path / 'ui.css'
    shutil.copyfile(target, source)
    target.unlink()
    target.symlink_to(source)
    with pytest.raises(ValueError, match='snapshot drifted'):
        builder.validate_style_snapshot(template)


def test_snapshot_source_and_missing_asset_fail(tmp_path):
    template = tmp_path / 'template'
    shutil.copytree(builder.REPO / 'templates/hello-wizard', template)
    lock_path = template / 'appearance-provenance.json'
    original = json.loads(lock_path.read_text())
    invalid = {**original, 'origin_repo': 'https://example.invalid/style'}
    lock_path.write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match='Invalid canonical'):
        builder.validate_style_snapshot(template)
    lock_path.write_text(json.dumps(original))
    (template / 'web/boot.js').unlink()
    with pytest.raises(ValueError, match='snapshot drifted'):
        builder.validate_style_snapshot(template)


def test_forked_kit_is_a_warning_not_an_error(tmp_path):
    from wam import builder
    import shutil
    app = tmp_path / 'app'
    shutil.copytree(builder.REPO / 'templates' / 'hello-wizard', app)
    assert builder.kit_rows(app) == []
    kit = app / 'web' / 'wizard-ui.css'
    kit.write_text(kit.read_text() + '\n.my-chat { display: flex; }\n')
    rows = builder.kit_rows(app)
    assert len(rows) == 1 and rows[0]['severity'] == 'warning' and 'styles.css' in rows[0]['detail']


def test_template_add_field_uses_look_only_names():
    from wam import builder
    html = (builder.REPO / 'templates' / 'hello-wizard' / 'web' / 'index.html').read_text()
    assert 'class="wz-field-row"' in html and 'wz-compose' not in html
