#!/usr/bin/env python3
"""Thales end-to-end proof for custom app views (wizard-os #30), through the real `wam` CLI.

A fresh Wizard home; the Habits app built by the builder and running in Docker; the views door
running as its own process. Customize -> apply -> use the API -> app update (merge) -> app update
(conflict, resolve) -> kill/restart door and app container -> revert -> reactivate.
Writes views-e2e.json; leaves the door and app running for the preview when --leave-running.
"""
import json, os, shutil, signal, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

V = Path(os.environ.get('E2E_ROOT', '/Volumes/ThalesMedia/WizardBuilder/20261012'))
SRC = V / 'views-src'
HOME = V / 'views-e2e' / 'home'
DEV = V / 'views-e2e' / 'dev'
PY = str(V / 'venv/bin/python')
APP_PORT, DOOR_PORT = 18811, 18910
TRUST = [h for h in os.environ.get('E2E_TRUST_HOSTS', '').split(',') if h]
ENV = dict(os.environ, WIZARD_OS_HOME=str(HOME), PYTHONPATH=str(SRC), PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
checks, door = [], None


def ok(name, cond, detail=None):
    checks.append({'check': name, 'ok': bool(cond), 'detail': detail})
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f'  {detail}'), flush=True)
    if not cond:
        finish(1)


def wam(*args, expect=0):
    r = subprocess.run([PY, '-m', 'wam', *args], env=ENV, cwd=SRC, capture_output=True, text=True, timeout=900)
    if r.returncode != expect:
        ok('wam ' + ' '.join(args), False, (r.stdout + r.stderr)[-1500:])
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        return {'raw': r.stdout, 'err': r.stderr}


def http(path, body=None, port=DOOR_PORT, **headers):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', **headers}, method='POST' if body is not None else 'GET')
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    try:
        r = urllib.request.build_opener(NR).open(req, timeout=10)
        return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
    except OSError as e:
        return 0, str(e), {}


def wait(path, port=DOOR_PORT, tries=120):
    for _ in range(tries):
        if http(path, port=port)[0] == 200:
            return True
        time.sleep(.5)
    return False


def start_door():
    global door
    args = [PY, '-m', 'wam', 'views', 'serve', '--port', str(DOOR_PORT)]
    for h in TRUST:
        args += ['--trust-host', h, '--trust-origin', 'https://' + h]
    log = open(V / 'views-door.log', 'a')
    door = subprocess.Popen(args, env=ENV, cwd=SRC, stdout=log, stderr=log, start_new_session=True)
    (V / 'views-door.pid').write_text(str(door.pid))
    return wait('/views/_index.json')


def stop_door():
    if door and door.poll() is None:
        os.killpg(door.pid, signal.SIGTERM); door.wait(10)


def compose(*args, app='habit-tracker'):
    st = json.loads((HOME / f'staged-apps/{app}/stage.json').read_text())
    return subprocess.run(['docker', 'compose', '-p', st['project'], '-f', st['compose'], *args], env=ENV,
                          capture_output=True, text=True, timeout=900)


def edit(p, old, new):
    t = p.read_text(); assert old in t, (p, old); p.write_text(t.replace(old, new, 1))


