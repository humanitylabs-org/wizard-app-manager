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
    assert all(a[a.index('exec') + 4] == 'b2' for a, _ in calls if agent_link.HERMES_PY in a)
    assert lifecycle == ['/app-lifecycle/hermes-agent:wizardapps/stop', 'stopped',
                         '/app-lifecycle/hermes-agent:wizardapps/start', 'running']
    edits_run = [(a, i) for a, i in calls if agent_link.HERMES_PY in a]
    assert len(edits_run) == 2 and all(i == 'svc-token\n' for _, i in edits_run)
    assert all('-u' in a and '1000:1000' in a for a, _ in edits_run)
    assert 'svc-token' not in json.dumps(out), 'the receipt never carries the token'
    assert all('svc-token' not in ' '.join(a) for a, _ in calls), 'the token never appears in argv'
