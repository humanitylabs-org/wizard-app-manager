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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'hello-wizard'
VERSION = '0.2.0'


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / 'habits.sqlite3', timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE)')
    c.execute('CREATE TABLE IF NOT EXISTS marks (habit_id INTEGER, day TEXT, PRIMARY KEY(habit_id,day))')
    return c


def today():
    return dt.date.today().isoformat()


def list_habits():
    c = connect()
    try:
        rows = c.execute('SELECT h.id,h.name,EXISTS(SELECT 1 FROM marks m WHERE m.habit_id=h.id AND m.day=?) AS done FROM habits h ORDER BY h.id', (today(),)).fetchall()
        return {'day': today(), 'habits': [dict(r, done=bool(r['done'])) for r in rows]}
    finally:
        c.close()


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def add_habit(args):
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
    return list_habits()


def set_done(habit_id, args):
    done = args.get('done')
    if not isinstance(done, bool):
        raise Problem(400, 'done must be true or false.')
    c = connect()
    try:
        with c:
            if not c.execute('SELECT id FROM habits WHERE id=?', (habit_id,)).fetchone():
                raise Problem(404, 'Habit not found.')
            if done:
                c.execute('INSERT OR IGNORE INTO marks VALUES (?,?)', (habit_id, today()))
            else:
                c.execute('DELETE FROM marks WHERE habit_id=? AND day=?', (habit_id, today()))
    finally:
        c.close()
    return list_habits()


TOOLS = [
    {'name': 'wizard_time', 'description': 'Current UTC time on the Wizard box.', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'wizard_echo', 'description': 'Echo text to prove this app is reachable.', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
    {'name': 'habits_list', 'description': 'List saved habits and today’s completion state.', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'habits_add', 'description': 'Add a habit by name (1–60 characters).', 'inputSchema': {'type': 'object', 'properties': {'name': {'type': 'string', 'minLength': 1, 'maxLength': 60}}, 'required': ['name']}},
    {'name': 'habits_set_done', 'description': 'Mark or undo today for one habit. Idempotent.', 'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'integer'}, 'done': {'type': 'boolean'}}, 'required': ['id', 'done']}},
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
                value = list_habits()
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


class Handler(BaseHTTPRequestHandler):
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
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
        if path == '/api/habits':
            return self.send(200, list_habits())
        if path == '/api/time':
            return self.send(200, {'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        if path in {'/', '/index.html', '/app.js', '/styles.css', '/theme-client.js'}:
            name = 'index.html' if path == '/' else path[1:]
            ct = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'theme-client.js': 'text/javascript; charset=utf-8', 'styles.css': 'text/css; charset=utf-8'}[name]
            return self.send(200, (ROOT / 'web' / name).read_bytes(), ct)
        if path == '/openapi.json':
            return self.send(200, (ROOT / 'openapi.json').read_bytes())
        self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.send(403, {'error': 'Cross-origin writes refused.'})
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


if __name__ == '__main__':
    connect().close()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(os.environ.get('PORT', '8799'))), Handler).serve_forever()
