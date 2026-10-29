"""Owner confirmation on a separate approval origin (spec/PRIVILEGE.md, owner approval boundary).

Why: the shell origin deliberately serves app routes and proxied agent responses. Any script running there sends
the owner's real Tailscale identity and an accepted Origin, so an owner-identity + Origin + custom-header check on
that origin proves which *browser* asked, never that the owner *meant it*. A same-origin token would not help:
that script can read it.

So risk-raising owner actions (server access, developer source, host access, data grants, backup import, network
policy off) never execute from the shell origin. They become an INTENT: server-stored, bound to the exact action
and body, owner login, five-minute expiry, single use. They execute only from a POST on the approval origin, a
dedicated `tailscale serve` HTTPS port whose whole site is this manager's approval listener (127.0.0.1:7792). No
app or agent content is ever served there. The POST must carry that origin exactly (`Origin` and
`Sec-Fetch-Site: same-origin`), the owner's identity from a root-owned tailscaled peer, and the intent's digest
shown on the confirmation page. Pages forbid framing, opener access and cross-origin reads.

Risk-reducing actions (revoke access, developer off, deny host access, deny/revoke a grant, isolation on, release
updates) stay one click on the shell origin so the owner can always back out quickly.

Fail closed: with no verified approval origin, risk-raising actions are refused with the exact setup command.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import time
from pathlib import Path

PORT = 7792
DEFAULT_HTTPS_PORT = 8446
TTL = 300
CONFIG = 'approval-origin.json'
INTENTS = 'owner-intents'
SETUP = 'wam approval-origin enable'
ID = re.compile(r'[A-Za-z0-9_-]{32,64}')
HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cross-Origin-Opener-Policy': 'same-origin',
    'Cross-Origin-Resource-Policy': 'same-origin',
    'Content-Security-Policy': ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
}


class NeedsConfirmation(Exception):
    def __init__(self, payload):
        super().__init__(payload.get('error', 'needs owner confirmation'))
        self.payload = payload


# ---------------------------------------------------------------- classification

def risk_reducing(path: str, body: dict) -> bool:
    """True only for actions that can never add authority to the agent, an app or the box."""
    if not isinstance(body, dict):
        return False
    if path == '/api/developer/server-access':
        return body.get('action') == 'revoke'
    if path == '/api/developer':
        return body.get('enabled') is False and set(body) <= {'enabled'}
    if path == '/api/network':
        return body.get('action') in ('deny', 'on')
    if path == '/api/grants':
        return body.get('action') == 'revoke'
    if path == '/api/updates/apply':
        return True  # signed releases on the owner's chosen channel; not a grant of authority
    if path == '/api/updates':
        return set(body) == {'mode'}  # a channel change picks different code: confirm it
    return False


def summary(path: str, body: dict) -> str:
    if path == '/api/developer/server-access':
        secs = body.get('seconds', 604800)
        return f'Give the agent FULL, root-equivalent access to this server for {secs} seconds.'
    if path == '/api/developer':
        if body.get('enabled'):
            return (f'Turn on developer mode and set the App Manager source to {body.get("repo")} '
                    f'at {body.get("ref")}. Release updates stop.')
        return 'Turn developer mode off.'
    if path == '/api/developer/apply':
        return 'Install the App Manager from the personal developer source now. It runs with manager authority.'
    if path == '/api/network':
        if body.get('action') == 'allow':
            return f'Allow {body.get("app")} the host access fingerprint {body.get("sha256")}.'
        return 'Turn app network isolation off.'
    if path == '/api/grants':
        return f'Let {body.get("app")} use the data of {body.get("to")}.'
    if path == '/api/backup/import':
        return f'Import and restore the backup file {body.get("file")} onto this box.'
    if path == 'host-approve':
        return f'Allow {body.get("app")} the host access fingerprint {body.get("sha256")}.'
    return f'{path} {json.dumps(body, sort_keys=True)}'


# ---------------------------------------------------------------- approval origin

def _serve_status():
    from . import links
    return links.serve_status()


def listener_owned(home, proc=Path('/proc/net/tcp')) -> bool:
    """A route string is not listener ownership. Require exactly one IPv4
    loopback LISTEN socket owned by the protected manager account, not the agent.
    Read the kernel table, never an HTTP response/header that another listener
    can forge. Lack of proc visibility or ambiguity is a refusal.
    """
    try:
        expected = Path(home).stat().st_uid
        rows = []
        for line in Path(proc).read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != '0A':
                continue
            addr, port = fields[1].split(':')
            if int(port, 16) == PORT:
                rows.append((addr, int(fields[7])))
        return rows == [('0100007F', expected)]
    except (OSError, ValueError, IndexError):
        return False


def configured(home) -> str | None:
    try:
        doc = json.loads((Path(home) / CONFIG).read_text())
    except (OSError, ValueError):
        return None
    origin = doc.get('origin') if isinstance(doc, dict) else None
    if not isinstance(origin, str) or not re.fullmatch(r'https://[a-z0-9.-]+:\d{2,5}', origin):
        return None
    return origin


def verified_origin(home, status=None) -> str | None:
    """The configured origin, only while its whole Serve site is the approval listener and nothing else."""
    origin = configured(home)
    if not origin or not listener_owned(home):
        return None
    try:
        web = (status if status is not None else _serve_status()).get('Web', {})
    except (OSError, ValueError):
        return None
    site = web.get(origin.removeprefix('https://')) or {}
    handlers = site.get('Handlers') or {}
    if set(handlers) != {'/'} or (handlers['/'] or {}).get('Proxy') != f'http://127.0.0.1:{PORT}':
        return None
    return origin


def enable(home, host: str, https_port: int = DEFAULT_HTTPS_PORT, run=None) -> dict:
    """Publish the dedicated approval site on a port no shell or app uses, then record its origin."""
    import subprocess
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30))
    if not listener_owned(home):
        raise ValueError('approval listener is absent or not manager-owned; start the protected manager service before publishing its route')
    authority = f'{host}:{https_port}'
    web = _serve_status().get('Web', {})
    if authority in web and verified_origin_for(web, authority) is None:
        raise ValueError(f'port {https_port} already serves something else; choose another with --port')
    if authority not in web:
        r = run(['tailscale', 'serve', '--bg', f'--https={https_port}', f'http://127.0.0.1:{PORT}'])
        if getattr(r, 'returncode', 1) != 0:
            raise ValueError(((r.stderr or r.stdout) or 'tailscale serve failed').strip()[:300])
    path = Path(home) / CONFIG
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({'origin': f'https://{authority}'}) + '\n')
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    origin = verified_origin(home)
    if not origin:
        path.unlink(missing_ok=True)
        run(['tailscale', 'serve', f'--https={https_port}', 'off'])
        raise ValueError('the approval site did not read back as the approval listener only')
    return {'origin': origin}


def verified_origin_for(web, authority):
    handlers = (web.get(authority) or {}).get('Handlers') or {}
    ok = set(handlers) == {'/'} and (handlers['/'] or {}).get('Proxy') == f'http://127.0.0.1:{PORT}'
    return f'https://{authority}' if ok else None


# ---------------------------------------------------------------- intents

def _dir(home) -> Path:
    d = Path(home) / INTENTS
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    (d / 'used').mkdir(mode=0o700, exist_ok=True)
    return d


def digest(kind: str, path: str, body: dict, login: str) -> str:
    canon = json.dumps({'kind': kind, 'path': path, 'body': body, 'login': login.lower()},
                       sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canon.encode()).hexdigest()


def create(home, kind: str, path: str, body: dict, login: str, now=None) -> dict:
    if not login:
        raise ValueError('owner login unknown')
    now = time.time() if now is None else now
    d = _dir(home)
    for old in d.glob('*.json'):  # bounded housekeeping
        try:
            if json.loads(old.read_text()).get('expires', 0) < now:
                old.unlink(missing_ok=True)
        except (OSError, ValueError):
            old.unlink(missing_ok=True)
    intent_id = secrets.token_urlsafe(32)
    doc = {'id': intent_id, 'kind': kind, 'path': path, 'body': body, 'login': login.lower(),
           'summary': summary(path, body), 'created': now, 'expires': now + TTL,
           'sha': digest(kind, path, body, login)}
    fd = os.open(d / f'{intent_id}.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(doc, f)
    return doc


def peek(home, intent_id: str, login: str, now=None) -> dict:
    if not ID.fullmatch(intent_id or ''):
        raise ValueError('unknown or expired confirmation')
    try:
        doc = json.loads((_dir(home) / f'{intent_id}.json').read_text())
    except (OSError, ValueError):
        raise ValueError('unknown, used or expired confirmation') from None
    now = time.time() if now is None else now
    if doc.get('expires', 0) < now or doc.get('login') != (login or '').lower():
        raise ValueError('unknown, used or expired confirmation')
    return doc


def consume(home, intent_id: str, sha: str, login: str, now=None) -> dict:
    """Single use: the atomic rename wins exactly once even under concurrent POSTs."""
    doc = peek(home, intent_id, login, now)
    if not secrets.compare_digest(str(sha), doc['sha']) or doc['sha'] != digest(doc['kind'], doc['path'], doc['body'], login):
        raise ValueError('this confirmation does not match the action shown')
    d = _dir(home)
    try:
        os.rename(d / f'{intent_id}.json', d / 'used' / f'{intent_id}.json')
    except FileNotFoundError:
        raise ValueError('unknown, used or expired confirmation') from None
    return doc


# ---------------------------------------------------------------- shell-origin gate

def gate(c, path: str, body: dict, login: str):
    """Risk-reducing: run now. Risk-raising: an intent plus the approval-origin URL, or a fail-closed refusal."""
    from . import owner
    if risk_reducing(path, body):
        return owner.execute(c, path, body)
    origin = verified_origin(c.home)
    if not origin:
        raise NeedsConfirmation({'error': 'approval_origin_missing',
                                 'message': 'This needs your confirmation on the separate owner approval address, '
                                            'which is not set up on this box.', 'setup': SETUP})
    intent = create(c.home, 'owner', path, body, login)
    raise NeedsConfirmation({'error': 'needs_owner_confirmation', 'summary': intent['summary'],
                             'confirm_url': f'{origin}/confirm/{intent["id"]}', 'expires_in': TTL})


# ---------------------------------------------------------------- approval-origin listener

def _send(handler, code: int, data: bytes, ctype: str):
    handler.send_response(code)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(len(data)))
    for k, v in HEADERS.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


def _json(handler, code, doc):
    _send(handler, code, json.dumps(doc).encode(), 'application/json')


def _page(handler, title, body_html, script='confirm.js'):
    data = (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><link rel="stylesheet" href="/approve.css">'
            f'<main>{body_html}<p id="result" role="status"></p></main>'
            f'<script src="/{script}" defer></script></html>').encode()
    _send(handler, 200, data, 'text/html; charset=utf-8')


def post_problem(headers, origin: str | None) -> str | None:
    """Exact approval origin, browser-attested same-origin fetch, JSON with the custom header."""
    if not origin:
        return 'approval address is not set up'
    if (headers.get('Origin') or '') != origin:
        return 'wrong origin'
    if (headers.get('Sec-Fetch-Site') or '') != 'same-origin':
        return 'cross-site request refused'
    if headers.get('X-Wizard-Store') != '1' or 'application/json' not in (headers.get('Content-Type') or ''):
        return 'bad request'
    return None


def respond(handler, method: str):
    """Requests on 127.0.0.1:7792 (the approval site). Identity first, then the exact-origin rule for POST."""
    from urllib.parse import urlparse
    from . import owner, store_install as si
    why = owner.check(handler)  # root-owned tailscaled peer + pinned owner login (no shell-origin list)
    if why:
        return _json(handler, 403, {'error': why})
    login = si.owner_login()
    home = handler.cfg.home
    origin = verified_origin(home)
    path = urlparse(handler.path).path
    if method == 'GET' and path in ('/approve.css', '/confirm.js', '/developer.js'):
        file = Path(__file__).parent / 'views' / path.lstrip('/')
        return _send(handler, 200, file.read_bytes(), 'text/css' if path.endswith('.css') else 'text/javascript')
    if not origin:
        return _json(handler, 403, {'error': f'approval address is not verified; run: {SETUP}'})
    body: dict = {}
    if method == 'POST':
        why = post_problem(handler.headers, origin)
        if why:
            return _json(handler, 403, {'error': why})
        try:
            size = int(handler.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('invalid body size')
            body = json.loads(handler.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('expected an object')
        except (ValueError, OSError) as exc:
            return _json(handler, 400, {'error': str(exc)[:200]})
    try:
        m = re.fullmatch(r'/confirm/([A-Za-z0-9_-]{32,64})', path)
        if m and method == 'GET':
            doc = peek(home, m.group(1), login)
            return _page(handler, 'Confirm owner action',
                         f'<h1>Confirm this change</h1><p class="summary">{html.escape(doc["summary"])}</p>'
                         f'<p>Only confirm if you started this just now. It expires in five minutes and works once.</p>'
                         f'<button id="confirm" type="button" data-intent="{doc["id"]}" data-sha="{doc["sha"]}">'
                         f'Confirm</button>')
        if m and method == 'POST':
            doc = consume(home, m.group(1), str(body.get('sha', '')), login)
            return _json(handler, 200, {'done': True, 'result': _execute(handler.cfg, doc)})
        hm = re.fullmatch(r'/approve/([a-z0-9][a-z0-9-]{0,62})/([a-f0-9]{64})', path)
        if hm and method == 'GET':
            from . import host_policy
            app, sha = hm.groups()
            row = host_policy.pending(home, app, sha)
            doc = create(home, 'host', 'host-approve', {'app': app, 'sha256': sha}, login)
            risks = ''.join('<li>' + html.escape(str(r)) + '</li>' for r in row['risks'])
            return _page(handler, 'Approve app access',
                         f'<h1>Allow {html.escape(app)} to access this server?</h1>'
                         f'<p>This app asks for access outside its own data and your files. It could affect other '
                         f'apps or the server.</p><ul>{risks}</ul><details><summary>Access fingerprint</summary>'
                         f'<code>{sha}</code></details><p>Approval covers the access listed above. Approving does '
                         f'not start the app; retry the refused action afterwards.</p>'
                         f'<button id="confirm" type="button" data-intent="{doc["id"]}" data-sha="{doc["sha"]}">'
                         f'Approve this access</button>')
        if path == '/developer' and method == 'GET':
            from . import developer_approval
            return _page(handler, 'Developer mode', developer_approval.body_html(home), script='developer.js')
        if path == '/intent' and method == 'POST':
            target, action = developer_route(body)
            doc = create(home, 'owner', target, action, login)
            return _json(handler, 200, {'confirm_url': f'{origin}/confirm/{doc["id"]}', 'summary': doc['summary']})
    except (ValueError, OSError, KeyError) as exc:
        return _json(handler, 409, {'error': str(exc)[:300]})
    return _json(handler, 404, {'error': 'not found'})


def developer_route(body: dict):
    body = dict(body)
    action = body.pop('action', None)
    target = {'configure': '/api/developer', 'grant': '/api/developer/server-access',
              'revoke': '/api/developer/server-access', 'apply': '/api/developer/apply'}.get(action)
    if not target:
        raise ValueError('unknown developer action')
    if action in ('grant', 'revoke'):
        body['action'] = action
    return target, body


def _execute(c, doc):
    from . import host_policy, owner
    if doc['kind'] == 'host':
        return host_policy.approve(c.home, doc['body']['app'], doc['body']['sha256'])
    if doc['kind'] == 'owner':
        from . import owner as _o
        if doc['path'] not in _o.PATHS:
            raise ValueError('unknown owner action')
        return owner.execute(c, doc['path'], doc['body'])
    raise ValueError('unknown confirmation kind')
