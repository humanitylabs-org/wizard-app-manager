import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from wam import builder, system_functions


@pytest.mark.parametrize('snippet,rule', [
    ('navigator.mediaDevices.getUserMedia({audio:true})', 'capture'),
    ('new MediaRecorder(stream)', 'capture'),
    ('new AudioContext()', 'capture'),
    ('''navigator.permissions.query({name:'microphone'})''', 'capture'),
    ('''navigator["permissions"]["query"]({name:'camera'})''', 'capture'),
    ('navigator.share({text:"x"})', 'share'),
    ('navigator["share"]({text:"x"})', 'share'),
    ('new CloseWatcher()', 'close-watcher'),
    ('navigator.serviceWorker.register("sw.js")', 'service-worker'),
    ('navigator["serviceWorker"].getRegistrations()', 'service-worker'),
    ('self.addEventListener("install",()=>self.skipWaiting())', 'service-worker'),
    ('''self.addEventListener('fetch',e=>e.respondWith(caches.match(e.request)))''', 'service-worker'),
    ('''self.oninstall = e => caches.open('app-cache')''', 'service-worker'),
    ('addEventListener("beforeinstallprompt", prompt)', 'install-prompt'),
    ('localStorage.setItem("theme", "dark")', 'theme-storage'),
    ('localStorage.getItem("wos.skin")', 'theme-storage'),
    ('function toggleDarkMode() {}', 'theme-switch'),
    ('<button id="theme-toggle">Dark</button>', 'theme-switch'),
    ('history.pushState({rootGuard:1}, "", location.href)', 'root-guard'),
    ('addEventListener("popstate",()=>{ history.pushState({},"",location.href) })', 'root-history-bounce'),
    ('window.onpopstate = () => { history.forward() };', 'root-history-bounce'),
    ('window.onpopstate = () => history.forward();', 'root-history-bounce'),
    ('''window.onpopstate = () => history.pushState(null, '', location.href);''', 'root-history-bounce'),
])
def test_raw_implementations_rejected(tmp_path, snippet, rule):
    (tmp_path / 'screen.html').write_text(snippet)
    assert any(not r['ok'] and r['rule'] == 'system functions: ' + rule for r in system_functions.check(tmp_path))


def test_domain_callbacks_real_routes_and_reading_controls_allowed(tmp_path):
    (tmp_path / 'app.js').write_text('''
      WizardApp.ready({name:'domain',version:'1'});
      WizardApp.back.register({canGoBack:()=>route!=='home', back:()=>{route='home';render();return true}});
      const overlay=WizardApp.back.push(()=>{closeEditor();return true}); overlay.release();
      WizardApp.back.useHistory({isRoot:()=>route==='home'});
      addEventListener('popstate',()=>render(history.state));
      addEventListener('popstate',()=>{ if (route==='removed-record') history.pushState({route:'list'}, '', '/list') });
      addEventListener('popstate',()=>history.pushState(state, '', location.pathname + '?record=2'));
      window.onpopstate = () => history.pushState({record:2}, '', location.href);
      document.querySelector('record-card').addEventListener('activate', render);
      navigator.permissions.query({name:'geolocation'});
      function openRecord(id) { history.pushState({record:id}, '', '/record/'+id);render() }
      function closeRecord() { history.back() }
      WizardApp.theme.onChange(a=>applyReadingSurface(a));
      localStorage.setItem('reader-font-size', '20');
      WizardApp.share.text({text:'domain'});
      WizardApp.capture.start({kind:'audio'});
      const hold=WizardApp.update.hold('draft'); hold.release();
      // navigator.share is forbidden in implementation, not comments.
      /* new MediaRecorder(stream); */
    ''')
    assert all(r['ok'] for r in system_functions.check(tmp_path))


def test_exact_core_bytes_only_no_name_exemption(tmp_path):
    core = tmp_path / 'core'; core.mkdir()
    client = core / 'app-client.js'; client.write_text('navigator.share({text:"x"}); new MediaRecorder(stream);')
    assert not all(r['ok'] for r in system_functions.check(tmp_path))
    trust = {str(client.resolve()): hashlib.sha256(client.read_bytes()).hexdigest()}
    assert all(r['ok'] for r in system_functions.check(tmp_path, trust))
    client.write_text(client.read_text() + '\n// altered')
    assert any(r['rule'].endswith('approved asset hash') and not r['ok'] for r in system_functions.check(tmp_path, trust))


