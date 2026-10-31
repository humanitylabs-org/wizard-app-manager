import http.client
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from wam import cli, index, recommended as rec, slots, store_install

SHA = 'a' * 40
IMAGE = 'ghcr.io/example/connector:1@sha256:' + 'b' * 64


def cfg(tmp_path):
    return SimpleNamespace(home=tmp_path, index='https://index.example')


def indexed(monkeypatch):
    def resolve(base, repo):
        app = 'wizard-cloudflare' if repo.endswith('wizard-cloudflare') else 'connected-accounts'
        return {'id': app, 'repo': repo, 'install': {'commit': SHA}}
    monkeypatch.setattr(index, 'resolve', resolve)


def recipe(url):
    assert '/' + SHA + '/' in url
    return f'services:\n  app:\n    image: {IMAGE}\n  helper:\n    image: {IMAGE}\n'


def test_bootstrap_excludes_both_recommendations_keeps_agent_logins():
    wanted = slots.to_install({})
    assert 'wizard-cloudflare' not in wanted and 'connected-accounts' not in wanted
    assert {'wizard-chatgpt', 'wizard-claude'} <= set(wanted)
    assert {d['app'] for d in rec.catalog()} == {'wizard-cloudflare', 'connected-accounts'}


def test_recommendations_survive_old_release_pins(monkeypatch):
    from wam import release_pins
    monkeypatch.setattr(release_pins, 'load', lambda: {'default_apps': {'shell': {'app': 'wizard-ux'}}})
    assert len(rec.catalog()) == 2
    assert slots.to_install({}) == ['wizard-ux']


def test_listing_is_source_qualified_and_does_not_install(tmp_path, monkeypatch):
    indexed(monkeypatch)
    result = rec.listing(cfg(tmp_path))
    assert len(result['apps']) == 2
    for app in result['apps']:
        assert app['selection']['source'] == 'standard'
        assert app['selection']['commit'] == SHA
        assert app['selection']['operation'] == 'install'
        assert app['installed'] is False
    assert list(tmp_path.iterdir()) == []


def test_unpublished_entry_is_left_out_of_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'resolve', lambda *a: (_ for _ in ()).throw(index.IndexError_('404')))
    assert rec.listing(cfg(tmp_path)) == {'apps': []}


def test_only_the_unpublished_entry_is_left_out(tmp_path, monkeypatch):
    def resolve(base, repo):
        if repo.endswith('wizard-connectors'):
            raise index.IndexError_('404')
        return {'id': 'wizard-cloudflare', 'repo': repo, 'install': {'commit': SHA}}
    monkeypatch.setattr(index, 'resolve', resolve)
    monkeypatch.setattr(rec.store_selection, 'resolve', lambda c, app, body: None)
    ids = [a['id'] for a in rec.listing(cfg(tmp_path))['apps']]
    assert ids == ['wizard-cloudflare']


def test_prefetch_pulls_only_with_fake_runner_and_keeps_registry_unchanged(tmp_path, monkeypatch):
    indexed(monkeypatch)
    calls = []
    registry = tmp_path / 'apps.json'
    registry.write_text('{"apps":[]}')
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if 'inspect' in argv else 0)
    result = rec.prefetch(cfg(tmp_path), runner=run, fetch=recipe)
    assert [a['state'] for a in result['apps']] == ['cached', 'cached']
    pulls = [a for a in calls if 'pull' in a]
    assert len(pulls) == 2 and all(a[-3:] == ['docker', 'pull', IMAGE] for a in pulls)
    assert all('nice' in a and '19' in a for a in pulls)
    assert all('pull' in a or a[:3] == ['docker', 'image', 'inspect'] for a in calls)
    assert registry.read_text() == '{"apps":[]}'
    assert not (tmp_path / 'secrets').exists()
    assert json.loads((tmp_path / 'prefetch.json').read_text()) == result


