"""Shell-origin approval entry points: static assets and redirects to the approval origin only."""
from pathlib import Path


def respond(handler, method, tail):
    if method == 'GET' and tail in {'approve.js', 'approve.css'}:
        file = Path(__file__).parent / 'views' / tail
        data = file.read_bytes()
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/javascript' if tail.endswith('.js') else 'text/css')
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers()
        handler.wfile.write(data)
        return
    if tail == 'developer':
        from . import developer_approval
        return developer_approval.respond(handler, method)
    # Host access is risk-raising: it is approved only on the separate approval
    # origin (owner_confirm.py). The shell origin redirects GETs and refuses POSTs.
    from . import owner_confirm
    import re
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}/[a-f0-9]{64}', tail):
        return handler._json(404, {'error': 'not found'})
    origin = owner_confirm.verified_origin(handler.cfg.home)
    if method == 'POST' or not origin:
        return handler._json(403, {'error': 'approval_origin_missing' if not origin else 'approve on the owner approval address',
                                   'setup': owner_confirm.SETUP,
                                   **({'confirm_url': f'{origin}/approve/{tail}'} if origin else {})})
    handler.send_response(303)
    handler.send_header('Location', f'{origin}/approve/{tail}')
    handler.send_header('Content-Length', '0')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