def test_app_copy_same_name_and_shared_url_inclusion(tmp_path):
    platform = tmp_path / 'platform'; platform.mkdir()
    core = platform / 'app-client.js'; core.write_text('new CloseWatcher()')
    app = tmp_path / 'app'; app.mkdir()
    (app / 'app-client.js').write_bytes(core.read_bytes())
    trust = {str(core.resolve()): hashlib.sha256(core.read_bytes()).hexdigest()}
    assert not all(r['ok'] for r in system_functions.check(app, trust))
    (app / 'app-client.js').unlink()
    (app / 'index.html').write_text('<script src="/wizard-os-home/core/app-client.js"></script>')
    assert all(r['ok'] for r in system_functions.check(app))


def test_inline_and_symlink_source_not_hidden(tmp_path):
    (tmp_path / 'index.html').write_text('<script>navigator.share({})</script>')
    assert not all(r['ok'] for r in system_functions.check(tmp_path))
    (tmp_path / 'index.html').unlink()
    outside = tmp_path.parent / (tmp_path.name + '-outside.js'); outside.write_text('new CloseWatcher()')
    (tmp_path / 'view.js').symlink_to(outside)
    assert not all(r['ok'] for r in system_functions.check(tmp_path))
    (tmp_path / 'view.js').unlink()
    outside_dir = tmp_path.parent / (tmp_path.name + '-external-dir'); outside_dir.mkdir()
    (outside_dir / 'hidden.js').write_text('new MediaRecorder(stream)')
    (tmp_path / 'web').symlink_to(outside_dir, target_is_directory=True)
    assert any(not r['ok'] and r['rule'] == 'system functions: confined source' for r in system_functions.check(tmp_path))


def test_wam_check_and_stage_publish_update_are_wired(tmp_path):
    app = builder.new_app('gate-app', tmp_path / 'dev')
    assert builder.check_app(app)['ok']
    rec = builder.install_local(app, tmp_path / 'home', stage_only=True)
    (app / 'web' / 'bad.js').write_text('navigator.share({text:"local"})')
    for proposal in (False, True):
        assert not builder.check_app(app, proposal)['ok']
    proc = subprocess.run([sys.executable, '-m', 'wam', 'check', str(app)], cwd=builder.REPO, capture_output=True, text=True)
    assert proc.returncode == 1 and not json.loads(proc.stdout)['ok']
    assert any(r['rule'] == 'system functions: share' for r in json.loads(proc.stdout)['rules'])
    with pytest.raises(ValueError, match='App check failed'):
        builder.install_local(app, tmp_path / 'new-home', stage_only=True)
    with pytest.raises(ValueError, match='App check failed'):
        builder.update_local(app, tmp_path / 'home')
    with pytest.raises(ValueError, match='Source failed checker'):
        builder.prepare_publish(app, 'example/gate-app')
    # An older staged app with unchanged illegal bytes must not skip the new gate.
    stage_json = Path(rec['compose']).parent / 'stage.json'
    old_receipt = json.loads(stage_json.read_text())
    old_receipt['source_sha256'] = builder.source_hash(app)
    stage_json.write_text(json.dumps(old_receipt))
    with pytest.raises(ValueError, match='App check failed'):
        builder.update_local(app, tmp_path / 'home')
    assert Path(rec['source']).is_dir()


def test_scaffold_has_shared_client_no_platform_copy(tmp_path):
    app = builder.new_app('gate-app', tmp_path)
    assert all(r['ok'] for r in system_functions.check(app))
    html = (app / 'web/index.html').read_text()
    assert system_functions.CLIENT_URL in html
    assert html.index(system_functions.CLIENT_URL) < html.index('app.js')
    assert not (app / 'web/theme-client.js').exists()
    js = (app / 'web/app.js').read_text()
    assert 'WizardApp.back.register' in js and 'WizardApp.back.useHistory' in js
    assert 'history.pushState' not in js
