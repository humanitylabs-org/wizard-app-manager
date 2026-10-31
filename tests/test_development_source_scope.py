"""Development harnesses are not hosted UI, but a test-looking served path is."""
from pathlib import Path
import pytest
from wam import system_functions

BAD = "localStorage.setItem('wos.skin','nebula'); window.__wosNativeBack();"


def app(tmp_path, source='web', dist='web'):
    (tmp_path / 'wizard.md').write_text(
        '---\nview:\n  source:\n    path: ' + source + '\n    dist: ' + dist + '\n---\n')
    (tmp_path / 'web').mkdir()
    (tmp_path / 'web/app.js').write_text('WizardApp.theme.subscribe(() => {});')
    return tmp_path


def forbidden(root):
    return [r for r in system_functions.check(root) if not r['ok']]


@pytest.mark.parametrize('directory', ['tests', 'test', 'qa', 'ops'])
def test_nonserved_development_harness_is_not_platform_implementation(tmp_path, directory):
    root = app(tmp_path)
    (root / directory).mkdir()
    (root / directory / 'acceptance.cjs').write_text(BAD)
    assert not forbidden(root)


@pytest.mark.parametrize('directory', ['tests', 'test', 'qa', 'ops'])
def test_nested_served_test_names_are_still_checked(tmp_path, directory):
    root = app(tmp_path)
    (root / 'web' / directory).mkdir()
    (root / 'web' / directory / 'app.js').write_text(BAD)
    assert forbidden(root)


@pytest.mark.parametrize(('source', 'dist'), [('tests', 'tests'), ('.', 'web'), ('web', '.'), ('../web', 'web'), ('web', '../web')])
def test_declared_or_ambiguous_served_development_files_fail_closed(tmp_path, source, dist):
    root = app(tmp_path, source, dist)
    (root / 'tests').mkdir()
    (root / 'tests' / 'app.js').write_text(BAD)
    assert forbidden(root)


def test_missing_manifest_keeps_full_source_scan(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/fixture.js').write_text(BAD)
    assert forbidden(tmp_path)


@pytest.mark.parametrize('reference', [
    "import '../tests/fixture.js'", '<script src="../tests/fixture.js"></script>',
    "fetch('../tests/fixture.js')",
])
def test_runtime_reference_prevents_development_exclusion(tmp_path, reference):
    root = app(tmp_path)
    (root / 'tests').mkdir()
    (root / 'tests/fixture.js').write_text(BAD)
    (root / 'web/app.js').write_text(reference)
    assert forbidden(root)


def test_development_symlink_cannot_hide_source(tmp_path):
    root = app(tmp_path)
    (root / 'web/secret.js').write_text(BAD)
    (root / 'tests').symlink_to(root / 'web', target_is_directory=True)
    assert forbidden(root)


def test_inline_compose_remains_checked(tmp_path):
    import base64
    root = app(tmp_path)
    (root / 'runtipi').mkdir()
    import json
    packed = base64.b64encode(json.dumps({'tests/served.js': BAD}).encode()).decode()
    import yaml
    (root / 'runtipi/docker-compose.yml').write_text(yaml.safe_dump({'services': {'app': {
        'command': ['python3', '-c', "files=json.loads(base64.b64decode('" + packed + "'))"]
    }}}))
    rows = forbidden(root)
    assert any('docker-compose.yml::tests/served.js' in r['detail'] for r in rows)


def test_design_warnings_use_the_same_nonserved_scope(tmp_path):
    from wam import builder
    root = app(tmp_path)
    (root / 'tests').mkdir()
    (root / 'tests/acceptance.mjs').write_text('window.alert("fixture");')
    assert not builder.design_rows(root)
    (root / 'web/tests').mkdir()
    (root / 'web/tests/served.js').write_text('window.alert("runtime");')
    assert any('web/tests/served.js' in r['detail'] for r in builder.design_rows(root))


@pytest.mark.parametrize(('filename', 'content'), [
    ('Dockerfile', 'FROM node:22\nCMD ["npx", "serve", "tests"]\n'),
    ('Containerfile', 'FROM node:22\nCMD npx serve tests\n'),
    ('serve.sh', '#!/bin/sh\nexec npx serve tests\n'),
    ('runtipi/docker-compose.yml', 'services:\n  app:\n    command: npx serve tests\n'),
    ('runtipi/docker-compose.yml', 'services:\n  app:\n    build: tests\n'),
])
def test_runtime_entrypoints_keep_development_trees_in_scope(tmp_path, filename, content):
    root = app(tmp_path)
    (root / 'tests').mkdir()
    (root / 'tests/app.js').write_text(BAD)
    target = root / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert forbidden(root)


@pytest.mark.parametrize('command', ['npm start', 'npm run serve-ui', 'npm test', '["npm", "run", "test"]'])
def test_runtime_package_scripts_are_followed_but_unused_tests_are_not(tmp_path, command):
    import json
    root = app(tmp_path)
    (root / 'tests').mkdir()
    (root / 'tests/app.js').write_text(BAD)
    (root / 'package.json').write_text(json.dumps({'scripts': {
        'test': 'node tests/app.js', 'start': 'npm run serve-ui',
        'serve-ui': 'npx serve tests'
    }}))
    (root / 'Dockerfile').write_text('FROM node:22\nCMD ' + command + '\n')
    assert forbidden(root)


@pytest.mark.parametrize(('filename', 'content'), [
    ('web/app.js', "// import '../tests/fixture.js'\nWizardApp.theme.get();"),
    ('web/app.js', "/* import '../tests/fixture.js' */\nWizardApp.theme.get();"),
    ('server.py', "# serve 'tests'\nprint('app')\n"),
    ('Dockerfile', '# serve tests\nFROM node:22\nCMD npx serve web\n'),
    ('runtipi/docker-compose.yml', 'services:\n  app:\n    command: npx serve web # tests is QA only\n'),
])
def test_comment_only_development_mentions_are_not_runtime_references(tmp_path, filename, content):
    root = app(tmp_path)
    (root / 'tests').mkdir()
    (root / 'tests/app.js').write_text(BAD)
    target = root / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert not forbidden(root)


def test_package_test_script_alone_does_not_make_harness_runtime(tmp_path):
    import json
    root = app(tmp_path)
    (root / 'tests').mkdir()
    (root / 'tests/app.js').write_text(BAD)
    (root / 'package.json').write_text(json.dumps({'scripts': {
        'test': 'node tests/app.js', 'start': 'npx serve web'
    }}))
    assert not forbidden(root)


def test_transitive_entrypoint_in_development_tree_is_also_runtime(tmp_path):
    root = app(tmp_path)
    for directory in ['ops', 'tests']:
        (root / directory).mkdir()
    (root / 'runtipi').mkdir()
    (root / 'runtipi/docker-compose.yml').write_text('services:\n  app:\n    command: sh ops/start.sh\n')
    (root / 'ops/start.sh').write_text('exec npx serve tests\n')
    (root / 'tests/app.js').write_text(BAD)
    assert forbidden(root)


def test_transitive_import_in_development_tree_is_also_runtime(tmp_path):
    root = app(tmp_path)
    for directory in ['qa', 'tests']:
        (root / directory).mkdir()
    (root / 'web/app.js').write_text("import '../tests/loader.js';")
    (root / 'tests/loader.js').write_text("import '../qa/app.js';")
    (root / 'qa/app.js').write_text(BAD)
    assert forbidden(root)
