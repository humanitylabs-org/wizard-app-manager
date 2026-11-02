"""PROPOSED second reviewed separate-origin exception (wizard-os issue 386; awaiting Miguel's approval; default-off).

The Hermes dashboard (`hermes-agent` from oscarhenrycollins/hermes-agent) declares
`browser_contract: {version: 1, isolation: own-tab}`. With the browser boundary on, WAM records it as an own-tab app
(no bindings, grants or listener credential), keeps it on its OWN origin (port 9119, root path; the agent dashboard
never had a Wizard-origin path) and tells its listener its own address. Hermes itself is not patched: the dashboard
keeps its own login. Nobody else can declare their way out of the opaque frame.
"""
import json

import pytest

from wam import browser_admission as ba
from wam import links
from test_browser_admission import box, install  # noqa: F401  (fixture)
from test_browser_own_tab import OWN_TAB, fake_tailscale, hermetic_runtipi  # noqa: F401  (fixtures)


def test_hermes_dashboard_is_own_tab_with_no_browser_authority(box):
    box.rows.append(install(box.root, app_id='hermes-agent', repo='oscarhenrycollins/hermes-agent', contract=OWN_TAB,
                            provides='agent', port=9119))
    b = box.app(box.write(), 'hermes-agent')
    assert b['isolation'] == 'own-tab' and b['reason'] == 'own_tab'
    assert b['admissible'] is False and b['granted'] == [] and 'listener_credential' not in b
    with pytest.raises(ValueError, match='own tab'):
        ba.grant(box.c.home, 'hermes-agent', ['notifications.list'])


@pytest.mark.parametrize('app_id,repo', [('hermes-agent', 'someone/hermes-agent'), ('hermes-agent', 'local/hermes-agent'),
                                         ('hermes-frontend-3', 'oscarhenrycollins/hermes-agent'),
                                         ('hermes-frontend-3', 'humanitylabs-org/hermes-frontend-3')])
def test_only_the_reviewed_dashboard_source_is_an_exception(box, app_id, repo):
    box.rows.append(install(box.root, app_id=app_id, repo=repo, contract=OWN_TAB, provides='agent'))
    b = box.app(box.write(), app_id)
    assert b['reason'] == 'invalid_contract' and 'isolation' not in b and b['admissible'] is False


def test_default_off_writes_no_isolation(box, monkeypatch):
    monkeypatch.delenv('WIZARD_BROWSER_BOUNDARY')
    box.rows.append(install(box.root, app_id='hermes-agent', repo='oscarhenrycollins/hermes-agent', contract=OWN_TAB,
                            provides='agent', port=9119))
    assert 'browser' not in next(a for a in box.write()['apps'] if a['id'] == 'hermes-agent')


def _reg(own_tab: bool):
    dash = {'id': 'hermes-agent', 'name': 'Hermes Agent', 'category': 'agent', 'status': 'running',
            'view': {'url': 'http://127.0.0.1:9119/'}}
    if own_tab:
        dash['browser'] = {'schema': 1, 'isolation': 'own-tab', 'reason': 'own_tab'}
    return {'apps': [{'id': 'wizard-shell', 'name': 'Wizard', 'category': 'interface', 'status': 'running',
                      'view': {'url': 'http://127.0.0.1:8796/wizard-ux/'}}, dash]}


def test_dashboard_stays_on_its_own_origin_and_its_listener_learns_its_address(tmp_path, fake_tailscale):
    calls, served = fake_tailscale
    off = next(l for l in links.publish(_reg(False), tmp_path / 'home', {'wizard-shell'})['links'] if l['id'] == 'hermes-agent')
    assert off['open'] == 'https://box.tail1234.ts.net:9119/' and 'own_tab' not in off   # unchanged: never a Wizard path
    root = tmp_path / 'runtipi'
    d = root / 'apps' / 'wizardapps' / 'hermes-agent'
    d.mkdir(parents=True)
    (d / 'docker-compose.yml').write_text('services:\n  hermes-agent:\n    environment:\n      WIZARD_OWN_HOSTS: ${WIZARD_OWN_HOSTS:-}\n')
    on = next(l for l in links.publish(_reg(True), tmp_path / 'home', {'wizard-shell'}, runtipi_root=root)['links']
              if l['id'] == 'hermes-agent')
    assert on['own_tab'] is True and on['url'] == on['open'] == 'https://box.tail1234.ts.net:9119/'
    assert on['own_tab_env'] == 'written' and not on.get('closed_owner_routes')
    log = calls.read_text()
    assert '--set-path=/hermes-agent/' not in log and 'off' not in log
    env = (root / 'user-config' / 'wizardapps' / 'hermes-agent' / 'app.env').read_text()
    assert 'WIZARD_BROWSER_BOUNDARY=1' in env and 'WIZARD_OWN_HOSTS=box.tail1234.ts.net:9119' in env
    assert 'ASSWORD' not in env   # never a secret: the dashboard login stays the owner's
    out = links.apply_to_registry(_reg(True), tmp_path / 'home')
    assert next(a for a in out['apps'] if a['id'] == 'hermes-agent')['view']['url'] == 'https://box.tail1234.ts.net:9119/'


def test_an_older_recipe_without_the_variable_gets_no_env(tmp_path, fake_tailscale):
    root = tmp_path / 'runtipi'
    d = root / 'apps' / 'wizardapps' / 'hermes-agent'
    d.mkdir(parents=True)
    (d / 'docker-compose.yml').write_text('services:\n  hermes-agent:\n    environment:\n      FRONT_PORT: 9119\n')
    on = next(l for l in links.publish(_reg(True), tmp_path / 'home', {'wizard-shell'}, runtipi_root=root)['links']
              if l['id'] == 'hermes-agent')
    assert on['own_tab_env'] == 'not_declared'
    assert not (root / 'user-config' / 'wizardapps' / 'hermes-agent' / 'app.env').exists()
