"""Approved separate-origin exception (wizard-os #385, Files decision 2 Nov 2026; default-off).

File Browser (`files` from oscarhenrycollins/files) declares `browser_contract: {version: 1, isolation: own-tab}`.
With the browser boundary on, WAM records it as an own-tab app (no bindings, grants or listener credential), publishes
it only on its OWN origin (its own HTTPS port), removes its old route on the Wizard origin when that route is exactly
ours, and tells its listener its own address. No other app can declare its way out of the opaque frame.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from wam import browser_admission as ba
from wam import cli, links
from test_browser_admission import box, install  # noqa: F401  (fixture)

OWN_TAB = 'browser_contract: {version: 1, isolation: own-tab}\n'


@pytest.fixture(autouse=True)
def hermetic_runtipi(tmp_path, monkeypatch):
    # links falls back to the configured Runtipi root for the listener env: never this machine's real one.
    monkeypatch.setenv('RUNTIPI_ROOT', str(tmp_path / 'no-runtipi'))


def test_own_tab_contract_takes_nothing_else():
    assert ba.normalize_contract({'browser_contract': {'version': 1, 'isolation': 'own-tab'}})['contract']['isolation'] == 'own-tab'
    for extra in ({'operations': ['notifications.list']}, {'state': {'persistent_bytes': 1}}, {'navigation': []}):
        assert 'error' in ba.normalize_contract({'browser_contract': {'version': 1, 'isolation': 'own-tab', **extra}})


def test_reviewed_files_is_own_tab_with_no_browser_authority(box):
    box.rows.append(install(box.root, app_id='files', repo='oscarhenrycollins/files', contract=OWN_TAB, provides='files', port=8794))
    b = box.app(box.write(), 'files')
    assert b['isolation'] == 'own-tab' and b['reason'] == 'own_tab'
    assert b['admissible'] is False and b['granted'] == [] and 'listener_credential' not in b
    assert b['contract'] == {'version': 1, 'isolation': 'own-tab', 'operations': [], 'state': {'schema_version': 1, 'persistent_bytes': 0}}
    with pytest.raises(ValueError, match='own tab'):
        ba.grant(box.c.home, 'files', ['notifications.list'])


@pytest.mark.parametrize('app_id,repo', [('files', 'someone/files'), ('notifications', 'oscarhenrycollins/notifications'),
                                         ('files', 'local/files')])
def test_nobody_else_can_opt_out_of_the_opaque_frame(box, app_id, repo):
    box.rows.append(install(box.root, app_id=app_id, repo=repo, contract=OWN_TAB, provides='files'))
    b = box.app(box.write(), app_id)
    assert b['reason'] == 'invalid_contract' and 'isolation' not in b and b['admissible'] is False


def test_default_off_writes_no_isolation(box, monkeypatch):
    monkeypatch.delenv('WIZARD_BROWSER_BOUNDARY')
    box.rows.append(install(box.root, app_id='files', repo='oscarhenrycollins/files', contract=OWN_TAB, provides='files', port=8794))
    assert 'browser' not in next(a for a in box.write()['apps'] if a['id'] == 'files')


@pytest.fixture
def fake_tailscale(tmp_path, monkeypatch):
    """Stock-shaped `tailscale` with serve add/remove; the Wizard origin (8796) already has Files at /files/."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls, served = tmp_path / 'calls', tmp_path / 'served'
    served.write_text(json.dumps({'TCP': {'8796': {'HTTPS': True}}, 'Web': {'box.tail1234.ts.net:8796': {'Handlers': {
        '/': {'Proxy': 'http://127.0.0.1:8796'}, '/files/': {'Proxy': 'http://127.0.0.1:8794/files/'}}}}}))
    (bin_dir / 'tailscale').write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
p = Path({str(served)!r})
with open({str(calls)!r}, 'a') as f: f.write(' '.join(args) + '\\n')
if args[:2] == ['status', '--json']:
    print(json.dumps({{'BackendState': 'Running', 'Self': {{'DNSName': 'box.tail1234.ts.net.'}}}}))
elif args[:2] == ['serve', 'status']:
    print(p.read_text())
elif args[0] == 'serve':
    d = json.loads(p.read_text())
    port = next(a.split('=',1)[1] for a in args if a.startswith('--https='))
    path = next((a.split('=',1)[1] for a in args if a.startswith('--set-path=')), '/')
    web = d.setdefault('Web', {{}}).setdefault('box.tail1234.ts.net:'+port, {{}}).setdefault('Handlers', {{}})
    if args[-1] == 'off':
        web.pop(path, None)
    else:
        d.setdefault('TCP', {{}})[port] = {{'HTTPS': True}}
        web[path] = {{'Proxy': args[-1]}}
    p.write_text(json.dumps(d))
