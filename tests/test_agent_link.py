"""Fresh mode links the hermes-agent app to the Agent Service gateway (release 0.2.2 e2e gap)."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from wam import agent_link


def run_edit(home: Path, token='tok-1', probe=False):
    env = {**os.environ, 'HERMES_HOME': str(home)}
    argv = [sys.executable, '-c', agent_link.EDIT, agent_link.GATEWAY_URL, 'wizard', str(home / 'skills-view')]
    p = subprocess.run(argv + (['probe'] if probe else []), input=token + '\n', capture_output=True, text=True, env=env)
    return p


def test_edit_adds_gateway_skills_and_token_then_is_idempotent(tmp_path):
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'model': {'provider': 'p', 'default': 'm'},
                                                          'mcp_servers': {'other': {'url': 'http://x/mcp'}},
                                                          'skills': {'external_dirs': ['/opt/wizard-builder/current/system-skills']}}))
    (tmp_path / '.env').write_text('KEEP=1\n')
    os.chmod(tmp_path / '.env', 0o600)
    p = run_edit(tmp_path)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['config_changed'] and out['env_changed']
    cfg = yaml.safe_load((tmp_path / 'config.yaml').read_text())
    assert cfg['model'] == {'provider': 'p', 'default': 'm'}
    assert cfg['mcp_servers']['other'] == {'url': 'http://x/mcp'}
    assert cfg['mcp_servers']['wizard'] == {'url': agent_link.GATEWAY_URL,
                                            'headers': {'Authorization': 'Bearer ${WIZARD_MCP_TOKEN}'}}
    assert cfg['skills']['external_dirs'] == ['/opt/wizard-builder/current/system-skills', str(tmp_path / 'skills-view')]
    assert (tmp_path / '.env').read_text() == 'KEEP=1\nWIZARD_MCP_TOKEN=tok-1\n'
    assert oct((tmp_path / '.env').stat().st_mode & 0o777) == '0o600'
    assert 'tok-1' not in (tmp_path / 'config.yaml').read_text(), 'the token lives only in .env'
    again = json.loads(run_edit(tmp_path).stdout)
    assert not again['config_changed'] and not again['env_changed']


def test_edit_rotates_token_without_duplicates(tmp_path):
    (tmp_path / 'config.yaml').write_text('{}\n')
    run_edit(tmp_path, 'a')
    out = json.loads(run_edit(tmp_path, 'b').stdout)
    assert out['env_changed'] and not out['config_changed']
    assert (tmp_path / '.env').read_text() == 'WIZARD_MCP_TOKEN=b\n'


def test_edit_refuses_foreign_wizard_server(tmp_path):
    raw = yaml.safe_dump({'mcp_servers': {'wizard': {'url': 'http://someone-else/mcp'}}})
    (tmp_path / 'config.yaml').write_text(raw)
    p = run_edit(tmp_path)
    assert p.returncode != 0 and 'not Wizard OS' in p.stderr
    assert (tmp_path / 'config.yaml').read_text() == raw


def test_edit_lists_visible_pointer_skills(tmp_path):
    (tmp_path / 'config.yaml').write_text('{}\n')
    (tmp_path / 'skills-view' / 'wizard-job-video-editing').mkdir(parents=True)
    (tmp_path / 'skills-view' / 'other').mkdir()
    assert json.loads(run_edit(tmp_path).stdout)['skills_visible'] == ['wizard-job-video-editing']


def _cfg(tmp_path):
    root = tmp_path / 'runtipi'
    (root / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/skills').mkdir(parents=True)
    return SimpleNamespace(runtipi_root=root, home=tmp_path / 'wizard-os')


def test_skills_mount_is_additive_and_idempotent(tmp_path):
    c = _cfg(tmp_path)
    ov = c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml'
    ov.parent.mkdir(parents=True)
    builder = {'type': 'bind', 'source': '/b', 'target': '/opt/wizard-builder', 'read_only': True}
    ov.write_text(yaml.safe_dump({'services': {'hermes-agent-engine': {'volumes': [builder],
                                                                        'environment': {'WIZARD_BUILDER_ROOT': '/opt/wizard-builder'}}}}))
    assert agent_link.add_skills_mount(c) is True
    doc = yaml.safe_load(ov.read_text())['services']['hermes-agent-engine']
    assert doc['volumes'][0] == builder and doc['environment'] == {'WIZARD_BUILDER_ROOT': '/opt/wizard-builder'}
    assert doc['volumes'][1] == {'type': 'bind', 'read_only': True, 'target': '/opt/wizard-os/skills',
                                 'source': str(c.runtipi_root / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/skills')}
    before = ov.read_bytes()
    assert agent_link.add_skills_mount(c) is False and ov.read_bytes() == before


def test_skills_mount_keeps_compose_merge_tags(tmp_path):
    """A fresh install's override already has `networks: !override` (wam network). 0.3.82 on a bare VPS printed
    "agent not linked to the app gateway (could not determine a constructor for the tag '!override')" and the
    agent never got the `wizard` gateway (demo VPS, 29 Oct 2026)."""
    c = _cfg(tmp_path)
    ov = c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml'
    ov.parent.mkdir(parents=True)
    (c.runtipi_root / 'app-data/wizardapps/wizard-agent-service/data/wizard-os/skills').mkdir(parents=True, exist_ok=True)
    ov.write_text('services:\n  hermes-agent-engine:\n    networks: !override\n      wizard-app-hermes-agent:\n        gw_priority: 1\n'
                  'networks:\n  wizard-app-hermes-agent:\n    name: wizard-app-hermes-agent\n    external: true\n')
    assert agent_link.add_skills_mount(c) is True
    text = ov.read_text()
    assert 'networks: !override' in text
    from wam.network import load_yaml
    vols = load_yaml(text)['services']['hermes-agent-engine']['volumes']
    assert any(isinstance(v, dict) and v.get('target') == '/opt/wizard-os/skills' for v in vols)


def test_skills_mount_collision_is_refused(tmp_path):
    c = _cfg(tmp_path)
    ov = c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml'
    ov.parent.mkdir(parents=True)
    raw = yaml.safe_dump({'services': {'hermes-agent-engine': {'volumes': ['/elsewhere:/opt/wizard-os/skills:ro']}}})
    ov.write_text(raw)
    with pytest.raises(ValueError, match='collision'):
        agent_link.add_skills_mount(c)
    assert ov.read_text() == raw


def test_alongside_box_is_refused(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setattr(alongside, 'load', lambda home: {'agent': {}})
    with pytest.raises(ValueError, match='wizard link'):
        agent_link.link(_cfg(tmp_path))


def test_link_reads_service_token_edits_restarts_and_probes(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setattr(alongside, 'load', lambda home: None)
    c = _cfg(tmp_path)
    calls, lifecycle = [], []
    edits = iter([{'config_changed': True, 'env_changed': True, 'skills_visible': []},
                  {'config_changed': False, 'env_changed': False, 'gateway_status': 200,
                   'skills_visible': ['wizard-job-video-editing']}])

    def fake_run(argv, **kw):
        calls.append((argv, kw.get('input')))
        if argv[-2:] == ['ps', '-q']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if 'ps' in argv:
            return SimpleNamespace(returncode=0, stdout='a1 wizard-agent-service\nb2 hermes-agent-engine\nc3 defleur-video\n', stderr='')
        if 'sh' in argv:
            return SimpleNamespace(returncode=0, stdout='svc-token', stderr='')
        if agent_link.PLUGIN in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'plugin': 'current', 'changed': False}) + '\n', stderr='')
        return SimpleNamespace(returncode=0, stdout=json.dumps(next(edits)) + '\n', stderr='')

    class RT:
        def req(self, method, path, body, timeout):
            lifecycle.append(path)
        def wait_status(self, urn, want, timeout):
            lifecycle.append(sorted(want)[0])
    c.runtipi = lambda: RT()
    monkeypatch.setattr(agent_link.subprocess, 'run', fake_run)
    out = agent_link.link(c)
    assert out['linked'] and out['restarted'] and out['gateway_status'] == 200
    assert out['gateway'] == 'http://wizard-agent-service:8787/wizard/mcp'
    assert all(a[a.index(agent_link.HERMES_PY) - 1] == 'b2' for a, _ in calls if agent_link.HERMES_PY in a)
    assert lifecycle == ['/app-lifecycle/hermes-agent:wizardapps/stop', 'stopped',
                         '/app-lifecycle/hermes-agent:wizardapps/start', 'running']
    edits_run = [(a, i) for a, i in calls if agent_link.EDIT in a]
    assert len(edits_run) == 2 and all(i == 'svc-token\n' for _, i in edits_run)
    assert all('-u' in a and '1000:1000' in a for a, _ in edits_run)
    assert 'svc-token' not in json.dumps(out), 'the receipt never carries the token'
    assert all('svc-token' not in ' '.join(a) for a, _ in calls), 'the token never appears in argv'


def run_plugin_edit(home: Path, changes='chatgpt'):
    env = {**os.environ, 'HERMES_HOME': str(home), 'WIZARD_LINK_PLUGIN': 'wizard-os', 'WIZARD_LINK_ACCOUNT_CHANGES': changes}
    argv = [sys.executable, '-c', agent_link.EDIT, agent_link.GATEWAY_URL, 'wizard', str(home / 'skills-view')]
    return subprocess.run(argv, input='tok\n', capture_output=True, text=True, env=env)


def test_edit_enables_installed_plugin_and_chatgpt_account_changes(tmp_path):
    """Fresh 0.3.86: the ChatGPT app said "Your agent can't add sign-ins from this app yet" (no_broker): the agent app
    had no wizard-os plugin. Once the plugin is in the agent's plugins folder, the link enables it and turns its
    account broker on for ChatGPT only (Claude keeps its Claude Code page)."""
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['other']}}))
    (tmp_path / 'plugins/wizard-os').mkdir(parents=True)
    (tmp_path / 'plugins/wizard-os/plugin.yaml').write_text('name: wizard-os\n')
    p = run_plugin_edit(tmp_path)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out['config_changed'] and out['env_changed']
    assert yaml.safe_load((tmp_path / 'config.yaml').read_text())['plugins']['enabled'] == ['other', 'wizard-os']
    assert (tmp_path / '.env').read_text() == 'WIZARD_MCP_TOKEN=tok\nWIZARD_ACCOUNT_CHANGES=chatgpt\n'
    again = json.loads(run_plugin_edit(tmp_path).stdout)
    assert not again['config_changed'] and not again['env_changed']


def test_edit_keeps_owner_plugin_choices(tmp_path):
    """An owner who disabled the plugin or set WIZARD_ACCOUNT_CHANGES himself keeps his choice; no plugin, no edit."""
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'disabled': ['wizard-os']}}))
    (tmp_path / '.env').write_text('WIZARD_ACCOUNT_CHANGES=0\n')
    (tmp_path / 'plugins/wizard-os').mkdir(parents=True)
    (tmp_path / 'plugins/wizard-os/plugin.yaml').write_text('name: wizard-os\n')
    run_plugin_edit(tmp_path)
    assert 'enabled' not in yaml.safe_load((tmp_path / 'config.yaml').read_text())['plugins']
    assert (tmp_path / '.env').read_text() == 'WIZARD_ACCOUNT_CHANGES=0\nWIZARD_MCP_TOKEN=tok\n'
    bare = tmp_path / 'bare'
    bare.mkdir()
    (bare / 'config.yaml').write_text('{}\n')
    run_plugin_edit(bare)
    assert 'plugins' not in yaml.safe_load((bare / 'config.yaml').read_text())
    assert 'WIZARD_ACCOUNT_CHANGES' not in (bare / '.env').read_text()


FAKE_HERMES = """#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
assert args[:2] == ['plugins', 'install'] and '--no-enable' in args and '--no-deps' in args
root = os.path.join(os.environ['HERMES_HOME'], 'plugins', 'wizard-os')
if os.path.isdir(root):
    assert '--force' in args, 'an existing plugin is replaced only with --force'
    shutil.rmtree(root)
