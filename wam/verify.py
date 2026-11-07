"""`wam verify <source>`: the one fixed check an app needs before install. No test authoring.

Builds the app's container, starts it on loopback, runs its API round trip (create, list, update, restart the
container, read back), runs `wam check`, and takes one phone-width screenshot of the view per skin when a headless
Chromium is available. Then removes the container, its data volume and the image, prints a short receipt and exits
non-zero on any failure.

The round trip: built in for `--kind list` apps (web/list.json), otherwise declared in the app's verify.json:

    {"round_trip": [
      {"do": "POST /api/things", "body": {"name": "Verify {{run}}"}, "status": 201, "save": {"tid": "things.-1.id"}},
      {"do": "GET /api/things", "expect": {"things.-1.name": "Verify {{run}}"}},
      {"do": "POST /api/things/{{tid}}/done", "body": {"done": true}},
      {"restart": true},
      {"do": "GET /api/things", "expect": {"things.-1.done": true}}
    ]}

`{{run}}` is unique per run, `{{uuid:name}}` a fresh UUID reused by name, saved values are `{{name}}`.
Paths are dotted keys and list indexes (negative counts from the end).
"""
from __future__ import annotations

import argparse
import glob
import http.client
import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import wizardmd

PHONE = (390, 844)
CHROMES = ('chrome-headless-shell', 'headless_shell', 'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable')


class Failed(Exception):
    pass


def _run(argv, timeout=300, **kw):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)
    if r.returncode:
        tail = (r.stderr or r.stdout).strip().splitlines()[-6:]
        raise Failed(' '.join(argv[:3]) + ' failed: ' + ' | '.join(tail))
    return r.stdout.strip()


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            headers = {'Host': f'127.0.0.1:{self.port}'}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers.update({'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}'})
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else None
            except ValueError:
                return r.status, raw.decode(errors='replace')
        finally:
            c.close()

    def wait(self, path='/healthz', seconds=30):
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            try:
                status, _ = self.call('GET', path)
                if status == 200:
                    return
                last = f'HTTP {status}'
            except OSError as e:
                last = str(e)
            time.sleep(0.25)
        raise Failed(f'{path} did not answer 200 within {seconds}s ({last})')


def dig(value, path):
    for part in str(path).split('.') if path != '' else []:
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError(part)
    return value


def _fill(value, names):
    if isinstance(value, str):
        def one(m):
            key = m.group(1)
            if key.startswith('uuid:'):
                names.setdefault(key, str(uuid.uuid4()))
            if key not in names:
                raise Failed(f'{{{{{key}}}}} is not defined yet')
            return str(names[key])
        whole = re.fullmatch(r'\{\{([\w:.-]+)\}\}', value)
        if whole and not whole.group(1).startswith('uuid:') and whole.group(1) in names:
            return names[whole.group(1)]  # keep numbers numbers
        return re.sub(r'\{\{([\w:.-]+)\}\}', one, value)
    if isinstance(value, list):
        return [_fill(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, names) for k, v in value.items()}
    return value


def list_round_trip(mcp=True):
    """The built-in round trip for list-kind apps (web/list.json)."""
    steps = [
        {'do': 'POST /api/items/change', 'name': 'create',
         'body': {'operation_id': '{{uuid:op1}}', 'id': '{{uuid:item}}', 'create': True, 'title': 'Verify {{run}}'}},
        {'do': 'POST /api/items/change', 'name': 'replay is not duplicated',
         'body': {'operation_id': '{{uuid:op1}}', 'id': '{{uuid:item}}', 'create': True, 'title': 'Verify {{run}}'}},
        {'do': 'GET /api/items', 'name': 'list', 'expect_item': {'id': '{{uuid:item}}', 'title': 'Verify {{run}}', 'completed': False}},
        {'do': 'POST /api/items/change', 'name': 'update',
         'body': {'operation_id': '{{uuid:op2}}', 'id': '{{uuid:item}}', 'title': 'Verified {{run}}', 'completed': True}},
        {'restart': True},
        {'do': 'GET /api/items', 'name': 'read back after restart',
         'expect_item': {'id': '{{uuid:item}}', 'title': 'Verified {{run}}', 'completed': True}, 'expect_count': 1},
    ]
    return steps


