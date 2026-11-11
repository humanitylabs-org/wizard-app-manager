"""Every Wizard app ships the Try it switch contract (wizard.app-switches/1, wizard-os docs/APP_SWITCHES.md).

`wam new` starts apps with it; `wam check` warns when a repo's wizard.md lacks it; `wam check --installed` and
`wam doctor` warn about installed apps that serve no switch endpoint.
"""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from wam import app_switches, builder

REPO = Path(builder.REPO)


def _load(template, tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WIZARD_SWITCHES_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location(f'tpl_{template.replace("-", "_")}', REPO / 'templates' / template / 'app/server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(port, path, method='GET', headers=None, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


OWNER = {'Host': 'box.tail0.ts.net', 'X-Wizard-Switch': '1', 'Tailscale-User-Login': 'owner@example.com',
         'Origin': 'https://box.tail0.ts.net'}


def test_templates_declare_and_serve_the_contract(tmp_path, monkeypatch):
    for template in ('hello-wizard', 'list'):
        text = (REPO / 'templates' / template / 'wizard.md').read_text()
        assert 'contract: wizard.app-switches/1' in text and 'path: /api/switches' in text
        mod = _load(template, tmp_path / template, monkeypatch)
        srv = ThreadingHTTPServer(('127.0.0.1', 0), mod.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            assert _call(port, '/api/switches', headers=OWNER) == (200, {'contract': 'wizard.app-switches/1', 'switches': []})
            # owner only: no Serve login, a box-local host, a bearer, a foreign origin
            assert _call(port, '/api/switches', headers={k: v for k, v in OWNER.items() if k != 'Tailscale-User-Login'})[0] == 403
            assert _call(port, '/api/switches', headers={**OWNER, 'Host': f'127.0.0.1:{port}'})[0] == 403
            assert _call(port, '/api/switches', headers={**OWNER, 'Authorization': 'Bearer x'})[0] == 403
            post = {**OWNER, 'Content-Type': 'application/json'}
            assert _call(port, '/api/switches', 'POST', {**post, 'Origin': 'https://evil.example'}, b'{"id":"a","on":true}')[0] == 403
            assert _call(port, '/api/switches', 'POST', post, b'{"id":1}')[0] == 400
            assert _call(port, '/api/switches', 'POST', post, b'{"id":"a","on":true}')[0] == 404
            mod.SWITCHES.append({'id': 'a', 'name': 'A', 'description': '', 'default': False, 'issues': []})
            code, doc = _call(port, '/api/switches', 'POST', post, b'{"id":"a","on":true}')
            assert code == 200 and doc['switches'][0]['on'] is True and mod.switch_on('a')
        finally:
            srv.shutdown()


def test_new_app_has_the_contract_and_check_warns_when_it_goes(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, '_home', lambda: tmp_path)
    src = Path(builder.new_app('switch-probe', tmp_path / 'dev'))
    rule = 'switches: Try it contract declared (wizard.app-switches/1)'
    assert 'contract: wizard.app-switches/1' in (src / 'wizard.md').read_text()
    assert 'wizard_switches_install(Handler)' in (src / 'app/server.py').read_text()
    assert rule not in [r['rule'] for r in builder.check_app(src)['rules'] if not r['ok']]
    md = src / 'wizard.md'
    md.write_text(md.read_text().replace('switches:\n  contract: wizard.app-switches/1\n  path: /api/switches\n', ''))
    out = builder.check_app(src)
    assert rule in [w['rule'] for w in out['warnings']]


def test_missing_lists_installed_apps_without_a_declaration(tmp_path, monkeypatch):
    home, root = tmp_path / 'home', tmp_path / 'runtipi'
    home.mkdir()
    (home / 'app-manager.json').write_text(json.dumps({'runtipi_root': str(root)}))
    apps = [
        {'id': 'has', 'name': 'Has', 'urn': 'has:wizardapps', 'view': {'url': '/has/', 'local': 'http://127.0.0.1:9001'}},
        {'id': 'lacks', 'name': 'Lacks', 'urn': 'lacks:wizardapps', 'view': {'url': '/lacks/', 'local': 'http://127.0.0.1:9002'}},
        {'id': 'headless', 'name': 'Headless', 'urn': 'headless:wizardapps'},
    ]
    (home / 'apps.json').write_text(json.dumps({'apps': apps}))
    for a, extra in (('has', 'switches: /api/switches\n'), ('lacks', ''), ('headless', '')):
        d = root / 'apps/wizardapps' / a
        d.mkdir(parents=True)
        (d / 'wizard.md').write_text(f'---\nid: {a}\nversion: 1.0.0\n{extra}---\n')
    monkeypatch.delenv('RUNTIPI_ROOT', raising=False)
    gaps = app_switches.missing(home)
    assert [g['id'] for g in gaps] == ['lacks'] and 'switches' in gaps[0]['why']
