"""Owner approval UI on the manager's existing, strict tailscale Store listener."""
import html
import json
from pathlib import Path
from . import host_policy


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
    try:
        app, sha = tail.split('/')
        row = host_policy.pending(handler.cfg.home, app, sha)
        if method == 'POST':
            # StoreHandler has already enforced root peer, owner, Origin and the
            # custom header. Re-check pending bytes inside approve under its lock.
            return handler._json(200, host_policy.approve(handler.cfg.home, app, sha))
    except (ValueError, OSError) as exc:
        return handler._json(409, {'error': str(exc)})
    risks = ''.join('<li>' + html.escape(str(r)) + '</li>' for r in row['risks'])
    data = (f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve app access</title><link rel="stylesheet" href="../approve.css">
<main><h1>Allow {html.escape(app)} to access this server?</h1>
<p>This app asks for access outside its own data and your files. It could affect other apps or the server.</p>
<ul>{risks}</ul><details><summary>Access fingerprint</summary><code>{sha}</code></details>
<p>Approval covers the access listed above. Updates that keep or reduce it can continue; additional access needs your approval. Approving does not start the app; retry the refused action afterwards.</p>
<button id="approve" type="button">Approve this access</button><p id="result" role="status"></p></main>
<script src="../approve.js" defer></script></html>''').encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    handler.wfile.write(data)