def round_trip(source, doc):
    spec = source / 'verify.json'
    if spec.is_file():
        steps = json.loads(spec.read_text()).get('round_trip')
        if not isinstance(steps, list) or not steps:
            raise Failed('verify.json needs a non-empty "round_trip" list')
        return 'verify.json', steps
    if (source / 'web' / 'list.json').is_file():
        return 'list kind (built in)', list_round_trip()
    raise Failed('No API round trip declared. Start from a --kind template, or add verify.json (see `wam verify --help`).')


def run_steps(steps, client, restart):
    names, done = {'run': uuid.uuid4().hex[:8]}, []
    if not any(s.get('restart') for s in steps):
        raise Failed('The round trip must restart the container once ({"restart": true}) and read back after it.')
    for i, step in enumerate(steps, 1):
        if step.get('restart'):
            restart()
            done.append('restart')
            continue
        method, _, path = str(step.get('do', '')).partition(' ')
        label = step.get('name') or step.get('do')
        status, body = client.call(method.upper(), _fill(path, names), _fill(step['body'], names) if 'body' in step else None)
        want = step.get('status', 200 if method.upper() == 'GET' else (200, 201))
        if status not in (want if isinstance(want, (list, tuple)) else (want,)):
            raise Failed(f'step {i} ({label}): HTTP {status}, wanted {want}: {str(body)[:200]}')
        try:
            for key, expected in (step.get('expect') or {}).items():
                got = dig(body, key)
                if got != _fill(expected, names):
                    raise Failed(f'step {i} ({label}): {key} is {got!r}, wanted {_fill(expected, names)!r}')
            if 'expect_item' in step:
                want_item = _fill(step['expect_item'], names)
                rows = [r for r in dig(body, step.get('items', 'items')) if r.get('id') == want_item.get('id')]
                if len(rows) != step.get('expect_count', len(rows) or 1):
                    raise Failed(f'step {i} ({label}): {len(rows)} copies of the item, wanted {step.get("expect_count", 1)}')
                if not rows or any(rows[0].get(k) != v for k, v in want_item.items()):
                    raise Failed(f'step {i} ({label}): wanted {want_item}, got {rows[0] if rows else "nothing"}')
            for name, key in (step.get('save') or {}).items():
                names[name] = dig(body, key)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise Failed(f'step {i} ({label}): response has no {e} ({str(body)[:200]})') from None
        done.append(label)
    return done