os.makedirs(root)
ref = args[args.index('--ref') + 1]
open(os.path.join(root, 'plugin.yaml'), 'w').write('name: wizard-os\\nref: %s\\n' % ref)
meta = os.path.join(os.environ['HERMES_HOME'], 'plugins', '.install-metadata.json')
open(meta, 'w').write(json.dumps({'wizard-os': {'ref': ref}}))
with open(os.path.join(os.environ['HERMES_HOME'], 'calls.log'), 'a') as f:
    f.write(json.dumps(args) + '\\n')
if os.environ.get('FAKE_FAIL'):
    sys.exit(1)   # a partial copy and metadata left behind, as an interrupted Hermes install does
"""


def run_plugin(home: Path, ref: str, fail=False):
    fake = home / 'fake-hermes'
    if not fake.exists():
        fake.write_text(FAKE_HERMES)
        fake.chmod(0o755)
    argv = [sys.executable, '-c', agent_link.PLUGIN, str(fake), agent_link.PLUGIN_SOURCE, ref, 'wizard-os']
    env = {**os.environ, 'HERMES_HOME': str(home), **({'FAKE_FAIL': '1'} if fail else {})}
    p = subprocess.run(argv, capture_output=True, text=True, env=env)
    if fail:
        assert p.returncode != 0 and 'nothing changed' in p.stderr, p.stderr
        return None
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_plugin_is_installed_pinned_then_refreshed_only_while_untouched(tmp_path):
    (tmp_path / 'plugins').mkdir()
    a, b, c = 'a' * 40, 'b' * 40, 'c' * 40
    assert run_plugin(tmp_path, a) == {'plugin': 'installed', 'changed': True, 'managed': True}
    calls = [json.loads(l) for l in (tmp_path / 'calls.log').read_text().splitlines()]
    assert calls == [['plugins', 'install', agent_link.PLUGIN_SOURCE, '--ref', a, '--no-enable', '--no-deps']]
    assert run_plugin(tmp_path, a) == {'plugin': 'current', 'changed': False, 'managed': True}
    assert run_plugin(tmp_path, b) == {'plugin': 'refreshed', 'changed': True, 'managed': True}
    assert 'ref: ' + b in (tmp_path / 'plugins/wizard-os/plugin.yaml').read_text()
    (tmp_path / 'plugins/wizard-os/plugin.yaml').write_text('owner edit\n')
    assert run_plugin(tmp_path, c) == {'plugin': 'owner-modified', 'changed': False, 'managed': False}
    assert (tmp_path / 'plugins/wizard-os/plugin.yaml').read_text() == 'owner edit\n'


def test_plugin_the_link_did_not_install_is_left_alone(tmp_path):
    (tmp_path / 'plugins/wizard-os').mkdir(parents=True)
    (tmp_path / 'plugins/wizard-os/plugin.yaml').write_text('mine\n')
    assert run_plugin(tmp_path, 'a' * 40) == {'plugin': 'owner-managed', 'changed': False, 'managed': False}
    assert not (tmp_path / 'calls.log').exists()


def test_plugin_ref_is_one_immutable_commit():
    import re
    assert re.fullmatch(r'[0-9a-f]{40}', agent_link.PLUGIN_REF)
    assert agent_link.ACCOUNT_CHANGES == 'chatgpt'


def test_link_restarts_when_only_the_plugin_arrived_and_survives_a_plugin_failure(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setattr(alongside, 'load', lambda home: None)
    for plugin, restarted in (({'plugin': 'installed', 'changed': True, 'managed': True}, True), (None, False)):
        c = _cfg(tmp_path / str(restarted))
        lifecycle = []
        quiet = {'config_changed': False, 'env_changed': False, 'gateway_status': 200, 'skills_visible': []}

        def fake_run(argv, **kw):
            if argv[-2:] == ['ps', '-q']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            if 'ps' in argv:
                return SimpleNamespace(returncode=0, stdout='a1 wizard-agent-service\nb2 hermes-agent-engine\n', stderr='')
            if 'sh' in argv:
                return SimpleNamespace(returncode=0, stdout='svc-token', stderr='')
            if agent_link.PLUGIN in argv:
                assert '-u' in argv and '1000:1000' in argv and agent_link.PLUGIN_REF in argv
                if plugin is None:
                    return SimpleNamespace(returncode=1, stdout='', stderr='wizard-os plugin not installed: offline')
                return SimpleNamespace(returncode=0, stdout=json.dumps(plugin) + '\n', stderr='')
            # enabled only when the link installed it; after a plugin failure nothing is enabled
            assert ('WIZARD_LINK_PLUGIN=wizard-os' in argv and 'WIZARD_LINK_ACCOUNT_CHANGES=chatgpt' in argv) is bool(plugin)
            return SimpleNamespace(returncode=0, stdout=json.dumps(quiet) + '\n', stderr='')

        class RT:
            def req(self, method, path, body, timeout):
                lifecycle.append(path)
            def wait_status(self, urn, want, timeout):
                pass
        (c.runtipi_root / 'user-config/wizardapps/hermes-agent').mkdir(parents=True)
        (c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml').write_text(
            yaml.safe_dump({'services': {'hermes-agent-engine': {'volumes': [agent_link.skills_mount(c)]}}}))
        c.runtipi = lambda: RT()
        monkeypatch.setattr(agent_link.subprocess, 'run', fake_run)
        out = agent_link.link(c)
        assert out['restarted'] is restarted and out['linked']
        if plugin is None:
            assert out['plugin'] == 'error' and 'offline' in out['plugin_error'] and lifecycle == []


def test_installer_rerun_relinks_an_existing_fresh_agent(tmp_path, monkeypatch):
    """A fresh box installed before the plugin existed gets it on its next installer run (bootstrap), not only when the
    agent app is installed; alongside boxes and boxes without both apps are left to `wizard link`."""
    from wam import alongside, cli
    seen = []
    monkeypatch.setattr(agent_link, 'link', lambda c, store='wizardapps', restart=True: seen.append(store) or {'linked': True})
    c = SimpleNamespace(home=tmp_path)
    monkeypatch.setattr(alongside, 'load', lambda home: None)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {'hermes-agent', 'wizard-agent-service', 'files'})
    assert cli.relink_fresh_agent(c, object()) == {'linked': True} and seen == ['wizardapps']
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {'hermes-agent'})
    assert cli.relink_fresh_agent(c, object()) is None
    monkeypatch.setattr(alongside, 'load', lambda home: {'agent': {}})
    assert cli.relink_fresh_agent(c, object()) is None and seen == ['wizardapps']
    monkeypatch.setattr(alongside, 'load', lambda home: None)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {'hermes-agent', 'wizard-agent-service'})
    monkeypatch.setattr(agent_link, 'link', lambda *a, **k: (_ for _ in ()).throw(ValueError('expected one running')))
    assert cli.relink_fresh_agent(c, object()) is None


def test_failed_install_leaves_nothing_and_is_retried(tmp_path):
    """A Hermes install that fails after writing part of the plugin must not leave a copy that later reads as the
    owner's (never retried, maybe enabled half-written)."""
    (tmp_path / 'plugins').mkdir()
    (tmp_path / 'plugins/.install-metadata.json').write_text('{"other": {}}')
    run_plugin(tmp_path, 'a' * 40, fail=True)
    assert not (tmp_path / 'plugins/wizard-os').exists() and not (tmp_path / 'plugins/.wizard-fresh-link.json').exists()
    assert (tmp_path / 'plugins/.install-metadata.json').read_text() == '{"other": {}}'
    assert run_plugin(tmp_path, 'a' * 40)['plugin'] == 'installed'


