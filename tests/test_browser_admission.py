"""Browser-trust admission facts are WAM state, not app claims (default-off)."""
import json
from types import SimpleNamespace

import pytest

from wam import browser_admission as ba
from wam import cli

NOTIF_CONTRACT = """browser_contract:
  version: 1
  isolation: opaque
  operations: [notifications.list, notifications.mark_read, state.local]
  state: {schema_version: 1, persistent_bytes: 65536}
"""


def install(root, app_id='notifications', repo='oscarhenrycollins/notifications', serial=7, created=1761955200,
            commit='c' * 40, contract=NOTIF_CONTRACT, provides='notifications', source=None, port=8810):
    d = root / 'apps' / 'wizardapps' / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / 'wizard.md').write_text(f'---\nid: {app_id}\nname: {app_id}\ntype: service\nversion: 1.0.0\n'
                                 f'provides: [{provides}]\nview: {{path: /{app_id}/, port: 8080}}\n{contract}---\n')
    (d / 'config.json').write_text(json.dumps({'port': port}))
    src = {'repo': repo, **({'source': source} if source else {})}
    (d / '.wizard-source').write_text(json.dumps(src))
    (d / '.wizard-commit').write_text(commit + '\n')
    return {'app': {'id': serial, 'createdAt': created, 'status': 'running', 'port': port},
            'info': {'urn': f'{app_id}:wizardapps'}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '1')
    root = tmp_path / 'runtipi'
    rows = []
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=root, host_ip=lambda: '127.0.0.1',
                        runtipi=lambda: SimpleNamespace(installed=lambda: list(rows), req=lambda *a, **k: []))
    monkeypatch.setattr(cli, 'Config', lambda: c)
    return SimpleNamespace(c=c, root=root, rows=rows,
                           write=lambda: cli.write_registry(c),
                           app=lambda reg, i='notifications': next(a for a in reg['apps'] if a['id'] == i)['browser'])


def test_default_off_registry_has_no_browser_projection(box, monkeypatch):
    monkeypatch.delenv('WIZARD_BROWSER_BOUNDARY')
    box.rows.append(install(box.root))
    assert 'browser' not in box.write()['apps'][0]


def test_declaration_alone_grants_nothing(box):
    box.rows.append(install(box.root))
    b = box.app(box.write())
    assert b['installation_id'].startswith('inst-') and b['source_id'] == 'standard:oscarhenrycollins/notifications'
    assert b['commit'] == 'c' * 40 and b['upstream'] == 'http://127.0.0.1:8810'
    assert b['contract']['operations'] == ['notifications.list', 'notifications.mark_read', 'state.local']
    assert b['granted'] == [] and b['admissible'] is False and b['reason'] == 'no_grant'


def test_cli_grant_binds_current_installation_and_moves_revision(box, capsys):
    box.rows.append(install(box.root))
    before = box.app(box.write())
    assert cli.main(['browser-grants', 'grant', 'notifications', '--operation', 'notifications.list',
                     '--operation', 'notifications.mark_read']) == 0
    after = box.app(json.loads((box.c.home / 'apps.json').read_text()))
    assert after['granted'] == ['notifications.list', 'notifications.mark_read'] and after['admissible'] is True
    assert after['permission_revision'] != before['permission_revision']
    shared = json.loads((box.root / 'app-data' / 'wizard-os' / 'apps.json').read_text())
    assert box.app(shared) == after   # the Service's read-only copy carries the same facts
    assert oct((box.c.home / 'browser-grants.json').stat().st_mode & 0o777) == '0o600'
    assert cli.main(['browser-grants', 'revoke', 'notifications', '--operation', 'notifications.mark_read']) == 0
    revoked = box.app(json.loads((box.c.home / 'apps.json').read_text()))
    assert revoked['granted'] == ['notifications.list'] and revoked['permission_revision'] not in (before['permission_revision'], after['permission_revision'])
    assert cli.main(['browser-grants', 'revoke', 'notifications']) == 0
    gone = box.app(json.loads((box.c.home / 'apps.json').read_text()))
    assert gone['granted'] == [] and gone['admissible'] is False
    capsys.readouterr()


