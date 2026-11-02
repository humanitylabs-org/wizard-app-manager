"""wizard-os 389 rehearsal release blockers O4 (gate timing) and O5 (release-default grants).

O4: the switch restarted Files, THEN `wam links` wrote its own-tab env, and nothing restarted it again: Files served
    cross-site frames ungated until a manual restart. Every boundary env change now restarts the running app before
    WAM returns, and `wam app start|restart` writes the env BEFORE Runtipi recreates the container.
O5: a fresh box had no browser grants, so the default Notifications said "needs permission". Decided (Miguel): an
    app the running release pins as a default gets its own manifest-declared operations at install/upgrade, recorded
    with release + commit + permission revision; an increase on update waits for the owner; everything else starts
    with no grants.
"""
import json
from types import SimpleNamespace

import pytest

from wam import browser_admission as ba
from wam import cli
from test_browser_admission import NOTIF_CONTRACT, install

OWN_TAB = 'browser_contract: {version: 1, isolation: own-tab}\n'
OWN_TAB_COMPOSE = ('services:\n  files:\n    environment:\n      WIZARD_BROWSER_BOUNDARY: ${WIZARD_BROWSER_BOUNDARY:-0}\n'
                   '      WIZARD_OWN_HOSTS: ${WIZARD_OWN_HOSTS:-}\n')
CRED_COMPOSE = ('services:\n  notifications:\n    environment:\n      WIZARD_BROWSER_BOUNDARY: ${WIZARD_BROWSER_BOUNDARY:-0}\n'
                '      WIZARD_APP_CREDENTIAL: ${WIZARD_APP_CREDENTIAL:-}\n')
PIN = 'c' * 40