def test_cached_images_are_not_pulled_again(tmp_path, monkeypatch):
    indexed(monkeypatch)
    calls = []
    result = rec.prefetch(cfg(tmp_path), runner=lambda a, **kw: (calls.append(a) or SimpleNamespace(returncode=0)), fetch=recipe)
    assert all(a['state'] == 'cached' for a in result['apps'])
    assert not any('pull' in a for a in calls)


def test_pull_failure_is_best_effort_and_does_not_install(tmp_path, monkeypatch):
    indexed(monkeypatch)
    def fail(a, **kw):
        raise subprocess.CalledProcessError(1, a, stderr='secret must not escape')
    result = rec.prefetch(cfg(tmp_path), runner=fail, fetch=recipe)
    assert all(a['state'] == 'unavailable' for a in result['apps'])
    assert 'secret must not escape' not in json.dumps(result)


@pytest.mark.parametrize('image', ['wizard-connectors:reviewed', '${MISSING}', '--help', 'https://bad/image'])
def test_refuses_local_or_unresolved_images(image):
    selection = {'repo': 'org/repo', 'commit': SHA, 'entry': {}}
    with pytest.raises(ValueError):
        rec.recipe_images(selection, lambda u: f'services:\n  app:\n    image: "{image}"\n')


def test_default_image_substitution_and_deduplication():
    selection = {'repo': 'org/repo', 'commit': SHA, 'entry': {}}
    assert rec.recipe_images(selection, lambda u: f'services:\n  app:\n    image: ${{IMAGE:-{IMAGE}}}\n') == [IMAGE]


def test_background_request_is_idempotent_and_resumes(tmp_path, monkeypatch):
    gate = threading.Event()
    calls = []
    def worker(c):
        calls.append(c)
        gate.wait(2)
        (c.home / 'prefetch.json').write_text('{"state":"finished"}')
    monkeypatch.setattr(rec, '_worker', None)
    monkeypatch.setattr(rec, 'prefetch', worker)
    c = cfg(tmp_path)
    assert rec.start(c)['state'] == 'running'
    assert rec.start(c)['state'] == 'running'
    assert (tmp_path / 'prefetch-requested').exists()
    gate.set(); rec._worker.join(3)
    assert rec.start(c)['state'] == 'finished' and len(calls) == 1
    (tmp_path / 'prefetch.json').unlink()
    rec.resume(c); rec._worker.join(3)
    assert len(calls) == 2


def test_store_recommendations_preserve_owner_and_origin_checks(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    (tmp_path / 'links.json').write_text('{"host":"box.example"}')
    indexed(monkeypatch)
    monkeypatch.setattr(store_install, 'peer_uid', lambda p: 0)
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'owner@example.com')
    monkeypatch.setattr(store_install, 'served_origins', lambda h: {'https://box.example'})
    monkeypatch.setattr(cli.StoreHandler, 'cfg', c, raising=False)
    starts = []
    monkeypatch.setattr(rec, 'start', lambda c: starts.append(c) or {'state': 'running'})
    server = ThreadingHTTPServer(('127.0.0.1', 0), cli.StoreHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        def call(method, headers):
            h = http.client.HTTPConnection(*server.server_address)
            h.request(method, '/_recommended', '{}' if method == 'POST' else None, headers)
            r = h.getresponse(); body = json.loads(r.read()); h.close()
            return r.status, body
        headers = {'X-Forwarded-For': '100.64.0.9', 'Tailscale-User-Login': 'owner@example.com', 'Origin': 'https://box.example',
                   'Content-Type': 'application/json', 'X-Wizard-Store': '1'}
        assert call('GET', {})[0] == 403
        assert call('GET', headers)[0] == 200
        assert call('POST', {**headers, 'Origin': 'https://evil.example'})[0] == 403
        assert starts == []
        assert call('POST', headers) == (202, {'state': 'running'})
        assert starts == [c]
    finally:
        server.shutdown(); server.server_close(); t.join()
