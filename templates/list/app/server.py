"""A Wizard list app: one stdlib service layer behind a documented HTTP API, MCP and a vanilla view.

Nouns, group names and extra item fields come from web/list.json: change the words there, not the code.
The authenticated Wizard gateway owns identity. Direct ports stay loopback-only.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-list'
TOOL = APP_ID.replace('-', '_')
VERSION = '0.1.0'
DEFAULT_GROUP = '00000000-0000-4000-8000-000000000001'
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.svg': 'image/svg+xml', '.json': 'application/json'}
CONFIG = json.loads((ROOT / 'web' / 'list.json').read_text())
ITEM, MANY, GROUP = CONFIG['item']['one'], CONFIG['item']['many'], CONFIG['group']['one']
FIELDS = {f['name']: f for f in CONFIG.get('fields', [])}
TITLE_MAX = int(CONFIG.get('title_max', 240))


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / 'list.sqlite3', timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def initialize():
    c = connect()
    try:
        c.execute('PRAGMA journal_mode=WAL')
        with c:
            c.execute('CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
            c.execute('INSERT OR IGNORE INTO meta VALUES (1,0)')
            c.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, title TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}', "
                      "completed INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
                      "updated_at TEXT NOT NULL, version INTEGER NOT NULL)")
            c.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request TEXT NOT NULL, response TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1)')
            c.execute('INSERT OR IGNORE INTO groups VALUES (?,?,0,1)', (DEFAULT_GROUP, CONFIG['group'].get('default', CONFIG['title'])))
            c.execute('CREATE TABLE IF NOT EXISTS placement (item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE, '
                      'group_id TEXT NOT NULL REFERENCES groups(id), position INTEGER NOT NULL)')
    finally:
        c.close()


def snapshot(c, include_deleted=True):
    groups = [dict(r) for r in c.execute('SELECT * FROM groups ORDER BY position,id')]
    rows = c.execute('SELECT i.*, p.group_id AS g, p.position AS pos FROM items i LEFT JOIN placement p ON p.item_id=i.id '
                     'ORDER BY p.position IS NULL, p.position, i.created_at, i.id').fetchall()
    items = [{'id': r['id'], 'title': r['title'], 'fields': json.loads(r['fields'] or '{}'), 'completed': bool(r['completed']),
              'deleted': bool(r['deleted']), 'group_id': r['g'] or DEFAULT_GROUP, 'position': r['pos'] or 0, 'version': r['version'],
              'created_at': r['created_at'], 'updated_at': r['updated_at']} for r in rows if include_deleted or not r['deleted']]
    return {'items': items, 'groups': groups, 'revision': c.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]}


def list_items(args=None):
    args = {} if args is None else args
    if not isinstance(args, dict) or set(args) - {'filter', 'search', 'include_deleted'}:
        raise Problem(400, 'Expected an object with optional filter, search, include_deleted.')
    mode, query = args.get('filter', 'all'), args.get('search', '')
    if mode not in ('all', 'active', 'completed') or not isinstance(query, str) or len(query) > 240:
        raise Problem(400, 'Use filter all, active or completed and a search of at most 240 characters.')
    if not isinstance(args.get('include_deleted', False), bool):
        raise Problem(400, 'include_deleted must be true or false.')
    c = connect()
    try:
        out = snapshot(c, args.get('include_deleted', False))
    finally:
        c.close()
    out['items'] = [t for t in out['items'] if (mode == 'all' or t['completed'] == (mode == 'completed'))
                    and query.casefold() in t['title'].casefold()]
    return out


def valid_id(value, name):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise Problem(400, name + ' must be a lowercase UUID.') from None
    return value


def valid_text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or any(ord(x) < 32 for x in value):
        raise Problem(400, f'Use a {name} of 1-{limit} characters without control characters.')
    return value.strip()


def valid_fields(value):
    """Configured extra fields only; '' or null clears one."""
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise Problem(400, 'fields may contain only: ' + (', '.join(FIELDS) or 'nothing (none are configured)') + '.')
    out = {}
    for name, v in value.items():
        spec, label = FIELDS[name], FIELDS[name].get('label', name)
        if v is None or v == '':
            out[name] = None
        elif spec.get('type') == 'number':
            if type(v) not in (int, float):
                raise Problem(400, label + ' must be a number.')
            out[name] = v
        elif spec.get('type') == 'date':
            if not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
                raise Problem(400, label + ' must be a date (YYYY-MM-DD).')
            out[name] = v
        else:
            out[name] = valid_text(v, label.lower(), int(spec.get('max', 240)))
    return out


def valid_common(args, allowed, entity):
    if not isinstance(args, dict) or set(args) - allowed:
        raise Problem(400, f'Unknown {entity} fields. Allowed: ' + ', '.join(sorted(allowed)) + '.')
    for flag in ('create', 'completed', 'deleted'):
        if flag in args and not isinstance(args[flag], bool):
            raise Problem(400, flag + ' must be true or false.')
    if 'expected_version' in args and (type(args['expected_version']) is not int or args['expected_version'] < 1):
        raise Problem(400, 'expected_version must be a positive integer.')
    for key in ('group_id', 'after_id'):
        if args.get(key) is not None:
            valid_id(args[key], key)
    return valid_id(args.get('operation_id'), 'operation_id'), valid_id(args.get('id'), 'id')


def place(c, kind, ident, after, group=None):
    """Dense integer order inside one write transaction; after=None appends."""
    if kind == 'item':
        ids = [r[0] for r in c.execute('SELECT item_id FROM placement WHERE group_id=? AND item_id!=? ORDER BY position,item_id', (group, ident))]
    else:
        ids = [r[0] for r in c.execute('SELECT id FROM groups WHERE id!=? ORDER BY position,id', (ident,))]
    if after is not None and after not in ids:
        raise Problem(409, 'The row to insert below has moved. Refresh and try again.')
    ids.insert(ids.index(after) + 1 if after is not None else len(ids), ident)
    for position, key in enumerate(ids):
        if kind == 'item':
            c.execute('INSERT INTO placement VALUES (?,?,?) ON CONFLICT(item_id) DO UPDATE SET group_id=excluded.group_id, '
                      'position=excluded.position', (key, group, position))
        else:
            c.execute('UPDATE groups SET position=? WHERE id=?', (position, key))


def once(op, request, write):
    """Run one write transaction once per operation_id; an exact replay returns the first result."""
    c = connect()
    try:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT request,response FROM operations WHERE id=?', (op,)).fetchone()
        if old:
            if old['request'] != request:
                raise Problem(409, 'That operation_id was already used for a different change.')
            return json.loads(old['response'])
        result = write(c)
        c.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
        result['revision'] = c.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
        c.execute('INSERT INTO operations VALUES (?,?,?)', (op, request, json.dumps(result)))
        c.commit()
        return result
    finally:
        c.close()  # an uncommitted transaction rolls back


def group_change(args):
    op, ident = valid_common(args, {'operation_id', 'id', 'name', 'create', 'after_id', 'expected_version'}, GROUP)
    name = valid_text(args.get('name'), GROUP + ' name', 80)

    def write(c):
        row = c.execute('SELECT * FROM groups WHERE id=?', (ident,)).fetchone()
        if args.get('create'):
            if row:
                raise Problem(409, f'That {GROUP} already exists.')
            c.execute('INSERT INTO groups VALUES (?,?,0,1)', (ident, name))
        else:
            if not row:
                raise Problem(404, GROUP.capitalize() + ' not found.')
            if args.get('expected_version', row['version']) != row['version']:
                raise Problem(409, f'This {GROUP} changed elsewhere. Refresh before editing it.')
            c.execute('UPDATE groups SET name=?,version=version+1 WHERE id=?', (name, ident))
        if args.get('create') or 'after_id' in args:
            place(c, 'group', ident, args.get('after_id'))
        return {'group': dict(c.execute('SELECT * FROM groups WHERE id=?', (ident,)).fetchone())}
    return once(op, json.dumps({'entity': 'group', **args}, sort_keys=True), write)


ITEM_KEYS = {'operation_id', 'id', 'title', 'fields', 'completed', 'deleted', 'create', 'expected_version', 'group_id', 'after_id'}


def item_change(args):
    op, ident = valid_common(args, ITEM_KEYS, ITEM)
    title = valid_text(args['title'], 'title', TITLE_MAX) if 'title' in args else None
    fields = valid_fields(args['fields']) if 'fields' in args else None
    if not set(args) & {'title', 'fields', 'completed', 'deleted', 'group_id', 'after_id'}:
        raise Problem(400, 'Supply a title, fields, completed, deleted, group_id or after_id.')

    def write(c):
        row = c.execute('SELECT * FROM items WHERE id=?', (ident,)).fetchone()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        group = args.get('group_id')
        if group and not c.execute('SELECT 1 FROM groups WHERE id=?', (group,)).fetchone():
            raise Problem(404, GROUP.capitalize() + ' not found.')
        if args.get('create'):
            if row:
                raise Problem(409, f'That {ITEM} already exists.')
            if title is None:
                raise Problem(400, f'A new {ITEM} needs a title.')
            kept = {k: v for k, v in (fields or {}).items() if v is not None}
            c.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,1)', (ident, title, json.dumps(kept), int(args.get('completed', False)),
                                                                     int(args.get('deleted', False)), now, now))
        else:
            if not row:
                raise Problem(404, ITEM.capitalize() + ' not found.')
            if args.get('expected_version', row['version']) != row['version']:
                raise Problem(409, f'This {ITEM} changed elsewhere. Refresh before editing it.')
            # Explicit state, never a toggle: a retry through API or MCP is safe.
            changes = {k: int(args[k]) for k in ('completed', 'deleted') if k in args}
            if title is not None:
                changes['title'] = title
            if fields is not None:
                merged = {**json.loads(row['fields'] or '{}'), **fields}
                changes['fields'] = json.dumps({k: v for k, v in merged.items() if v is not None})
            c.execute('UPDATE items SET ' + ''.join(k + '=?,' for k in changes) + 'updated_at=?, version=version+1 WHERE id=?',
                      (*changes.values(), now, ident))
        if args.get('create') or 'after_id' in args or group:
            previous = c.execute('SELECT group_id FROM placement WHERE item_id=?', (ident,)).fetchone()
            place(c, 'item', ident, args.get('after_id'), group or (previous[0] if previous else DEFAULT_GROUP))
        return {'item': next(t for t in snapshot(c)['items'] if t['id'] == ident)}
    return once(op, json.dumps(args, sort_keys=True), write)


UUID = {'type': 'string', 'format': 'uuid'}
FIELD_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    n: {'type': ['number' if f.get('type') == 'number' else 'string', 'null'], 'description': f.get('label', n)} for n, f in FIELDS.items()}}
ITEM_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['operation_id', 'id'], 'properties': {
    'operation_id': UUID, 'id': UUID, 'create': {'type': 'boolean'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': TITLE_MAX},
    'fields': FIELD_SCHEMA, 'group_id': UUID, 'after_id': {'type': ['string', 'null'], 'format': 'uuid'},
    'completed': {'type': 'boolean'}, 'deleted': {'type': 'boolean'}, 'expected_version': {'type': 'integer', 'minimum': 1}}}
LIST_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'filter': {'enum': ['all', 'active', 'completed']}, 'search': {'type': 'string', 'maxLength': 240}, 'include_deleted': {'type': 'boolean'}}}
GROUP_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['operation_id', 'id', 'name'], 'properties': {
    'operation_id': UUID, 'id': UUID, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 80}, 'create': {'type': 'boolean'},
    'after_id': {'type': ['string', 'null'], 'format': 'uuid'}, 'expected_version': {'type': 'integer', 'minimum': 1}}}
TOOLS = [
    {'name': TOOL + '_list', 'inputSchema': LIST_SCHEMA,
     'description': f'List saved {MANY} in order with their {CONFIG["group"]["many"]} and a revision. Optional filter and search.'},
    {'name': TOOL + '_change', 'inputSchema': ITEM_SCHEMA,
     'description': f'Add (create:true + title), edit title or fields, complete/reopen (completed true/false), delete/undo (deleted true/false) '
                    f'or move (group_id, after_id) one {ITEM}. Use fresh lowercase UUIDs for operation_id and a new id; retry the identical payload.'},
    {'name': TOOL + '_group_change', 'inputSchema': GROUP_SCHEMA,
     'description': f'Create (create:true) or rename a {GROUP}; after_id places it below another. Retry the identical payload.'},
]
CALLS = {TOOL + '_list': list_items, TOOL + '_change': item_change, TOOL + '_group_change': group_change}


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    if 'id' not in msg:
        return None
    method, rid, p = msg.get('method'), msg.get('id'), msg.get('params', {})
    if not isinstance(p, dict):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Expected params object.'}}
    try:
        if method == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            call = CALLS.get(p.get('name'))
            if call is None:
                raise Problem(400, 'Unknown tool.')
            value = call(p.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}
        else:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, sqlite3.Error, OSError) as e:
        text = e.message if isinstance(e, Problem) else 'Storage is unavailable. Nothing was discarded; retry this operation.'
        result = {'content': [{'type': 'text', 'text': text}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': rid, 'result': result}


def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f"127.0.0.1:{os.environ.get('PORT', '8799')}", f"localhost:{os.environ.get('PORT', '8799')}"}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        if (self.headers.get('Host') or '').lower() not in TRUSTED_HOSTS:
            self.send(403, {'error': 'Unknown host.'})
            return True
        origin = (self.headers.get('Origin') or '').lower()
        if write and ((origin and origin not in TRUSTED_ORIGINS) or (not origin and self.headers.get('Sec-Fetch-Site') == 'cross-site')):
            self.send(403, {'error': 'Cross-origin writes refused.'})
            return True
        return False

    def send(self, code, body=None, content_type='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        for k, v in {'Content-Type': content_type, 'Content-Length': str(len(b)), 'Cache-Control': 'no-store',
                     'X-Content-Type-Options': 'nosniff',
                     'Content-Security-Policy': "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def do_GET(self):
        if self.refused():
            return
        url = urlsplit(self.path)
        try:
            if url.path == '/healthz':
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
            if url.path == '/api/items':
                args = {k: v[0] for k, v in parse_qs(url.query).items()}
                if 'include_deleted' in args:
                    if args['include_deleted'] not in ('true', 'false'):
                        raise Problem(400, 'include_deleted must be true or false.')
                    args['include_deleted'] = args['include_deleted'] == 'true'
                return self.send(200, list_items(args))
            if url.path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if url.path == '/' else url.path[1:]
            kind = STATIC_TYPES.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if url.path == '/mcp' else 404, {'error': 'Use POST /mcp.' if url.path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (sqlite3.Error, OSError):
            self.send(503, {'error': f'Storage is unavailable. Your saved {MANY} have not been discarded. Try again.'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.refused(write=True):
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'Use application/json.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                return self.send(413, {'error': 'Request too large or empty.'})
            body = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == '/mcp':
                out = rpc(body)
                return self.send(202 if out is None else 200, out)
            if path == '/api/items/change':
                return self.send(200, item_change(body))
            if path == '/api/groups/change':
                return self.send(200, group_change(body))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError, AttributeError):
            self.send(400, {'error': 'Invalid JSON request.'})
        except (sqlite3.Error, OSError):
            self.send(503, {'error': f'Could not save. Your saved {MANY} are unchanged; retry this operation.'})

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    initialize()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(os.environ.get('PORT', '8799'))), Handler).serve_forever()
