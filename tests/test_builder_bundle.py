"""Stock bundle lifecycle; no live owner config, Docker or network required."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from wam import builder_bundle as bb, registry


@pytest.fixture
def homes(tmp_path, monkeypatch):
    osh, hh = tmp_path / 'os', tmp_path / 'hermes'
    hh.mkdir()
    (hh / 'config.yaml').write_text('# owner comment\nmodel: my-model\nterminal:\n  backend: ssh\n  ssh_host: build-box\n')
    monkeypatch.setenv('WIZARD_OS_HOME', str(osh))
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    return osh, hh


def test_complete_bundle_runs_without_checkout_and_preserves_cwd(homes, tmp_path):
    osh, hh = homes
    result = bb.install(osh)
    bb.register(hh, osh)
    assert all(r[1] for r in bb.health(osh, hh))
    # Invoked from an unrelated working directory, without an editable install.
    env = {**os.environ, 'PYTHONPATH': '', 'WIZARD_OS_HOME': str(osh)}
    command = result['command']
    r = subprocess.run([command, 'new', 'proof-app', '--dev-root', 'dev'], cwd=tmp_path,
                       env=env, text=True, capture_output=True, check=True)
    source = tmp_path / Path(json.loads(r.stdout)['source'])
    assert source == tmp_path / 'dev/proof-app'
    assert (source / 'web/wizard-ui.css').is_file()
    subprocess.run([command, 'check', str(source)], cwd=tmp_path, env=env, check=True, capture_output=True)
    assert bb.install(osh)['sha256'] == result['sha256']
    assert yaml.safe_load((hh / 'config.yaml').read_text())['terminal']['ssh_host'] == 'build-box'
    assert (osh / 'system-skills/wizard-builder/references/build.md').is_file()


def test_generated_registry_cannot_delete_system_bundle(homes):
    osh, hh = homes
    bb.install(osh)
    registry.write({'apps': []}, osh)
    assert (osh / 'system-skills/wizard-builder/SKILL.md').is_file()
    assert all(r[1] for r in bb.health(osh))


@pytest.mark.parametrize('external', [False, True])
def test_name_collision_preserves_owner_skill_and_config(homes, tmp_path, external):
    osh, hh = homes
    root = tmp_path / 'external' if external else hh / 'skills'
    skill = root / 'nested/renamed' if external else root / 'wizard-builder'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: wizard-builder\n---\nowner skill')
    if external:
        (hh / 'config.yaml').write_text(yaml.safe_dump({'skills': {'external_dirs': [str(root)]}}))
    before = (hh / 'config.yaml').read_bytes()
    with pytest.raises(ValueError, match='collision'):
        bb.register(hh, osh)
    assert (hh / 'config.yaml').read_bytes() == before
    assert (skill / 'SKILL.md').read_text().endswith('owner skill')


def test_unmanaged_destination_is_not_overwritten(homes):
    osh, _ = homes
    skill = osh / 'system-skills/wizard-builder'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('custom')
    with pytest.raises(ValueError, match='Unmanaged'):
        bb.install(osh)
    assert (skill / 'SKILL.md').read_text() == 'custom'


def test_health_missing_corrupt_incompatible(homes):
    osh, _ = homes
    assert not bb.health(osh)[0][1]
    bb.install(osh)
    manifest = osh / 'builder/current/bundle.json'
    doc = json.loads(manifest.read_text())
    doc['schema'] = 999
    manifest.write_text(json.dumps(doc))
    assert 'incompatible' in bb.health(osh)[0][2]
    doc['schema'] = bb.SCHEMA
    manifest.write_text(json.dumps(doc))
    (osh / 'builder/current/templates/hello-wizard/web/boot.js').unlink()
    assert not bb.health(osh)[0][1]
    damaged = (osh / 'builder/current').resolve()
    repaired = bb.install(osh)
    assert all(r[1] for r in bb.health(osh))
    assert Path(repaired['preserved_modified_release']) == damaged
    assert bb.install(osh)['release'] == repaired['release']
    assert damaged.exists()


def test_upgrade_retains_previous_release(homes, tmp_path):
    osh, _ = homes
    first = bb.install(osh)
    source = tmp_path / 'source'
    for name in ('wam', 'templates', 'system-skills', 'wizard-views'):
        shutil.copytree(bb.SOURCE / name, source / name, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy(bb.SOURCE / 'LICENSE', source / 'LICENSE')
    p = source / 'system-skills/wizard-builder/references/build.md'
    p.write_text(p.read_text() + '\nNew release reference\n')
    second = bb.install(osh, source)
    assert first['sha256'] != second['sha256']
    assert (osh / 'builder/releases' / first['sha256'] / 'bundle.json').is_file()
    assert (osh / 'system-skills/wizard-builder/references/build.md').read_text().endswith('New release reference\n')
    assert bb.rollback(osh, first['release'])['sha256'] == first['sha256']
    assert not (osh / 'system-skills/wizard-builder/references/build.md').read_text().endswith('New release reference\n')
    with pytest.raises(ValueError):
        bb.rollback(osh, '../escape')


def test_fresh_merges_mounts_preserves_terminal_and_is_idempotent(homes, tmp_path):
    osh, hh = homes
    c = SimpleNamespace(home=osh, runtipi_root=tmp_path / 'runtipi')
    data = c.runtipi_root / 'app-data/wizardapps/hermes-agent/data'
    data.mkdir(parents=True)
    shutil.copy(hh / 'config.yaml', data / 'config.yaml')
    override = c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml'
    override.parent.mkdir(parents=True)
    override.write_text('services:\n  hermes-agent-engine:\n    image: owner/image\n    environment:\n      OWNER: kept\n    volumes:\n      - /owner:/owner:ro\n')
    result = bb.fresh(c)
    before = override.read_bytes(), (data / 'config.yaml').read_bytes()
    bb.fresh(c)
    assert (override.read_bytes(), (data / 'config.yaml').read_bytes()) == before
    service = yaml.safe_load(override.read_text())['services']['hermes-agent-engine']
    assert service['image'] == 'owner/image'
    assert service['environment']['OWNER'] == 'kept'
    assert service['volumes'][1]['read_only'] is True
    assert result['restart_required']
    assert yaml.safe_load((data / 'config.yaml').read_text())['terminal']['backend'] == 'ssh'
    assert all(r[1] for r in bb.health(osh, data, bb.RUNTIME + '/current/system-skills'))


@pytest.fixture
def wizard(homes, monkeypatch):
    loader = importlib.machinery.SourceFileLoader('wizard_test_bundle', str(bb.SOURCE / 'wizard-cli/wizard'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    pytest.importorskip('ruamel.yaml')
    monkeypatch.setattr(module, 'editor_command', lambda home: [sys.executable, '-c', module._EDITOR])
    calls = []
    def run(home, *args, **kwargs):
        calls.append(args)
        if args[:2] == ('plugins', 'install'):
            plugin = home / 'plugins' / module.PLUGIN_NAME
            plugin.mkdir(parents=True, exist_ok=True)
            (plugin / 'plugin.yaml').write_text('name: wizard-os\n')
            (plugin / 'main.py').write_text('kernel = "wizard-builder"\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module, 'run_hermes', run)
    return module, calls


def ns(hh):
    return SimpleNamespace(hermes_home=str(hh), restart=False, plugin_source=None, plugin_ref='pin', gateway=None)


def test_link_repeat_refresh_unlink_and_owner_edits(homes, wizard, tmp_path):
    osh, hh = homes
    w, calls = wizard
    before = (hh / 'config.yaml').read_bytes()
    w.cmd_link(ns(hh))
    assert str(osh / 'system-skills') in yaml.safe_load((hh / 'config.yaml').read_text())['skills']['external_dirs']
    w.cmd_link(ns(hh))
    assert any('--force' in c for c in calls)
    w.cmd_unlink(ns(hh))
    assert (hh / 'config.yaml').read_bytes() == before
    assert (osh / 'system-skills/wizard-builder/SKILL.md').is_file()
    w.cmd_link(ns(hh))
    cfg = yaml.safe_load((hh / 'config.yaml').read_text())
    cfg['skills']['external_dirs'].append(str(tmp_path / 'owner-added'))
    cfg['owner'] = 'kept'
    (hh / 'config.yaml').write_text(yaml.safe_dump(cfg))
    w.cmd_unlink(ns(hh))
    cfg = yaml.safe_load((hh / 'config.yaml').read_text())
    assert cfg['skills']['external_dirs'] == [str(tmp_path / 'owner-added')]
    assert cfg['owner'] == 'kept'


def test_modified_plugin_refresh_is_refused(homes, wizard):
    _, hh = homes
    w, _ = wizard
    w.cmd_link(ns(hh))
    p = hh / 'plugins' / w.PLUGIN_NAME / 'main.py'
    p.write_text('owner changes')
    with pytest.raises(w.Fail, match='modified'):
        w.cmd_link(ns(hh))
    assert p.read_text() == 'owner changes'
    w.cmd_unlink(ns(hh))
    assert p.read_text() == 'owner changes'


def test_failed_link_rolls_back_discovery(homes, wizard, monkeypatch):
    osh, hh = homes
    w, _ = wizard
    before = (hh / 'config.yaml').read_bytes()
    monkeypatch.setenv('WIZARD_LINK_FAULT', 'after:skills.external_dirs')
    with pytest.raises(w.Fail, match='nothing was left changed'):
        w.cmd_link(ns(hh))
    assert (hh / 'config.yaml').read_bytes() == before
    assert (osh / 'system-skills/wizard-builder/SKILL.md').is_file()


@pytest.mark.parametrize('backend,returncode,ok', [('local', 0, True), ('ssh', 0, True), ('local', 1, False), ('docker', 0, False)])
def test_runtime_health_probes_configured_route(homes, tmp_path, monkeypatch, backend, returncode, ok):
    osh, _ = homes
    c = SimpleNamespace(home=osh, runtipi_root=tmp_path / 'runtipi')
    data = c.runtipi_root / 'app-data/wizardapps/hermes-agent/data'
    data.mkdir(parents=True)
    (data / 'config.yaml').write_text(yaml.safe_dump({'terminal': {'backend': backend, 'ssh_host': 'worker', 'ssh_user': 'builder', 'ssh_key': '/opt/data/key'}}))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ['docker', 'ps']:
            return SimpleNamespace(stdout='candidate-id\n', returncode=0)
        return SimpleNamespace(stdout='help', stderr='unreachable' if returncode else '', returncode=returncode)
    monkeypatch.setattr(bb.subprocess, 'run', run)
    assert bb.runtime_health(c)[0][1] is ok
    if backend == 'ssh':
        assert 'builder@worker' in calls[-1]
        assert 'StrictHostKeyChecking=yes' in calls[-1]
    elif backend == 'local':
        assert bb.RUNTIME + '/current/bin/wam' in calls[-1]