def test_undeclared_operation_cannot_be_granted(box):
    box.rows.append(install(box.root, contract='browser_contract: {version: 1, isolation: opaque, operations: [notifications.list]}\n'))
    box.write()
    with pytest.raises(ValueError, match='declares'):
        ba.grant(box.c.home, 'notifications', ['notifications.mark_read'])
    assert not (box.c.home / 'browser-grants.json').exists()


@pytest.mark.parametrize('contract,reason', [
    ('', 'no_contract'),
    ('browser_contract: {version: 1, isolation: opaque, operations: ["*"]}\n', 'invalid_contract'),
    ('browser_contract: {version: 1, isolation: opaque, operations: [owner.fetch]}\n', 'invalid_contract'),
    ('browser_contract: {version: 1, isolation: same-origin, operations: [notifications.list]}\n', 'invalid_contract'),
    ('browser_contract: {version: 1, isolation: opaque, operations: [notifications.list], handler: x}\n', 'invalid_contract'),
])
def test_invalid_or_absent_contracts_are_never_admissible(box, contract, reason):
    box.rows.append(install(box.root, contract=contract))
    b = box.app(box.write())
    assert b['reason'] == reason and b['admissible'] is False and b['granted'] == []


def test_update_keeps_same_source_grant_but_changes_commit_and_new_ops_need_a_grant(box):
    box.rows.append(install(box.root, contract='browser_contract: {version: 1, isolation: opaque, operations: [notifications.list]}\n'))
    box.write()
    ba.grant(box.c.home, 'notifications', ['notifications.list'])
    first = box.app(box.write())
    box.rows[0] = install(box.root, commit='d' * 40)   # update adds mark_read + state.local to the declaration
    upd = box.app(box.write())
    assert upd['installation_id'] == first['installation_id'] and upd['commit'] == 'd' * 40
    assert upd['granted'] == ['notifications.list']            # increase is not silently granted
    assert upd['permission_revision'] != first['permission_revision']


def test_uninstall_reinstall_is_a_new_installation_without_old_grants(box):
    box.rows.append(install(box.root))
    box.write()
    ba.grant(box.c.home, 'notifications', ['notifications.list'])
    old = box.app(box.write())
    box.rows.clear()
    assert all(a['id'] != 'notifications' for a in box.write()['apps'])
    box.rows.append(install(box.root, serial=8, created=1761958800))
    new = box.app(box.write())
    assert new['installation_id'] != old['installation_id'] and new['granted'] == [] and not new['admissible']


def test_changed_source_with_same_app_id_does_not_inherit_grants(box):
    """Duplicate publishers of `profiles`: a grant for one source never admits the other."""
    contract = 'browser_contract: {version: 1, isolation: opaque, operations: [state.local]}\n'
    box.rows.append(install(box.root, 'profiles', 'oscarhenrycollins/profiles', contract=contract, provides='profiles'))
    box.write()
    ba.grant(box.c.home, 'profiles', ['state.local'])
    legacy = box.app(box.write(), 'profiles')
    assert legacy['admissible'] and legacy['source_id'] == 'standard:oscarhenrycollins/profiles'
    # Same Runtipi row, recipe switched to the other publisher (e.g. a store source change).
    box.rows[0] = install(box.root, 'profiles', 'humanitylabs-org/profiles', contract=contract, provides='profiles')
    switched = box.app(box.write(), 'profiles')
    assert switched['source_id'] == 'standard:humanitylabs-org/profiles'
    assert switched['granted'] == [] and not switched['admissible']
    # The old source's grant record is retained (no silent deletion) but inert.
    assert ba.read_grants(box.c.home)['grants'][0]['source_id'] == 'standard:oscarhenrycollins/profiles'


def test_duplicate_installed_ids_fail_the_registry_not_open_admission(box):
    box.rows.append(install(box.root, 'profiles', 'oscarhenrycollins/profiles', provides='profiles'))
    other = install(box.root, 'profiles', 'humanitylabs-org/profiles', provides='profiles')
    other['info']['urn'] = 'profiles:wizardapps'
    box.rows.append(other)
    with pytest.raises(ValueError, match='Duplicate installed Wizard app id'):
        box.write()