def mcp_tools(client):
    status, body = client.call('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
    tools = (body or {}).get('result', {}).get('tools') if isinstance(body, dict) else None
    if status != 200 or not tools:
        raise Failed(f'MCP tools/list answered HTTP {status} with no tools')
    return len(tools)


def find_chrome():
    if os.environ.get('WAM_CHROMIUM'):
        return os.environ['WAM_CHROMIUM']
    for name in CHROMES:
        if shutil.which(name):
            return shutil.which(name)
    cache = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or str(Path.home() / '.cache/ms-playwright')
    for pattern in ('chromium_headless_shell-*/chrome-*/chrome-headless-shell', 'chromium_headless_shell-*/chrome-*/headless_shell',
                    'chromium-*/chrome-linux*/chrome'):
        found = sorted(glob.glob(os.path.join(cache, pattern)), reverse=True)
        if found:
            return found[0]
    return None


def skins(source):
    tokens = source / 'web' / 'wizard-tokens.css'
    names = re.findall(r'html\[data-skin="([\w-]+)"\]', tokens.read_text()) if tokens.is_file() else []
    return list(dict.fromkeys(names)) or ['default']


class _SkinProxy(ThreadingHTTPServer):
    """GET /s/<skin>/<path> -> the app's /<path>; the page's <html> gets data-skin=<skin>, as in the shell. Loopback only."""
    daemon_threads = True

    def __init__(self, app_port):
        self.app_port = app_port
        super().__init__(('127.0.0.1', 0), _SkinHandler)


class _SkinHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        m = re.match(r'^/s/([\w-]+)(/.*)$', self.path)
        if not m:
            self.send_response(404); self.end_headers(); return
        skin, rest = m.groups()
        c = http.client.HTTPConnection('127.0.0.1', self.server.app_port, timeout=10)
        try:
            c.request('GET', rest, headers={'Host': f'127.0.0.1:{self.server.app_port}'})
            r = c.getresponse()
            body = r.read()
            kind = r.getheader('Content-Type', 'application/octet-stream')
            if kind.startswith('text/html'):
                # What the shell's appearance message sets: the skin and its (dark) colour scheme.
                body = re.sub(rb'<html\b', b'<html data-skin="' + skin.encode() + b'" data-color-scheme="dark"', body, count=1)
            self.send_response(r.status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            c.close()

    def log_message(self, *a):
        pass


def screenshots(source, app_port, out):
    chrome = find_chrome()
    if not chrome:
        return [], 'skipped: no headless Chromium found (set WAM_CHROMIUM or install chromium); API and checks still ran'
    out.mkdir(parents=True, exist_ok=True)
    proxy = _SkinProxy(app_port)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    shots = []
    try:
        for skin in skins(source):
            png = out / f'{skin}-phone.png'
            png.unlink(missing_ok=True)
            with tempfile.TemporaryDirectory(prefix='wam-verify-chrome-') as profile:
                argv = [chrome, '--headless=new' if not re.search(r'headless', Path(chrome).name) else '--headless',
                        '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--hide-scrollbars', '--no-first-run',
                        f'--user-data-dir={profile}', f'--window-size={PHONE[0]},{PHONE[1]}', '--virtual-time-budget=4000',
                        f'--screenshot={png}', f'http://127.0.0.1:{proxy.server_address[1]}/s/{skin}/']
                subprocess.run(argv, capture_output=True, timeout=60)
            if not png.is_file() or png.stat().st_size < 1000:
                raise Failed(f'screenshot for skin {skin} was not written by {Path(chrome).name}')
            shots.append(str(png))
    finally:
        proxy.shutdown()
        proxy.server_close()
    return shots, f'{len(shots)} phone-width screenshots ({PHONE[0]}x{PHONE[1]}); look at them before handing over'


def _container_mem(cid):
    try:
        return _run(['docker', 'stats', '--no-stream', '--format', '{{.MemUsage}}', cid], timeout=30).split('/')[0].strip()
    except (Failed, subprocess.TimeoutExpired):
        return 'unknown'


def verify(source, out=None, keep=False, screenshot=True):
    source = Path(source).resolve()
    started = time.monotonic()
    receipt = {'source': str(source), 'ok': False, 'steps': []}
    say = lambda line: (receipt['steps'].append(line), print('  ' + line, flush=True))
    if not (source / 'wizard.md').is_file():
        raise Failed(f'{source} has no wizard.md')
    doc = wizardmd.front_matter((source / 'wizard.md').read_text()) or {}
    app_id = str(doc.get('id') or source.name)
    dockerfile = source / 'app' / 'Dockerfile'
    if not dockerfile.is_file():
        raise Failed('app/Dockerfile is missing; start from a `wam new --kind` template')
    tag = f'wam-verify-{app_id}:{uuid.uuid4().hex[:8]}'
    volume, cid = f'wam-verify-{app_id}-{uuid.uuid4().hex[:8]}', None
    out = Path(out) if out else Path(os.environ.get('WIZARD_OS_HOME', Path.home() / '.wizard-os')) / 'verify' / app_id
    print(f'wam verify {app_id}', flush=True)
    try:
        from .builder import check_app
        report = check_app(source, proposal=True)
        bad = [r for r in report['rules'] if not r['ok'] and r['severity'] != 'warning']
        if bad:
            raise Failed('wam check failed: ' + '; '.join(f"{r['rule']}: {r['detail'][:160]}" for r in bad[:4]))
        say(f"check: passed ({len(report['warnings'])} warnings)")
        build = source / doc.get('view', {}).get('source', {}).get('build', '').replace('python3 ', '', 1) \
            if str(doc.get('view', {}).get('source', {}).get('build', '')).startswith('python3 ') else None
        if build and build.is_file():
            _run([sys.executable, str(build)], cwd=source, timeout=60)
            say('view build: ok')
        t = time.monotonic()
        _run(['docker', 'build', '-q', '-f', str(dockerfile), '-t', tag, str(source)], timeout=600)
        say(f'image: built in {time.monotonic() - t:.1f}s')
        port = _free_port()
        hosts = f'127.0.0.1:{port},localhost:{port}'
        _run(['docker', 'volume', 'create', volume], timeout=60)
        cid = _run(['docker', 'run', '-d', '--memory', '256m', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                    '-p', f'127.0.0.1:{port}:8799', '-e', 'PORT=8799', '-e', 'DATA_DIR=/data',
                    '-e', f'WIZARD_TRUSTED_HOSTS={hosts}', '-e', f'WIZARD_TRUSTED_ORIGINS=http://127.0.0.1:{port}',
                    '-v', f'{volume}:/data', tag], timeout=120)
        client = Client(port)
        client.wait(doc.get('health') or '/healthz')
        say('started on loopback; health answered')

        def restart():
            _run(['docker', 'restart', '-t', '5', cid], timeout=120)
            client.wait(doc.get('health') or '/healthz')
        name, steps = round_trip(source, doc)
        done = run_steps(steps, client, restart)
        say(f'API round trip ({name}): ' + ' -> '.join(done))
        if doc.get('mcp'):
            say(f'MCP: tools/list returned {mcp_tools(client)} tools')
        if screenshot:
            shots, note = screenshots(source, port, out)
            receipt['screenshots'] = shots
            say('screenshots: ' + note + (f' in {out}' if shots else ''))
        receipt['container_memory'] = _container_mem(cid)
        receipt['ok'] = True
    except (Failed, OSError, ValueError, subprocess.TimeoutExpired) as e:
        receipt['error'] = str(e)
    finally:
        if cid and not keep:
            subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, timeout=60)
        if not keep:
            subprocess.run(['docker', 'volume', 'rm', '-f', volume], capture_output=True, timeout=60)
            subprocess.run(['docker', 'rmi', '-f', tag], capture_output=True, timeout=60)
        receipt['elapsed_s'] = round(time.monotonic() - started, 1)
        # Peak resident memory of this command and the tools it ran (Docker CLI, Chromium), in MiB.
        kib = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
        receipt['peak_rss_mib'] = round(kib / 1024, 1)
    return receipt


def main(argv=None):
    ap = argparse.ArgumentParser(prog='wam verify', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('source', type=Path)
    ap.add_argument('--out', type=Path, help='where screenshots go (default: ~/.wizard-os/verify/<id>/)')
    ap.add_argument('--no-screenshot', action='store_true', help='skip the phone screenshots')
    ap.add_argument('--keep', action='store_true', help='leave the container, volume and image for debugging')
    ap.add_argument('--json', action='store_true', help='print the receipt as JSON')
    a = ap.parse_args(argv)
    receipt = {'source': str(a.source), 'ok': False}
    try:
        receipt = verify(a.source, a.out, a.keep, not a.no_screenshot)
    except (Failed, OSError, ValueError, subprocess.TimeoutExpired) as e:
        receipt.update(ok=False, error=str(e))
    except KeyboardInterrupt:
        receipt.update(ok=False, error='interrupted')
    if a.json:
        print(json.dumps(receipt, indent=2))
    else:
        mem = f", container {receipt['container_memory']}" if receipt.get('container_memory') else ''
        tail = f" in {receipt.get('elapsed_s', '?')}s, peak {receipt.get('peak_rss_mib', '?')} MiB{mem}"
        print(('PASS' if receipt['ok'] else 'FAIL: ' + receipt.get('error', 'unknown error')) + tail)
        if receipt['ok']:
            print('Next: install it (wam install-local <source> --run) and hand it over; the owner using it is the final check.')
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
