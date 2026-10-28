"""Developer confirmation on the existing owner-authenticated Approve listener."""
import html
from . import cli, developer, owner


def respond(handler, method):
    if method == 'POST':
        try:
            body = cli.Handler._body(handler)
            action = body.pop('action', None)
            path = {'configure': '/api/developer', 'grant': '/api/developer/server-access',
                    'revoke': '/api/developer/server-access', 'apply': '/api/developer/apply'}.get(action)
            if not path: raise ValueError('unknown developer action')
            if action in ('grant', 'revoke'): body['action'] = action
            return handler._json(200, owner.execute(handler.cfg, path, body))
        except (OSError, ValueError, RuntimeError) as exc:
            return handler._json(400, {'error': str(exc)})
    doc = developer.status(handler.cfg.home)
    access = doc['server_access']
    esc = lambda text: html.escape(str(text), quote=True)
    disabled = '' if doc['enabled'] else ' disabled'
    data = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Developer mode</title><link rel="stylesheet" href="approve.css">
<main><h1>Developer mode</h1><p>{esc(doc.get('banner') or 'Developer mode is off. This box follows its release source.')}</p>
<p>Personal builds stop automatic release updates and stable guarantees. Only turn this on if you maintain your own build. Saving the source does not install it until you choose Install selected source.</p>
<form data-developer="configure"><input type="hidden" name="enabled" value="true">
<label>App Manager git repository <input name="repo" type="url" required value="{esc(doc['source']['repo'])}"></label>
<label>Branch or commit <input name="ref" required value="{esc(doc['source']['ref'])}"></label>
<button>Turn on developer mode / save source</button></form>
<form data-developer="apply"><button>Install selected source</button></form>
<form data-developer="configure"><input type="hidden" name="enabled" value="false"><button>Turn off and return to release</button></form>
<h2>Give the agent full server access</h2>
<p>This is root-equivalent access: the agent can change this entire server, including its protections. Every sudo use is logged. Access expires automatically after at most seven days. Turning developer mode off revokes it. The service layout stays unchanged.</p>
<p>{esc('Agent has full server access until ' + access['expires_at'] if access.get('active') else access.get('error') or access.get('reason') or 'Full server access is off.')}</p>
<form data-developer="grant"><label>Expires in seconds (maximum 604800) <input name="seconds" type="number" min="1" max="604800" value="604800" required{disabled}></label><button{disabled}>Give the agent full server access</button></form>
<form data-developer="revoke"><button>Revoke server access now</button></form>
<p id="result" role="status"></p></main><script src="approve.js" defer></script></html>'''.encode()
    handler.send_response(200)
    for key, value in {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(data)),
                       'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                       'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}.items():
        handler.send_header(key, value)
    handler.end_headers(); handler.wfile.write(data)