def test_failed_refresh_puts_the_working_plugin_back(tmp_path):
    (tmp_path / 'plugins').mkdir()
    a, b = 'a' * 40, 'b' * 40
    run_plugin(tmp_path, a)
    before = (tmp_path / 'plugins/wizard-os/plugin.yaml').read_text()
    marker = (tmp_path / 'plugins/.wizard-fresh-link.json').read_text()
    meta = (tmp_path / 'plugins/.install-metadata.json').read_text()
    run_plugin(tmp_path, b, fail=True)
    assert (tmp_path / 'plugins/wizard-os/plugin.yaml').read_text() == before
    assert json.loads((tmp_path / 'plugins/.wizard-fresh-link.json').read_text()) == json.loads(marker)
    assert (tmp_path / 'plugins/.install-metadata.json').read_text() == meta
    assert not any(p.name.startswith('.wizard-fresh-link.backup') for p in (tmp_path / 'plugins').iterdir())
    assert run_plugin(tmp_path, b)['plugin'] == 'refreshed', 'still ours: the next run refreshes it'


def test_interrupted_install_is_undone_on_the_next_run(tmp_path):
    """The script was killed mid-install (journal says installing): the partial copy is the link's, not the owner's."""
    (tmp_path / 'plugins/wizard-os').mkdir(parents=True)
    (tmp_path / 'plugins/wizard-os/plugin.yaml').write_text('half\n')
    (tmp_path / 'plugins/.wizard-fresh-link.json').write_text(json.dumps(
        {'state': 'installing', 'ref': 'a' * 40, 'previous': None, 'meta_existed': False}))
    assert run_plugin(tmp_path, 'a' * 40) == {'plugin': 'installed', 'changed': True, 'managed': True}
    assert 'ref: ' + 'a' * 40 in (tmp_path / 'plugins/wizard-os/plugin.yaml').read_text()