@pytest.fixture(autouse=True)
def hermetic_runtipi(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNTIPI_ROOT', str(tmp_path / 'no-runtipi'))
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    getattr(ba, 'ENV_CHANGED', set()).clear()


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A box whose Runtipi reports each app's status and records start/restart calls (with the app env at that
    moment), so the ORDER of env write and restart is observable."""
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '1')
    root, rows, calls = tmp_path / 'runtipi', [], []
    env = lambda app: root / 'user-config' / 'wizardapps' / app / 'app.env'   # noqa: E731

    def status_of(urn):
        r = next((r for r in rows if r['info']['urn'] == urn), None)
        return {'app': r['app']} if r else {}

    def lifecycle(verb):
        def go(urn):
            p = env(urn.partition(':')[0])
            calls.append((verb, urn.partition(':')[0], p.read_text() if p.is_file() else ''))
        return go

    rt = SimpleNamespace(installed=lambda: list(rows), req=lambda *a, **k: [], app=status_of,
                         restart=lifecycle('restart'), start=lifecycle('start'),
                         wait_status=lambda urn, want, timeout: 'running')
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=root, host_ip=lambda: '127.0.0.1', runtipi=lambda: rt)
    monkeypatch.setattr(cli, 'Config', lambda: c)
    monkeypatch.setattr(cli.time, 'sleep', lambda s: None)
    return SimpleNamespace(c=c, root=root, rows=rows, calls=calls, env=env, write=lambda: cli.write_registry(c),
                           app=lambda reg, i: next(a for a in reg['apps'] if a['id'] == i).get('browser'))


def files_own_tab(b, status='running'):
    row = install(b.root, app_id='files', repo='oscarhenrycollins/files', contract=OWN_TAB, provides='files', port=8794)
    row['app']['status'] = status
    (b.root / 'apps' / 'wizardapps' / 'files' / 'docker-compose.yml').write_text(OWN_TAB_COMPOSE)
    b.c.home.mkdir(parents=True, exist_ok=True)
    (b.c.home / 'links.json').write_text(json.dumps({'links': [
        {'id': 'files', 'url': 'https://box.tail1234.ts.net:8794/files/', 'own_tab': True}]}))
    b.rows.append(row)
    return row


def env_of(path):
    return dict(l.split('=', 1) for l in path.read_text().splitlines() if '=' in l) if path.is_file() else {}


# ---------------------------------------------------------------- O4

def test_switch_on_gates_a_running_own_tab_app_before_returning(box):
    files_own_tab(box)
    box.write()                                   # the switch's own `wam apps`
    assert env_of(box.env('files')) == {'WIZARD_BROWSER_BOUNDARY': '1', 'WIZARD_OWN_HOSTS': 'box.tail1234.ts.net:8794'}
    assert [(v, a) for v, a, _ in box.calls] == [('restart', 'files')]
    assert 'WIZARD_OWN_HOSTS=box.tail1234.ts.net:8794' in box.calls[0][2]   # restarted WITH its gate env
    box.write()                                   # steady state: nothing changes, nothing restarts
    assert len(box.calls) == 1


def test_app_restart_writes_the_env_first_and_restarts_once(box):
    files_own_tab(box)
    cli.app_lifecycle(box.c, 'restart', 'files')  # e.g. the switch restarting every app before `wam links`
    assert [(v, a) for v, a, _ in box.calls] == [('restart', 'files')]
    assert 'WIZARD_OWN_HOSTS=box.tail1234.ts.net:8794' in box.calls[0][2]


def test_credential_app_is_restarted_when_its_gate_env_is_first_written(box):
    row = install(box.root)
    (box.root / 'apps' / 'wizardapps' / 'notifications' / 'docker-compose.yml').write_text(CRED_COMPOSE)
    box.rows.append(row)
    box.write()
    assert [(v, a) for v, a, _ in box.calls] == [('restart', 'notifications')]
    assert 'WIZARD_APP_CREDENTIAL=' in box.calls[0][2]


def test_stopped_app_gets_the_env_but_is_not_started(box):
    files_own_tab(box, status='stopped')
    box.write()
    assert env_of(box.env('files'))['WIZARD_OWN_HOSTS'] == 'box.tail1234.ts.net:8794' and box.calls == []


def test_switch_off_drops_the_gate_immediately(box, monkeypatch):
    files_own_tab(box)
    box.write()
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '0')
    box.write()
    assert env_of(box.env('files')) == {}
    assert [(v, a) for v, a, _ in box.calls] == [('restart', 'files'), ('restart', 'files')]
    assert 'WIZARD_OWN_HOSTS' not in box.calls[1][2]


def test_boundary_off_plain_restart_does_not_touch_the_registry(box, monkeypatch):
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '0')
    files_own_tab(box)
    monkeypatch.setattr(cli, 'write_registry', lambda *a, **k: pytest.fail('no registry write for a plain restart'))
    cli.app_lifecycle(box.c, 'restart', 'files')
    assert [(v, a) for v, a, _ in box.calls] == [('restart', 'files')]


# ---------------------------------------------------------------- O5

def release(tmp_path, apps):
    img = {'ref': 'ghcr.io/humanitylabs-org/x:1', 'digest': 'sha256:' + 'a' * 64}
    doc = {'version': '0.3.103', 'default_apps': {slot: {'app': a, 'repo': r, 'commit': c} for slot, (a, r, c) in apps.items()},
           'images': {k: img for k in ('wizard-app-manager', 'wizard-ux', 'wizard-agent-service')}}
    p = tmp_path / 'release-pins.json'
    p.write_text(json.dumps(doc))
    return p


NOTIF = ('notifications', 'oscarhenrycollins/notifications', PIN)


def test_release_default_gets_its_declared_operations_with_provenance(box, tmp_path, monkeypatch):
    box.rows.append(install(box.root))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': NOTIF})))
    b = box.app(box.write(), 'notifications')
    assert b['granted'] == ['notifications.list', 'notifications.mark_read', 'state.local'] and b['admissible'] is True
    g, = ba.read_grants(box.c.home)['grants']
    assert g['by'] == 'release-default' and g['release'] == '0.3.103' and g['commit'] == PIN
    assert g['source_id'] == b['source_id'] and g['installation_id'] == b['installation_id']
    assert g['permission_revision'] == b['permission_revision']   # the revision the projection publishes
    assert g['contract_digest'] == b['contract_digest']
    box.write()
    assert len(ba.read_grants(box.c.home)['grants']) == 1         # idempotent


def test_non_default_and_moved_defaults_start_with_no_grants(box, tmp_path, monkeypatch):
    box.rows.append(install(box.root))                                         # pinned at PIN
    box.rows.append(install(box.root, app_id='wizard-files', repo='humanitylabs-org/wizard-files', serial=8,
                            contract=NOTIF_CONTRACT.replace('notifications.', 'files.'), provides='files'))
    other = ('notifications', 'oscarhenrycollins/notifications', 'd' * 40)      # release pins another commit
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': other})))
    reg = box.write()
    assert box.app(reg, 'notifications')['granted'] == [] and box.app(reg, 'wizard-files')['granted'] == []
    assert ba.read_grants(box.c.home)['grants'] == []


def test_no_release_run_grants_nothing(box):
    box.rows.append(install(box.root))
    assert box.app(box.write(), 'notifications')['granted'] == []


def test_owner_revoke_sticks_across_upgrades(box, tmp_path, monkeypatch):
    box.rows.append(install(box.root))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': NOTIF})))
    box.write()
    ba.revoke(box.c.home, 'notifications')
    assert box.app(box.write(), 'notifications')['granted'] == []


def test_increase_on_update_waits_for_the_owner(box, tmp_path, monkeypatch):
    box.rows.append(install(box.root))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': NOTIF})))
    box.write()
    # Same installation updated to a newer pinned commit that declares one more operation.
    new = 'e' * 40
    install(box.root, commit=new, contract=NOTIF_CONTRACT.replace('state.local]', 'state.local, notifications.media]'))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': (NOTIF[0], NOTIF[1], new)})))
    b = box.app(box.write(), 'notifications')
    assert b['granted'] == ['notifications.list', 'notifications.mark_read', 'state.local'], {k: b.get(k) for k in ('reason', 'contract', 'granted', 'source_id', 'installation_id')}
    assert b['ungranted'] == ['notifications.media']
    assert len(ba.read_grants(box.c.home)['grants']) == 1


def test_written_while_the_boundary_is_off_and_inert_until_on(box, tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '0')
    box.rows.append(install(box.root))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': NOTIF})))
    reg = box.write()
    assert box.app(reg, 'notifications') is None and len(ba.read_grants(box.c.home)['grants']) == 1
    monkeypatch.delenv('WIZARD_RELEASE_PINS')
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '1')                         # the switch, later, no pins
    assert box.app(box.write(), 'notifications')['admissible'] is True


def test_reinstall_of_a_default_gets_a_fresh_default_grant_only_in_a_release_run(box, tmp_path, monkeypatch):
    box.rows.append(install(box.root))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(release(tmp_path, {'notifications': NOTIF})))
    box.write()
    box.rows[:] = [install(box.root, serial=9)]                                 # reinstall = new installation
    monkeypatch.delenv('WIZARD_RELEASE_PINS')
    assert box.app(box.write(), 'notifications')['granted'] == []