def test_unattributed_legacy_install_is_refused(box):
    row = install(box.root, repo=None)
    box.rows.append(row)
    b = box.app(box.write())
    assert b['source_id'] is None and b['reason'] == 'unattributed_installation' and not b['admissible']
    row['app'].pop('id')
    (box.root / 'apps' / 'wizardapps' / 'notifications' / '.wizard-source').write_text('{"repo": "oscarhenrycollins/notifications"}')
    b = box.app(box.write())
    assert b['installation_id'] is None and not b['admissible']
    with pytest.raises(ValueError, match='not eligible'):
        ba.grant(box.c.home, 'notifications', ['notifications.list'])


def test_unreadable_grants_never_widen_access(box):
    box.rows.append(install(box.root))
    box.write()
    ba.grant(box.c.home, 'notifications', ['notifications.list'])
    (box.c.home / 'browser-grants.json').write_text('{not json')
    b = box.app(box.write())
    assert b['granted'] == [] and b['admissible'] is False and b['reason'] == 'grants_unreadable'


def test_fixture_tool_uses_the_real_writer_and_never_reuses_installations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / 'tools' / 'browser_admission_fixture.py'
    run = lambda *a: subprocess.run([sys.executable, str(tool), str(tmp_path), *a], check=True, capture_output=True, text=True)
    read = lambda: json.loads((tmp_path / 'home' / 'apps.json').read_text())
    run('install', 'notifications')
    run('grant', 'notifications', 'notifications.list')
    first = read()['apps'][0]['browser']
    assert first['admissible'] and first['granted'] == ['notifications.list']
    assert json.loads((tmp_path / 'runtipi' / 'app-data' / 'wizard-os' / 'apps.json').read_text())['apps'][0]['browser'] == first
    run('uninstall', 'notifications')
    assert read()['apps'] == []
    run('install', 'notifications')
    again = read()['apps'][0]['browser']
    assert again['installation_id'] != first['installation_id'] and again['granted'] == [] and not again['admissible']
    bad = subprocess.run([sys.executable, str(tool), str(tmp_path), 'grant', 'notifications', 'owner.fetch'], capture_output=True, text=True)
    assert bad.returncode != 0 and 'declares' in bad.stderr


CARD2_CONTRACT = """browser_contract:
  version: 1
  isolation: opaque
  operations: [notifications.inbox, notifications.mark]
"""


def test_card2_notifications_operations_are_reviewed_and_need_their_own_grant(box, capsys):
    box.rows.append(install(box.root, contract=CARD2_CONTRACT))
    b = box.app(box.write())
    assert b['contract']['operations'] == ['notifications.inbox', 'notifications.mark']
    assert b['admissible'] is False and b['reason'] == 'no_grant'
    assert cli.main(['browser-grants', 'grant', 'notifications', '--operation', 'notifications.inbox']) == 0
    b = box.app(box.write())
    assert b['granted'] == ['notifications.inbox'] and b['admissible'] is True
    # A card-1 grant id the installed contract does not declare is refused, not silently added.
    with pytest.raises(ValueError):
        ba.grant(box.c.home, 'notifications', ['notifications.mark_read'])
    assert cli.main(['browser-grants', 'grant', 'notifications', '--operation', 'notifications.mark']) == 0
    assert box.app(box.write())['granted'] == ['notifications.inbox', 'notifications.mark']


def test_card2_ids_are_exact_no_wildcards_or_lookalikes(box):
    for op in ('notifications.*', 'notifications.mark_all', 'Notifications.mark', 'notifications.inbox2', 'notifications.markread'):
        box.rows[:] = [install(box.root, contract=CARD2_CONTRACT.replace('notifications.mark]', op + ']'))]
        b = box.app(box.write())
        assert b['reason'] == 'invalid_contract' and b['admissible'] is False, op