def finish(code=0):
    out = {'passed': sum(c['ok'] for c in checks), 'failed': sum(not c['ok'] for c in checks), 'checks': checks,
           'home': str(HOME), 'door': f'127.0.0.1:{DOOR_PORT}', 'app': f'127.0.0.1:{APP_PORT}',
           'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    (V / 'views-e2e.json').write_text(json.dumps(out, indent=2))
    if code or '--leave-running' not in sys.argv:
        stop_door()
        for name in ('habit-tracker', 'habit-notes'):
            if (HOME / f'staged-apps/{name}/stage.json').exists() and '--leave-running' not in sys.argv:
                compose('down', '-v', app=name)
    sys.exit(code)


# ---- fresh box -----------------------------------------------------------------
for name in ('habit-tracker', 'habit-notes'):
    if (HOME / f'staged-apps/{name}/stage.json').exists():
        compose('down', '-v', app=name)
if (V / 'views-door.pid').exists():
    try: os.killpg(int((V / 'views-door.pid').read_text()), signal.SIGTERM)
    except (ProcessLookupError, ValueError): pass
shutil.rmtree(V / 'views-e2e', ignore_errors=True)
DEV.mkdir(parents=True)

wam('new', 'habit-tracker', '--dev-root', str(DEV), '--request', 'track my daily habits', '--name', 'Habits')
dev = DEV / 'habit-tracker'
origins = []
for h in TRUST:
    origins += ['--trust-origin', 'https://' + h]
st = wam('install-local', str(dev), '--stage-only', '--run', '--port', str(APP_PORT), *origins)
ok('app built and running in Docker', wait('/healthz', port=APP_PORT), st)
ok('door starts as its own process', start_door())
day = time.strftime('%Y-%m-%d')
for n in ('Drink water', 'Stretch 10 minutes', 'Read 10 pages'):
    http('/api/habits', {'name': n, 'day': day}, port=APP_PORT)

code, html, _ = http('/apps/habit-tracker/')
ok('uncustomized app opens its own screen through the door', code == 200 and '<h1>Habits</h1>' in html and 'data-wz-overlay' not in html)
ok('no views folder until the owner customizes', not (HOME / 'views').exists())

# ---- customize a real screen ---------------------------------------------------
c = wam('views', 'customize', 'habit-tracker')
web = Path(c['edit'])
ok('customize copies the screen source into views/<id>/src at the installed version', (web / 'index.html').is_file() and c['state'] == 'editing')
ok('nothing switches before a build passes', wam('views', 'status', 'habit-tracker')['open'] == '/apps/habit-tracker/')
edit(web / 'index.html', '<h1>Habits</h1>', '<h1>My rituals</h1>')
(web / 'styles.css').write_text((web / 'styles.css').read_text() + '''
/* Owner redesign: card list */
.wz-list { display: grid; gap: 10px; background: none; border: 0; }
.wz-list > li { border-radius: 16px; background: color-mix(in srgb, var(--wz-text, #fff) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--wz-text, #fff) 12%, transparent); }
.wz-head h1 { letter-spacing: -.02em; }
''')
a = wam('views', 'apply', 'habit-tracker')
ok('apply builds and switches to the owner version', a['active'] and a['serving'] and a['state'] == 'active', a)
code, html, h = http('/views/habit-tracker/')
ok('custom screen served at /views/habit-tracker/', code == 200 and '<h1>My rituals</h1>' in html)
ok('custom screen served with a same-origin CSP', "default-src 'self'" in h.get('Content-Security-Policy', ''))
code, body, _ = http(f'/views/habit-tracker/api/habits?day={day}')
ok('custom screen reads the real app through its API door', code == 200 and 'Drink water' in body, body[:200])
code, body, _ = http('/views/habit-tracker/api/habits', {'name': 'Walk outside', 'day': day})
ok('custom screen writes through the API door', code in (200, 201) and 'Walk outside' in http(f'/apps/habit-tracker/api/habits?day={day}')[1], body[:200])
ok('door forwards only to the declared api (no /mcp, no traversal)',
   http('/views/habit-tracker/api/../mcp')[0] == 404 and http('/views/habit-tracker/api/%2e%2e/mcp')[0] == 404)
ok('cross-origin write refused', http('/views/habit-tracker/api/habits', {'name': 'x', 'day': day}, Origin='https://evil.example')[0] == 403)
ok('unknown Host refused (DNS rebinding)', http('/views/habit-tracker/', Host='evil.example')[0] == 403)
ok('launcher index points at the custom screen', json.loads(http('/views/_index.json')[1])['apps']['habit-tracker']['open'] == '/views/habit-tracker/')

# ---- upstream update, clean merge ------------------------------------------------
edit(dev / 'web/index.html', 'Add one below, then tap it each day you do it.', 'Add one below. Tap it each day you finish it.')
u = wam('update-local', str(dev), '--run')
ok('app update re-staged and container rebuilt', u.get('update') == 'updated' and wait('/healthz', port=APP_PORT), u)
ok('app data survived the app update', 'Walk outside' in http(f'/api/habits?day={day}', port=APP_PORT)[1])
first = a['serving']
u = wam('views', 'update', 'habit-tracker')
html = http('/views/habit-tracker/')[1]
ok('update merged into the custom screen and rebuilt', u['state'] == 'active' and u['serving'] != first, u)
ok('owner change kept and app change arrived', '<h1>My rituals</h1>' in html and 'Tap it each day you finish it.' in html)

# ---- upstream update, conflict -----------------------------------------------------
edit(dev / 'web/index.html', '<h1>Habits</h1>', '<h1>Daily habits</h1>')
wam('update-local', str(dev), '--run'); wait('/healthz', port=APP_PORT)
kept = u['serving']
u = wam('views', 'update', 'habit-tracker')
ok('conflicting update recorded, not guessed', u['state'] == 'update_conflict' and u['conflicts'] == ['web/index.html'], u)
ok('owner version stays on screen during the conflict', u['serving'] == kept and '<h1>My rituals</h1>' in http('/views/habit-tracker/')[1])
ok('conflict note is in the launcher index', 'stays on screen' in (json.loads(http('/views/_index.json')[1])['apps']['habit-tracker']['note'] or ''))
r = wam('views', 'resolve', 'habit-tracker', '--keep', 'mine')
ok('resolve --keep mine finishes the update', r['state'] == 'active' and r['pending'] is None, r)
ok('owner title kept after resolve', '<h1>My rituals</h1>' in http('/views/habit-tracker/')[1])

# ---- restarts ------------------------------------------------------------------------
stop_door()
ok('door is down', http('/views/_index.json')[0] == 0)
ok('door restarted', start_door())
ok('custom route survives door restart', '<h1>My rituals</h1>' in http('/views/habit-tracker/')[1])
compose('restart')
ok('app container restarted', wait('/healthz', port=APP_PORT))
ok('custom screen API works after app restart', 'Walk outside' in http(f'/views/habit-tracker/api/habits?day={day}')[1])

# ---- revert / reactivate -------------------------------------------------------------
r = wam('views', 'revert', 'habit-tracker')
code, _, h = http('/views/habit-tracker/')
ok('revert puts the app screen back', not r['active'] and code == 307 and h.get('Location') == '/apps/habit-tracker/')
ok('reverted app shows the updated upstream screen', '<h1>Daily habits</h1>' in http('/apps/habit-tracker/')[1])
log = subprocess.run(['git', '-C', str(HOME / 'views/habit-tracker/src'), 'log', '--format=%s', 'main'], capture_output=True, text=True).stdout
ok('history kept after revert', 'Owner changes' in log and 'App update' in log, log)
wam('views', 'reactivate', 'habit-tracker')
ok('reactivate brings the custom screen back', '<h1>My rituals</h1>' in http('/views/habit-tracker/')[1])

# ---- screenshots ---------------------------------------------------------------------
shots = V / 'views-shots'; shutil.rmtree(shots, ignore_errors=True); shots.mkdir()
node = subprocess.run(['node', '-e', r'''
let pw;try{pw=require('playwright')}catch{pw=require('playwright-core')}
(async()=>{const b=await pw.chromium.launch();const c=await b.newContext({viewport:{width:393,height:760},colorScheme:'dark'});
const p=await c.newPage();const errs=[];p.on('pageerror',e=>errs.push(String(e)));
for(const [n,u] of [['app','/apps/habit-tracker/'],['custom','/views/habit-tracker/']]){
 await p.goto('http://127.0.0.1:'+process.argv[1]+u);await p.waitForSelector('#habits li',{timeout:15000});
 await p.screenshot({path:process.argv[2]+'/'+n+'.png'});}
await p.fill('#habit-name','Meditate');await p.click('#add');await p.waitForFunction(()=>[...document.querySelectorAll('#habits li')].some(l=>l.textContent.includes('Meditate')),null,{timeout:10000});
console.log(JSON.stringify({errs,ok:true}));await b.close()})().catch(e=>{console.log(JSON.stringify({ok:false,e:String(e)}));process.exit(1)});
''', str(DOOR_PORT), str(shots)], env={**ENV, 'NODE_PATH': '/Users/thales/workspace/wizard-mobile-link-candidate/node_modules'},
    capture_output=True, text=True, timeout=180)
res = json.loads((node.stdout.strip().splitlines() or ['{}'])[-1] or '{}')
ok('custom screen works in a real browser (add a habit through the door)', node.returncode == 0 and res.get('ok') and not res.get('errs'), node.stdout[-500:] + node.stderr[-500:])

# ---- overlay on a second real app: "move the add button to the top" ---------------------
wam('new', 'habit-notes', '--dev-root', str(DEV), '--request', 'track my daily habits', '--name', 'Habit notes')
dev2 = DEV / 'habit-notes'
wam('install-local', str(dev2), '--stage-only', '--run', '--port', str(APP_PORT + 1), *origins)
ok('second app running', wait('/healthz', port=APP_PORT + 1))
o = Path(wam('views', 'customize', 'habit-notes', '--mode', 'overlay')['edit'])
(o / 'overlay.css').write_text('.wz-app { display: flex; flex-direction: column; }\n#add-form { order: -1; margin: 0 0 12px; }\n')
(o / 'overlay.json').write_text(json.dumps({'requires': ['#add-form', '.wz-app']}))
(o / 'overlay.js').write_text('document.documentElement.dataset.ownerOverlay = "on";\n')
ok('overlay applies', wam('views', 'apply', 'habit-notes')['active'])
probe = r'''
let pw;try{pw=require('playwright')}catch{pw=require('playwright-core')}
(async()=>{const b=await pw.chromium.launch();const p=await b.newPage({viewport:{width:393,height:760},colorScheme:'dark'});
await p.goto('http://127.0.0.1:'+process.argv[1]+'/apps/habit-notes/');await p.waitForTimeout(1500);
const r=await p.evaluate(()=>{const f=document.querySelector('form'),l=document.querySelector('#habits');
 return {formTop:f?f.getBoundingClientRect().top:null,listTop:l?l.getBoundingClientRect().top:null,owner:document.documentElement.dataset.ownerOverlay||null,
 tags:document.querySelectorAll('[data-wz-overlay]').length}});
if(process.argv[3])await p.screenshot({path:process.argv[3]});console.log(JSON.stringify(r));await b.close()})();
'''
def browse(shot=''):
    r = subprocess.run(['node', '-e', probe, str(DOOR_PORT), 'x', shot], env={**ENV, 'NODE_PATH': '/Users/thales/workspace/wizard-mobile-link-candidate/node_modules'},
                       capture_output=True, text=True, timeout=120)
    try: return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception: return {'err': r.stdout[-300:] + r.stderr[-300:]}
r = browse(str(shots / 'overlay.png'))
ok('overlay moves the add box above the list in a real browser', r.get('formTop') is not None and r['formTop'] < r['listTop'] and r.get('owner') == 'on', r)
(o / 'overlay.js').write_text('this is not javascript (\n')
r = browse()
time.sleep(1)
st = wam('views', 'status', 'habit-notes')
ok('owner script syntax error switches the overlay off and leaves a note', st.get('suspended') and 'script failed' in (st.get('note') or ''), st)
(o / 'overlay.js').unlink()
ok('fixed overlay applies again', wam('views', 'apply', 'habit-notes')['active'] and not wam('views', 'status', 'habit-notes').get('suspended'))
edit(dev2 / 'web/index.html', 'id="add-form"', 'id="new-habit-form"')
wam('update-local', str(dev2), '--run'); wait('/healthz', port=APP_PORT + 1)
u = wam('views', 'update', 'habit-notes')
ok('app update that removes the target switches the overlay off with a note', u.get('suspended') and '#add-form' in (u.get('note') or ''), u)
r = browse()
ok('app opens normally after its overlay is switched off', r.get('tags') == 0 and r.get('formTop') is not None, r)
(o / 'overlay.css').write_text('.wz-app { display: flex; flex-direction: column; }\n#new-habit-form { order: -1; margin: 0 0 12px; }\n')
(o / 'overlay.json').write_text(json.dumps({'requires': ['#new-habit-form', '.wz-app']}))
ok('agent re-targets the overlay after the update', wam('views', 'apply', 'habit-notes')['active'])
r = browse(str(shots / 'overlay-after-update.png'))
ok('re-targeted overlay works on the updated app', r.get('formTop') is not None and r['formTop'] < r['listTop'], r)
finish(0)