""")
    (bin_dir / 'tailscale').chmod(0o755)
    monkeypatch.setenv('PATH', f"{bin_dir}:{os.environ['PATH']}")
    return calls, served


def reg(own_tab: bool, extra=()):
    files = {'id': 'files', 'name': 'Files', 'category': 'system', 'status': 'running', 'view': {'url': 'http://127.0.0.1:8794/files/'}}
    if own_tab:
        files['browser'] = {'schema': 1, 'isolation': 'own-tab', 'reason': 'own_tab'}
    return {'apps': [{'id': 'wizard-shell', 'name': 'Wizard', 'category': 'interface', 'status': 'running',
                      'view': {'url': 'http://127.0.0.1:8796/wizard-ux/'}}, files, *extra]}


def test_boundary_off_keeps_files_on_the_wizard_origin(tmp_path, fake_tailscale):
    calls, served = fake_tailscale
    res = links.publish(reg(False), tmp_path / 'home', {'wizard-shell'})
    files = next(l for l in res['links'] if l['id'] == 'files')
    assert files['open'] == '/files/' and 'own_tab' not in files
    assert 'off' not in calls.read_text()


def test_own_tab_is_published_on_its_own_origin_and_the_old_wizard_route_closes(tmp_path, fake_tailscale):
    calls, served = fake_tailscale
    root = tmp_path / 'runtipi'
    d = root / 'apps' / 'wizardapps' / 'files'
    d.mkdir(parents=True)
    (d / 'docker-compose.yml').write_text('services:\n  files:\n    environment:\n      WIZARD_OWN_HOSTS: ${WIZARD_OWN_HOSTS:-}\n')
    res = links.publish(reg(True), tmp_path / 'home', {'wizard-shell'}, runtipi_root=root)
    files = next(l for l in res['links'] if l['id'] == 'files')
    assert files['own_tab'] is True
    assert files['url'] == files['open'] == 'https://box.tail1234.ts.net:8794/files/'
    assert files['closed_owner_routes'] == ['8796/files/'] and files['own_tab_env'] == 'written'
    log = calls.read_text()
    assert 'serve --bg --https=8794 http://127.0.0.1:8794' in log
    assert 'serve --https=8796 --set-path=/files/ off' in log
    assert '--set-path=/files/ http' not in log      # never re-added on the Wizard origin
    web = json.loads(served.read_text())['Web']
    assert '/files/' not in web['box.tail1234.ts.net:8796']['Handlers']
    env = (root / 'user-config' / 'wizardapps' / 'files' / 'app.env').read_text()
    assert 'WIZARD_BROWSER_BOUNDARY=1' in env and 'WIZARD_OWN_HOSTS=box.tail1234.ts.net:8794' in env
    # The registry hands the shell the own-origin address (an absolute URL: the shell opens it as a tab).
    out = links.apply_to_registry(reg(True), tmp_path / 'home')
    assert next(a for a in out['apps'] if a['id'] == 'files')['view']['url'] == 'https://box.tail1234.ts.net:8794/files/'
    # A second run changes nothing and stays on its own origin.
    again = next(l for l in links.publish(reg(True), tmp_path / 'home', {'wizard-shell'}, runtipi_root=root)['links'] if l['id'] == 'files')
    assert again['open'] == files['open'] and again.get('closed_owner_routes') in (None, []) and again['own_tab_env'] == 'unchanged'


def test_someone_elses_route_on_that_path_is_left_alone(tmp_path, fake_tailscale):
    calls, served = fake_tailscale
    d = json.loads(served.read_text())
    d['Web']['box.tail1234.ts.net:8796']['Handlers']['/files/'] = {'Proxy': 'http://127.0.0.1:9999/files/'}
    served.write_text(json.dumps(d))
    files = next(l for l in links.publish(reg(True), tmp_path / 'home', {'wizard-shell'})['links'] if l['id'] == 'files')
    assert files['open'] == 'https://box.tail1234.ts.net:8794/files/' and not files.get('closed_owner_routes')
    assert files['own_tab_env'] == 'not_declared'   # configured root (hermetic here) has no recipe that takes it
    assert 'off' not in calls.read_text()
    assert json.loads(served.read_text())['Web']['box.tail1234.ts.net:8796']['Handlers']['/files/'] == {'Proxy': 'http://127.0.0.1:9999/files/'}


def test_owner_routes_keep_the_own_tab_address(box):
    """A box with owner routes (routes.json) never publishes raw host URLs, but the own-tab app's own https address
    is its only way to open; a Wizard-origin route for it would be refused by its listener anyway."""
    box.rows.append(install(box.root, app_id='files', repo='oscarhenrycollins/files', contract=OWN_TAB, provides='files', port=8794))
    box.c.home.mkdir(parents=True, exist_ok=True)
    (box.c.home / 'routes.json').write_text(json.dumps({'files': {'view': '/files/'}}))
    (box.c.home / 'links.json').write_text(json.dumps({'links': [{'id': 'files', 'open': 'https://box.tail1234.ts.net:8794/files/', 'own_tab': True}]}))
    files = next(a for a in box.write()['apps'] if a['id'] == 'files')
    assert files['view']['url'] == 'https://box.tail1234.ts.net:8794/files/'


def test_env_needs_the_recipe_to_take_it(tmp_path):
    root = tmp_path / 'runtipi'
    (root / 'apps' / 'wizardapps' / 'files').mkdir(parents=True)
    (root / 'apps' / 'wizardapps' / 'files' / 'docker-compose.yml').write_text('services: {files: {}}\n')
    assert ba.own_tab_env(root, 'wizardapps', 'files', 'box.tail1234.ts.net:8794') == 'not_declared'
    assert ba.own_tab_env(root, 'wizardapps', 'files', 'https://evil/') == 'unavailable'
    assert not (root / 'user-config').exists()
