"""Hello Wizard reference: one API/service layer used by the view and MCP.

Stdlib only. The host gateway owns authentication; direct ports stay loopback.
"""
import datetime as dt
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'hello-wizard'
VERSION = '0.2.0'
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / 'habits.sqlite3', timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE)')
    c.execute('CREATE TABLE IF NOT EXISTS marks (habit_id INTEGER, day TEXT, PRIMARY KEY(habit_id,day))')
    return c


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def resolve_day(value=None):
    """The owner's local day. Callers send it (the view knows the device's date); the
    server accepts only a real ISO date within a day of UTC, so a habit marked at 6 am in
    Bangkok lands on that morning, not on yesterday's UTC date."""
    utc = dt.datetime.now(dt.timezone.utc).date()
    if value in (None, ''):
        return utc.isoformat()
    try:
        day = dt.date.fromisoformat(str(value))
    except ValueError:
        raise Problem(400, 'day must be YYYY-MM-DD.') from None
    if abs((day - utc).days) > 1:
        raise Problem(400, 'day must be today in your timezone.')
    return day.isoformat()


def list_habits(day=None):
    day = resolve_day(day)
    c = connect()
    try:
        rows = c.execute('SELECT h.id,h.name,EXISTS(SELECT 1 FROM marks m WHERE m.habit_id=h.id AND m.day=?) AS done FROM habits h ORDER BY h.id', (day,)).fetchall()
        return {'day': day, 'habits': [dict(r, done=bool(r['done'])) for r in rows]}
    finally:
        c.close()


def add_habit(args):
    day = resolve_day(args.get('day'))
    name = args.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
        raise Problem(400, 'Use a habit name between 1 and 60 characters.')
    c = connect()
    try:
        with c:
            c.execute('INSERT INTO habits(name) VALUES (?)', (name.strip(),))
    except sqlite3.IntegrityError:
        raise Problem(409, 'That habit already exists.') from None
    finally:
        c.close()
    return list_habits(day)


def set_done(habit_id, args):
    done = args.get('done')
    if not isinstance(done, bool):
        raise Problem(400, 'done must be true or false.')
    day = resolve_day(args.get('day'))
    c = connect()
    try:
        with c:
            if not c.execute('SELECT id FROM habits WHERE id=?', (habit_id,)).fetchone():
                raise Problem(404, 'Habit not found.')
            if done:
                c.execute('INSERT OR IGNORE INTO marks VALUES (?,?)', (habit_id, day))
            else:
                c.execute('DELETE FROM marks WHERE habit_id=? AND day=?', (habit_id, day))
    finally:
        c.close()
    return list_habits(day)


TOOLS = [
    {'name': 'wizard_time', 'description': 'Current UTC time on the Wizard box.', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'wizard_echo', 'description': 'Echo text to prove this app is reachable.', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
    {'name': 'habits_list', 'description': 'List saved habits and whether each is done on the given day.', 'inputSchema': {'type': 'object', 'properties': {'day': {'type': 'string', 'description': "Owner's local date, YYYY-MM-DD. Pass it: the box clock is UTC."}}}},
    {'name': 'habits_add', 'description': 'Add a habit by name (1–60 characters).', 'inputSchema': {'type': 'object', 'properties': {'name': {'type': 'string', 'minLength': 1, 'maxLength': 60}, 'day': {'type': 'string', 'description': "Owner's local date, YYYY-MM-DD. Pass it: the box clock is UTC."}}, 'required': ['name']}},
    {'name': 'habits_set_done', 'description': 'Mark or undo one habit for the given day. Idempotent.', 'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'integer'}, 'done': {'type': 'boolean'}, 'day': {'type': 'string', 'description': "Owner's local date, YYYY-MM-DD. Pass it: the box clock is UTC."}}, 'required': ['id', 'done']}},
]


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            a = p.get('arguments') or {}; n = p.get('name')
            if n == 'habits_list':
                value = list_habits(a.get('day'))
            elif n == 'habits_add':
                value = add_habit(a)
            elif n == 'habits_set_done':
                if not isinstance(a.get('id'), int) or isinstance(a['id'], bool):
                    raise Problem(400, 'id must be an integer.')
                value = set_done(a['id'], a)
            elif n == 'wizard_time':
                value = {'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            elif n == 'wizard_echo':
                value = {'text': str(a.get('text', ''))}
            else:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


# Who may talk to this app. Trust never comes from the request itself: the Host must be one this app
# was configured for (stops DNS rebinding), and a browser Origin on writes must be configured too.
# The owner's Wizard gateway, or the local staging helper, passes these in.
_LOOPBACK = {f"127.0.0.1:{os.environ.get('PORT', '8799')}", f"localhost:{os.environ.get('PORT', '8799')}"}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        if (self.headers.get('Host') or '').lower() not in TRUSTED_HOSTS:
            self.send(403, {'error': 'Unknown host.'})
            return True
        if write:
            origin = (self.headers.get('Origin') or '').lower()
            if origin and origin not in TRUSTED_ORIGINS:
                self.send(403, {'error': 'Cross-origin writes refused.'})
                return True
            if not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
                self.send(403, {'error': 'Cross-origin writes refused.'})
                return True
        return False

    def send(self, code, body=None, content_type='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def do_GET(self):
        if self.refused():
            return
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
        if path == '/api/habits':
            try:
                return self.send(200, list_habits(parse_qs(urlsplit(self.path).query).get('day', [None])[0]))
            except Problem as e:
                return self.send(e.code, {'error': e.message})
        if path == '/api/time':
            return self.send(200, {'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        # The view: any file directly in web/, by an allowlisted type. No subpaths, no dotfiles.
        name = 'index.html' if path == '/' else path[1:]
        kind = STATIC_TYPES.get(Path(name).suffix)
        if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
            return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
        if path == '/openapi.json':
            return self.send(200, (ROOT / 'openapi.json').read_bytes())
        self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.refused(write=True):
            return
        path = urlsplit(self.path).path
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'Use application/json.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 65536:
                return self.send(413, {'error': 'Request too large.'})
            body = json.loads(self.rfile.read(size))
            if path == '/mcp':
                if not isinstance(body, dict):
                    return self.send(400, {'error': 'Expected a JSON-RPC object.'})
                out = rpc(body)
                return self.send(202 if out is None else 200, out)
            if not isinstance(body, dict):
                return self.send(400, {'error': 'Expected a JSON object.'})
            if path == '/api/habits':
                return self.send(201, add_habit(body))
            if path.startswith('/api/habits/') and path.endswith('/today'):
                parts = path.split('/')
                if len(parts) != 5 or not parts[3].isdigit():
                    raise Problem(400, 'Invalid habit id.')
                return self.send(200, set_done(int(parts[3]), body))
            if path == '/api/echo':
                return self.send(200, {'text': str(body.get('text', ''))})
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError, AttributeError):
            self.send(400, {'error': 'Invalid JSON request.'})

    def log_message(self, format, *args):
        pass


# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app; `wam new` starts every app with it (wizard-os AGENTS.md: a new app repo ships it in its first PR). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or DATA) / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------


wizard_switches_install(Handler)


if __name__ == '__main__':
    connect().close()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(os.environ.get('PORT', '8799'))), Handler).serve_forever()
