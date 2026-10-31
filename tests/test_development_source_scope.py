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