def test_owner_plugin_is_never_enabled_by_the_link(tmp_path, monkeypatch):
    from wam import alongside
    monkeypatch.setattr(alongside, 'load', lambda home: None)
    c = _cfg(tmp_path)
    edits = []

    def fake_run(argv, **kw):
        if argv[-2:] == ['ps', '-q']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if 'ps' in argv:
            return SimpleNamespace(returncode=0, stdout='a1 wizard-agent-service\nb2 hermes-agent-engine\n', stderr='')
        if 'sh' in argv:
            return SimpleNamespace(returncode=0, stdout='svc-token', stderr='')
        if agent_link.PLUGIN in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'plugin': 'owner-managed', 'changed': False,
                                                                   'managed': False}) + '\n', stderr='')
        edits.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'config_changed': False, 'env_changed': False,
                                                               'gateway_status': 200, 'skills_visible': []}) + '\n', stderr='')
    (c.runtipi_root / 'user-config/wizardapps/hermes-agent').mkdir(parents=True)
    (c.runtipi_root / 'user-config/wizardapps/hermes-agent/docker-compose.yml').write_text(
        yaml.safe_dump({'services': {'hermes-agent-engine': {'volumes': [agent_link.skills_mount(c)]}}}))
    monkeypatch.setattr(agent_link.subprocess, 'run', fake_run)
    out = agent_link.link(c)
    assert out['plugin'] == 'owner-managed' and out['linked'] and edits
    assert not any('WIZARD_LINK_PLUGIN=wizard-os' in a or 'WIZARD_LINK_ACCOUNT_CHANGES=chatgpt' in a for a in edits)
