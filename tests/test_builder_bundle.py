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


def test_fresh_keeps_the_wam_ownership_mark_so_fresh_agent_can_rewrite_the_overlay(homes, tmp_path):
    from wam import fresh_agent
    osh, hh = homes
    c = SimpleNamespace(home=osh, runtipi_root=tmp_path / 'runtipi')
    data = c.runtipi_root / 'app-data/wizardapps/hermes-agent/data'
    data.mkdir(parents=True)
    shutil.copy(hh / 'config.yaml', data / 'config.yaml')
    override = c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml'
    override.parent.mkdir(parents=True)
    override.write_text(fresh_agent.MARK + ' (fresh mode): test header\n# second line\n'
                        'services:\n  hermes-agent-engine:\n    environment:\n      WIZARD_OS_HOME: /wizard-os\n')
    bb.fresh(c)
    text = override.read_text()
    assert text.startswith(fresh_agent.MARK + ' (fresh mode): test header\n# second line\n')
    assert yaml.safe_load(text)['services']['hermes-agent-engine']['environment']['WIZARD_BUILDER_ROOT'] == bb.RUNTIME
    first = override.read_bytes()
    bb.fresh(c)
    assert override.read_bytes() == first
    # fresh-agent's own rewrite accepts the overlay builder-bundle touched (no overlay_not_ours).
    fresh_agent._write_overlay(c.runtipi_root, 'wizardapps', 'hermes-agent', fresh_agent.MARK + ' again\nservices: {}\n', {})


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


def test_exact_legacy_manual_skill_is_migrated_with_alias_preserved(homes):
    osh, hh = homes
    target = osh / 'system-skills/wizard-builder'
    fixture = Path(__file__).parent / 'fixtures/legacy-builder'
    shutil.copytree(fixture, target)
    alias = hh / 'skills/wizard-builder'
    alias.parent.mkdir()
    alias.symlink_to(target)
    bb.check_collisions(hh, osh)
    result = bb.install(osh)
    bb.register(hh, osh)
    assert target.is_symlink() and alias.is_symlink()
    assert 'version: 0.2.0' in (alias / 'SKILL.md').read_text()
    assert len(list((osh / 'builder').glob('legacy-skill-*'))) == 1
    assert bb.install(osh)['sha256'] == result['sha256']
    assert all(r[1] for r in bb.health(osh, hh))


def test_modified_legacy_copy_is_preserved_as_collision(homes):
    osh, hh = homes
    target = osh / 'system-skills/wizard-builder'
    shutil.copytree(Path(__file__).parent / 'fixtures/legacy-builder', target)
    p = target / 'references/build.md'
    p.write_text('owner edits')
    with pytest.raises(ValueError, match='Unmanaged'):
        bb.install(osh)
    assert p.read_text() == 'owner edits'


@pytest.mark.parametrize('kind', ['relative-external', 'expanded-external', 'create-dir', 'relative-create-dir', 'flat-local'])
def test_stock_discovery_collisions_refused_before_registration(homes, tmp_path, monkeypatch, kind):
    osh, hh = homes
    bb.install(osh)
    root = hh / 'custom'
    skills = {}
    if kind == 'relative-external':
        skills['external_dirs'] = ['custom']
    elif kind == 'expanded-external':
        monkeypatch.setenv('BUILDER_OWNER_SKILLS', str(root))
        skills['external_dirs'] = ['${BUILDER_OWNER_SKILLS}']
    elif kind == 'create-dir':
        skills['create_dir'] = str(root)
    elif kind == 'relative-create-dir':
        skills['create_dir'] = 'custom'
    else:
        root = hh / 'skills'
    skill = root / ('wizard-builder.md' if kind == 'flat-local' else 'wizard-builder/SKILL.md')
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: wizard-builder\n---\nowner')
    cfgp = hh / 'config.yaml'
    cfgp.write_text(yaml.safe_dump({'skills': skills}))
    before = cfgp.read_bytes()
    monkeypatch.chdir(tmp_path)  # deliberately outside HERMES_HOME
    with pytest.raises(ValueError, match='collision'):
        bb.register(hh, osh)
    assert cfgp.read_bytes() == before
    assert skill.read_text().endswith('owner')
    assert any(not ok and 'collision' in detail for _, ok, detail in bb.health(osh, hh))


@pytest.mark.parametrize('relative', ['nested/wizard-builder.md', 'scripts/wizard-builder.md', 'a/b/wizard-builder.md'])
def test_recursive_legacy_flat_collision(homes, relative):
    osh, hh = homes
    bb.install(osh)
    skill = hh / 'skills' / relative
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: wizard-builder\n---\nowner')
    before = (hh / 'config.yaml').read_bytes()
    with pytest.raises(ValueError, match='collision'):
        bb.register(hh, osh)
    assert (hh / 'config.yaml').read_bytes() == before


