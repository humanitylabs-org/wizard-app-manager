#!/usr/bin/env python3
"""Exercise the official UX resource widget against WAM's real HTTP projection with labeled fixture data.

Usage: python scripts/smoke-resources-ui.py /path/to/wizard-ux
Requires a local Chromium; no Docker, user profile, live session DB or provider tokens.
"""
import json
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam.cli import Handler

ux = Path(sys.argv[1]).resolve()
html = b'''<!doctype html><title>Synthetic resource contract smoke</title>
<body><h1>Synthetic resource contract smoke</h1><main data-home-resources></main><pre id="result">PENDING</pre>
<script>window.__wos={idx:0,desks:[{wins:[]}]};window.WosUsage={normalize:j=>j};</script>
<script src="/resources.js"></script><script>
(async()=>{try{
await WosResources.update(true);
const t=document.querySelector('[data-home-resources]').innerText;
for(const s of ['CPU','12%','Memory','42%','Disk','63%'])if(!t.includes(s))throw Error('Missing widget '+s+': '+t);
document.querySelector('[data-res-open="server"]').click();
const d=document.querySelector('[role="dialog"]');if(!d)throw Error('No resources dialog');
if(!d.innerText.includes('8 GB total')||!d.innerText.includes('37 GB free'))throw Error('Capacity missing: '+d.innerText);
d.querySelector('[data-res-close]').click();if(document.querySelector('[role="dialog"]'))throw Error('Back failed');
document.getElementById('result').textContent='UI PASS: real widget fetched projected numbers, opened capacity details and returned Home';
}catch(e){document.getElementById('result').textContent='UI FAIL: '+e.message;}})();
</script>'''


def snapshot(*a, **k):
    return dict(cpuLoad=12, percentUsedMemory=42, percentUsed=63, memoryTotal=8,
                diskUsed=63, diskSize=100, JWT_SECRET='synthetic-private-must-not-escape')


class Smoke(Handler):
    cfg = SimpleNamespace(runtipi=lambda: SimpleNamespace(req=snapshot))
    token = 'synthetic-only'

    def do_GET(self):
        if self.path in ('/', '/resources.js'):
            data = html if self.path == '/' else (ux / 'web/core/resources.js').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/javascript')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/wizard/ai-usage':
            self._json(200, {'providers': []})
        else:
            super().do_GET()


server = ThreadingHTTPServer(('127.0.0.1', 0), Smoke)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    chrome = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chrome:
        raise SystemExit('Chromium unavailable: UI smoke not run')
    r = subprocess.run([chrome, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                        '--dump-dom', '--virtual-time-budget=5000', f'http://127.0.0.1:{server.server_port}/'],
                       capture_output=True, text=True, timeout=30)
    if r.returncode or '<pre id="result">UI PASS:' not in r.stdout:
        print(r.stdout)
        print(r.stderr[-2000:])
        raise SystemExit('UI smoke failed')
    # The injected secret must never reach the DOM, including script or widget data.
    assert 'synthetic-private-must-not-escape' not in r.stdout
    print(json.dumps({'ui': 'PASS', 'checks': ['numeric widget', 'capacity dialog', 'Back to Home', 'no secret in DOM'],
                      'ux_source': str(ux), 'data': 'explicit synthetic fixture, not live metrics'}))
finally:
    server.shutdown()
    server.server_close()
    thread.join()
