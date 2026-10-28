"""Long-lived installs keep their access, without trusting executable recipes."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from wam import alongside, host_policy as hp, managed_overrides


def compose(**fields):
    return yaml.safe_dump({'services': {'audit': {'image': 'test:1', **fields}}})


def box(tmp_path):
    return SimpleNamespace(home=tmp_path / 'manager', runtipi_root=tmp_path / 'runtipi')


def write(c, rel, text):
    path = c.runtipi_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize('cap', sorted(hp.DEFAULT_CAPS))
@pytest.mark.parametrize('drop', [[], ['ALL']])
def test_docker_default_capabilities_never_require_owner(cap, drop):
    assert hp.risks(compose(cap_add=[cap, 'CAP_' + cap.lower()], cap_drop=drop), 'audit') == []


@pytest.mark.parametrize('cap', ['SYS_ADMIN', 'NET_ADMIN', 'SYS_PTRACE', 'ALL', 'CAP_SYS_MODULE'])
def test_nondefault_capabilities_still_require_owner(cap):
    assert hp.risks(compose(cap_add=['CHOWN', cap], cap_drop=['ALL']), 'audit')


@pytest.mark.parametrize('shape', ['release', 'source'])
@pytest.mark.parametrize('long_form', [False, True])
def test_only_same_app_read_only_resolved_manager_source_is_allowed(tmp_path, shape, long_form):
    root = tmp_path / 'runtipi'
    prefix = (root / 'repos/wizardapps/apps/audit/releases/v1-owner-abc' if shape == 'release'
              else root / 'app-data/.wizard-source/wizardapps/audit@0123456789ab')
    prefix.mkdir(parents=True)
    def check(source, readonly=True):
        mount = ({'type': 'bind', 'source': str(source), 'target': '/app', 'read_only': readonly} if long_form
                 else f'{source}:/app:' + ('ro' if readonly else 'rw'))
        return hp.risks(compose(volumes=[mount]), 'audit', root=root)
    assert check(prefix) == []
    assert check(prefix / 'src') == []
    assert check(prefix, False)
    assert check(str(prefix).replace('/audit', '/other'))
    assert check(prefix / '../' / prefix.name)
    (prefix / 'escape').symlink_to(tmp_path)
    assert check(prefix / 'escape')
    prefix.rename(prefix.with_name('moved'))
    prefix.symlink_to(prefix.with_name('moved'), target_is_directory=True)
    assert check(prefix)


def test_risk_approval_is_subset_not_recipe_hash_and_store_scoped(tmp_path):
    home = tmp_path / 'home'
    text = compose(privileged=True, cap_add=['SYS_ADMIN'])
    with pytest.raises(hp.NeedsOwnerApproval) as exc:
        hp.require(home, 'audit', [text])
    hp.approve(home, 'audit', exc.value.payload['approve_url'].split('/')[-1])
    hp.require(home, 'audit', [text.replace('test:1', 'test:2') + '# new recipe\n'])
    hp.require(home, 'audit', [compose(privileged=True)])
    with pytest.raises(hp.NeedsOwnerApproval):
        hp.require(home, 'audit', [compose(privileged=True, pid='host')])
    with pytest.raises(hp.NeedsOwnerApproval):
        hp.require(home, 'audit', [text], store='other')
    assert hp._read(home)['approvals']['audit']['risks'] == sorted(hp.risks(text, 'audit'))


@pytest.mark.parametrize('kind', ['mount-path', 'mount-write', 'env-file', 'device', 'network'])
def test_new_resolved_host_access_cannot_reuse_old_risks(tmp_path, kind):
    fields = {
        'mount-path': ({'volumes': ['${HOST}:/data:ro']}, {'HOST': '/etc/one'}, {'HOST': '/etc/two'}),
        'mount-write': ({'volumes': ['/etc:/data:${MODE}']}, {'MODE': 'ro'}, {'MODE': 'rw'}),
        'env-file': ({'env_file': '${HOST}'}, {'HOST': '/etc/one'}, {'HOST': '/etc/two'}),
        'device': ({'devices': ['${HOST}:/dev/test']}, {'HOST': '/dev/one'}, {'HOST': '/dev/two'}),
        'network': ({'network_mode': '${MODE}'}, {'MODE': 'host'}, {'MODE': 'container:other'}),
    }
    field, old, new = fields[kind]
    text = compose(**field)
    with pytest.raises(hp.NeedsOwnerApproval) as exc:
        hp.require(tmp_path, 'audit', [text], variables=old)
    sha = exc.value.payload['approve_url'].split('/')[-1]
    hp.approve(tmp_path, 'audit', sha)
    with pytest.raises(hp.NeedsOwnerApproval) as changed:
        hp.require(tmp_path, 'audit', [text], variables=new)
    assert changed.value.payload['approve_url'].split('/')[-1] != sha
    with pytest.raises(ValueError):
        hp.approve(tmp_path, 'audit', sha)


def test_upgrade_snapshot_uses_installed_not_store_and_runs_once(tmp_path):
    c = box(tmp_path)
    old = compose(volumes=['/etc:/server:ro'])
    write(c, 'apps/wizardapps/audit/docker-compose.yml', old)
    write(c, 'repos/wizardapps/apps/audit/docker-compose.yml', old + '# new image\n')
    hp.migrate(c)
    state = hp._read(c.home)
    row = state['approvals']['audit']
    assert row['source'] == 'grandfathered at upgrade' and row['timestamp']
    assert row['risks'] == hp.risks(old, 'audit', **hp.context(c, 'audit'))
    hp.require_app(c, 'audit')
    write(c, 'repos/wizardapps/apps/audit/docker-compose.yml', compose(volumes=['/:/server']))
    hp.migrate(c)
    assert hp._read(c.home) == state
    with pytest.raises(hp.NeedsOwnerApproval):
        hp.require_app(c, 'audit')
    assert 'outside its own storage' in hp.grandfathered_health(c.home)[0][2]
    # No late install or imported package can enroll itself after the snapshot.
    write(c, 'apps/wizardapps/late/docker-compose.yml', compose(privileged=True))
    hp.migrate(c)
    with pytest.raises(hp.NeedsOwnerApproval):
        hp.require_app(c, 'late')


def test_safe_installed_app_never_preapproves_store_new_risk(tmp_path):
    c = box(tmp_path)
    write(c, 'apps/wizardapps/audit/docker-compose.yml', compose())
    write(c, 'repos/wizardapps/apps/audit/docker-compose.yml', compose(privileged=True))
    hp.migrate(c)
    assert hp._read(c.home)['approvals']['audit']['risks'] == []
    with pytest.raises(hp.NeedsOwnerApproval):
        hp.require_app(c, 'audit')


def test_legacy_sha_and_forged_pending_token_do_not_approve(tmp_path):
    hp._write(tmp_path, {'approvals': {'audit': hp.digest([compose(privileged=True)])}, 'pending': {}})
    with pytest.raises(ValueError):
        hp.approve(tmp_path, 'audit', '0' * 64)
    with pytest.raises(hp.NeedsOwnerApproval):
        hp.require(tmp_path, 'audit', [compose(privileged=True)])
    with pytest.raises(ValueError):
        hp.approve(tmp_path, 'audit', '0' * 64)


def test_registered_agent_home_is_finite_and_bound_to_identity(tmp_path):
    c = box(tmp_path)
    folder = c.home / 'agents/philosopher'
    folder.mkdir(parents=True)
    (folder / 'agent.json').write_text(json.dumps({'schema': 1, 'id': 'philosopher',
        'runtime': {'kind': 'container', 'app_id': 'agent-philosopher'}}))
    ctx = hp.context(c, 'agent-philosopher')
    def recipe(source, target='/opt/data', service='agent-philosopher'):
        return {'services': {service: {'image': 'test', 'volumes': [f'{source}:{target}']}}}
    assert not hp.risks(recipe(folder / 'home'), 'agent-philosopher', **ctx)
    legacy = c.runtipi_root / 'app-data/wizard-os/agents/philosopher/home'
    assert not hp.risks(recipe(legacy), 'agent-philosopher', **ctx)
    assert hp.risks(recipe(folder / 'home', '/host'), 'agent-philosopher', **ctx)
    assert hp.risks(recipe(folder / 'home', service='other'), 'agent-philosopher', **ctx)
    assert hp.risks(recipe(c.home / 'agents/private/home'), 'agent-philosopher', **ctx)
    (folder / 'agent.json').unlink()
    assert hp.risks(recipe(folder / 'home'), 'agent-philosopher', **hp.context(c, 'agent-philosopher'))


def test_regenerate_alongside_override_without_writes_or_docker(tmp_path, monkeypatch):
    c = box(tmp_path)
    c.home.mkdir()
    hh = tmp_path / 'hermes'
    st = {'mode': 'alongside', 'agent': {'hermes_home': str(hh), 'origin': 'http://127.0.0.1:8642', 'uid': 1000, 'gid': 1000},
          'shell': {'port': 8789, 'share_history': False}, 'service': {'port': 8790}}
    alongside.save(c.home, st)
    text = alongside.shell_override(st, 'wizard-agent-service', c.home, 'service',
                                   alongside.host_media_root(c.runtipi_root), rootless=False, prepare=False)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('audit called a process'))
    monkeypatch.setattr(Path, 'mkdir', lambda *a, **k: pytest.fail('audit wrote a directory'))
    monkeypatch.setattr(Path, 'touch', lambda *a, **k: pytest.fail('audit wrote a file'))
    assert hp.normalized_override(c, 'wizard-agent-service', 'wizardapps', text, offline=True) is None
    changed = text + '\n# extra dangerous service\n'
    from wam.network import load_yaml, dump_yaml
    doc = load_yaml(changed)
    doc['services']['injected'] = {'image': 'evil', 'privileged': True}
    checked = hp.normalized_override(c, 'wizard-agent-service', 'wizardapps', dump_yaml(doc), offline=True)
    assert hp.risks(checked, 'wizard-agent-service', **hp.context(c, 'wizard-agent-service'))
    # A plausible comment or restoring old box wiring is not provenance.
    assert hp.risks(hp.normalized_override(c, 'audit', 'wizardapps', compose(network_mode='host'), offline=True), 'audit')


def test_builder_mount_needs_current_bundle_and_exact_fragment(tmp_path):
    from wam import builder_bundle
    c = box(tmp_path)
    manifest = c.home / 'builder/current/bundle.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'owner': builder_bundle.MARKER, 'schema': builder_bundle.SCHEMA}))
    mount = {'type': 'bind', 'source': str(c.home / 'builder'), 'target': builder_bundle.RUNTIME, 'read_only': True}
    doc = {'services': {'hermes-agent-engine': {'volumes': [mount],
           'environment': {'WIZARD_BUILDER_ROOT': builder_bundle.RUNTIME}}}}
    def audit(value):
        text = yaml.safe_dump(value)
        normalized = hp.normalized_override(c, 'hermes-agent', 'wizardapps', text, offline=True)
        return hp.risks(normalized, 'hermes-agent', **hp.context(c, 'hermes-agent'))
    assert not audit(doc)
    mount['read_only'] = False
    assert audit(doc)
    mount['read_only'] = True
    manifest.unlink()
    assert audit(doc)


def test_claude_share_is_derived_not_trusted_from_override_marker(tmp_path, monkeypatch):
    from wam import claude_login
    c = box(tmp_path)
    c.home.mkdir()
    hh = tmp_path / 'hermes'
    hh.mkdir()
    (hh / 'config.yaml').write_text('model: {provider: claude-subscription}\n')
    folder = tmp_path / 'claude'
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR', str(folder))
    st = {'mode': 'alongside', 'agent': {'hermes_home': str(hh), 'uid': 1000, 'gid': 1000}}
    alongside.save(c.home, st)
    override = tmp_path / 'override.yml'
    claude_login.apply(override, ['wizard-claude-engine'], folder, '1000:1000')
    def audit():
        doc = hp.normalized_override(c, 'wizard-claude', 'wizardapps', override.read_text(), offline=True)
        return hp.risks(doc, 'wizard-claude', **hp.context(c, 'wizard-claude'))
    assert not audit()
    # Even a self-consistent forged marker and mount cannot choose a new folder.
    claude_login.apply(override, ['wizard-claude-engine'], tmp_path / 'foreign', '1000:1000')
    assert audit()
    # Restored old-box share does not equal the current host's generated share.
    override.write_text(override.read_text().replace(str(tmp_path / 'foreign'), '/old-box/claude'))
    assert audit()


def test_harness_is_read_only_including_python_cache_and_dry_run(tmp_path):
    c = box(tmp_path)
    write(c, 'apps/wizardapps/audit/docker-compose.yml', compose(privileged=True))
    c.home.mkdir()
    def snapshot():
        return {str(p.relative_to(tmp_path)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in tmp_path.rglob('*') if p.is_file()}
    before = snapshot()
    script = Path(__file__).resolve().parents[1] / 'qa/privsep-slice2/installed-audit.py'
    for dry in (False, True):
        argv = [sys.executable, str(script), '--root', str(c.runtipi_root), '--home', str(c.home), '--json']
        proc = subprocess.run(argv + (['--grandfather-dry-run'] if dry else []), capture_output=True, text=True)
        report = json.loads(proc.stdout)
        assert report['apps'] == 1 and report['flagged'] == 1
        assert report['blocked'] == (0 if dry else 1)
        assert proc.returncode == (0 if dry else 1)
        assert str(tmp_path) not in proc.stdout
        assert snapshot() == before