@pytest.mark.parametrize('relative', ['wizard-builder.md', 'references/wizard-builder.md', 'nested/wizard-builder.md'])
def test_package_owned_flat_markdown_is_not_a_skill_collision(homes, relative):
    osh, hh = homes
    bb.install(osh)
    package = hh / 'skills/owner-skill'
    package.mkdir(parents=True)
    (package / 'SKILL.md').write_text('---\nname: owner-skill\n---\nowner')
    note = package / relative
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text('builder reference material')
    bb.register(hh, osh)
    assert all(ok for _, ok, _ in bb.health(osh, hh))


@pytest.mark.parametrize('pinned', [True, False])
def test_legacy_link_migrates_explicit_plugin_pin(homes, wizard, monkeypatch, tmp_path, pinned):
    osh, hh = homes
    w, calls = wizard
    w.cmd_link(ns(hh))
    rec = w.read_record()
    rec.pop('plugin_ref', None)
    rec.pop('plugin_sha256', None)
    revision = '6db5046292daad9f34eefad7b06e49fe23fc22db'
    metadata = {'revision': revision, 'source': w.PLUGIN_SOURCE, 'pinned': pinned}
    (hh / 'plugins/.install-metadata.json').write_text(json.dumps({w.PLUGIN_NAME: metadata}))
    original_run = subprocess.run
    def fake_git(cmd, **kwargs):
        if cmd[0] != 'git':
            return original_run(cmd, **kwargs)
        if 'checkout' in cmd:
            checkout = Path(cmd[2]) / 'hermes-plugin/wizard-os'
            shutil.copytree(hh / 'plugins/wizard-os', checkout)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_git)
    args = ns(hh); args.plugin_ref = None
    w.refresh_owned_plugin(hh, rec, args)
    last = calls[-1]
    assert ('--ref' in last) is pinned
    if pinned:
        assert last[last.index('--ref') + 1] == revision
        assert rec['plugin_ref'] == revision
    else:
        assert rec['plugin_ref'] is None


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads every file; the foreign-owner path needs an unprivileged user')
def test_fresh_on_a_box_whose_wizard_user_is_not_the_agents_uid(homes, tmp_path, monkeypatch):
    # Demo-box fresh install (0.3.24): the new `wizard` user is uid 1001, stock Hermes remaps itself to the recipe's
    # HERMES_UID 1000 and keeps config.yaml 0640, so `builder-bundle fresh --restart` died with Errno 13 and the whole
    # install failed. The installer's passwordless sudo reads/replaces it, keeping the agent's owner and mode.
    osh, hh = homes
    c = SimpleNamespace(home=osh, runtipi_root=tmp_path / 'runtipi')
    data = c.runtipi_root / 'app-data/wizardapps/hermes-agent/data'
    data.mkdir(parents=True)
    cfg = data / 'config.yaml'
    cfg.write_text('mcp_servers:\n  wizard:\n    url: http://wizard-agent-service:8787/wizard/mcp\n')
    cfg.chmod(0)                                   # stands in for 0640 owned by another uid
    calls = tmp_path / 'sudo-calls'
    fake = tmp_path / 'fakebin'
    fake.mkdir()
    (fake / 'sudo').write_text('#!/bin/sh\n[ "$1" = -n ] && shift\necho "$*" >> "$SUDO_CALLS"\n'
                               'case "$1" in cat) chmod u+r "$3"; cat "$3"; s=$?; chmod 0 "$3"; exit $s;; esac\nexec "$@"\n')
    (fake / 'sudo').chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SUDO_CALLS', str(calls))
    monkeypatch.setattr(bb, '_foreign', lambda p: p == cfg, raising=False)
    result = bb.fresh(c)
    assert result['restart_required']
    log = calls.read_text()
    assert 'cat -- ' + str(cfg) in log and ' -m 0 ' in log and 'mv -f -- ' in log
    assert (cfg.stat().st_mode & 0o777) == 0, 'the agent keeps its own mode (and owner)'
    cfg.chmod(0o600)
    doc = yaml.safe_load(cfg.read_text())
    assert doc['mcp_servers']['wizard']['url'].endswith('/wizard/mcp'), 'owner config kept'
    assert bb.RUNTIME + '/current/system-skills' in doc['skills']['external_dirs']
    cfg.chmod(0)
    assert all(r[1] for r in bb.health(osh, data, bb.RUNTIME + '/current/system-skills'))


def test_unset_plugin_source_is_never_the_current_directory():
    """Path('') is '.', so a missing --plugin-source once copied the cwd as the plugin."""
    src = (Path(__file__).resolve().parents[1] / 'wizard-cli' / 'wizard').read_text()
    assert "Path(ns.plugin_source or '')" not in src
    assert "Path(ns.plugin_source).expanduser() if ns.plugin_source else None" in src