def test_fixture_tool_installs_the_apps_own_manifest(tmp_path):
    import subprocess, sys
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / 'tools' / 'browser_admission_fixture.py'
    md = tmp_path / 'wizard.md'
    md.write_text('---\nwizard: 0.2\nid: notifications\nname: Notifications\ntype: service\nversion: 2.29.0-wizard16\n'
                  'provides:\n- notifications\nview:\n  path: /notifications/\n  port: 8080\n' + CARD2_CONTRACT + '---\n\n# Notifications\n')
    run = lambda *a: subprocess.run([sys.executable, str(tool), str(tmp_path / 'box'), *a], check=True, capture_output=True, text=True)
    run('install', 'notifications', '--wizard-md', str(md))
    run('grant', 'notifications', 'notifications.inbox', 'notifications.mark')
    reg = json.loads((tmp_path / 'box' / 'home' / 'apps.json').read_text())
    b = next(a for a in reg['apps'] if a['id'] == 'notifications')['browser']
    assert b['contract']['operations'] == ['notifications.inbox', 'notifications.mark']
    assert b['granted'] == ['notifications.inbox', 'notifications.mark'] and b['admissible'] is True


MEDIA_CONTRACT = CARD2_CONTRACT.replace('notifications.mark]', 'notifications.mark, notifications.media]')


def test_media_tickets_are_their_own_reviewed_grant(box):
    """Thumbnails (wizard-os ADAPTER-SKETCH §6): reading the inbox never implies reading its pictures."""
    box.rows.append(install(box.root, contract=MEDIA_CONTRACT))
    b = box.app(box.write())
    assert b['contract']['operations'] == ['notifications.inbox', 'notifications.mark', 'notifications.media']
    assert cli.main(['browser-grants', 'grant', 'notifications', '--operation', 'notifications.inbox']) == 0
    assert box.app(box.write())['granted'] == ['notifications.inbox']
    assert cli.main(['browser-grants', 'grant', 'notifications', '--operation', 'notifications.media']) == 0
    assert box.app(box.write())['granted'] == ['notifications.inbox', 'notifications.media']
    assert cli.main(['browser-grants', 'revoke', 'notifications', '--operation', 'notifications.media']) == 0
    after = box.app(box.write())
    assert after['granted'] == ['notifications.inbox'] and after['permission_revision'] != b['permission_revision']
    for op in ('media.open', 'notifications.media.*', 'notifications.file', 'files.read'):
        box.rows[:] = [install(box.root, contract=CARD2_CONTRACT.replace('notifications.mark]', op + ']'))]
        assert box.app(box.write())['reason'] == 'invalid_contract', op


FILES_CONTRACT = """browser_contract:
  version: 1
  isolation: opaque
  operations: [files.list, files.index, files.thumbnail, files.content, files.upload]
  state: {schema_version: 1, persistent_bytes: 65536}
"""


def test_wizard_files_operations_are_reviewed_and_each_its_own_grant(box):
    """First-party Files view (wizard-os #385/#386): listing never implies bytes, and bytes never imply uploads."""
    box.rows.append(install(box.root, app_id='wizard-files', repo='humanitylabs-org/wizard-files', contract=FILES_CONTRACT, provides='files'))
    b = box.app(box.write(), 'wizard-files')
    assert b['contract']['operations'] == sorted(['files.list', 'files.index', 'files.thumbnail', 'files.content', 'files.upload'])
    assert b['admissible'] is False and b['reason'] == 'no_grant'
    assert cli.main(['browser-grants', 'grant', 'wizard-files', '--operation', 'files.list']) == 0
    assert box.app(box.write(), 'wizard-files')['granted'] == ['files.list']
    assert cli.main(['browser-grants', 'grant', 'wizard-files', '--operation', 'files.upload']) == 0
    granted = box.app(box.write(), 'wizard-files')
    assert granted['granted'] == ['files.list', 'files.upload']
    assert cli.main(['browser-grants', 'revoke', 'wizard-files', '--operation', 'files.upload']) == 0
    after = box.app(box.write(), 'wizard-files')
    assert after['granted'] == ['files.list'] and after['permission_revision'] != granted['permission_revision']
    for op in ('files.*', 'files.read', 'files.upload.chunk', 'files.write', 'Files.list'):
        box.rows[:] = [install(box.root, app_id='wizard-files', repo='humanitylabs-org/wizard-files', contract=FILES_CONTRACT.replace('files.upload]', op + ']'), provides='files')]
        assert box.app(box.write(), 'wizard-files')['reason'] == 'invalid_contract', op
