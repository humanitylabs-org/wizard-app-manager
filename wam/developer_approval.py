"""Developer mode page. Risk-raising changes run only on the separate approval origin (owner_confirm.py)."""
import html
from . import cli, developer, owner_confirm


def body_html(home) -> str:
    doc = developer.status(home)
    access = doc['server_access']
    esc = lambda text: html.escape(str(text), quote=True)
    disabled = '' if doc['enabled'] else ' disabled'
    return f'''<h1>Developer mode</h1><p>{esc(doc.get('banner') or 'Developer mode is off. This box follows its release source.')}</p>
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
<p>Each change opens a confirmation that names it exactly. Nothing changes until you confirm there.</p>'''


def respond(handler, method):
    """Shell-origin entry (/_approve/developer). Never executes a risk-raising change here."""
    from . import store_install as si
    if method == 'POST':
        try:
            target, body = owner_confirm.developer_route(cli.Handler._body(handler))
            return handler._json(200, owner_confirm.gate(handler.cfg, target, body, si.owner_login()))
        except owner_confirm.NeedsConfirmation as exc:
            return handler._json(409 if exc.payload['error'] == 'needs_owner_confirmation' else 403, exc.payload)
        except (OSError, ValueError, RuntimeError) as exc:
            return handler._json(400, {'error': str(exc)})
    origin = owner_confirm.verified_origin(handler.cfg.home)
    if origin:
        handler.send_response(303)
        handler.send_header('Location', f'{origin}/developer')
        handler.send_header('Content-Length', '0')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        return
    # Fail closed for risk-raising changes, but the owner can always back out here.
    data = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Developer mode</title>'
            '<link rel="stylesheet" href="approve.css"><main><h1>Developer mode</h1>'
            '<p>Turning developer mode on or giving server access needs the separate owner approval address, which '
            f'is not set up on this box. An administrator can run <code>{owner_confirm.SETUP}</code>.</p>'
            '<form data-developer="revoke"><button>Revoke server access now</button></form>'
            '<form data-developer="configure"><input type="hidden" name="enabled" value="false">'
            '<button>Turn off and return to release</button></form><p id="result" role="status"></p></main>'
            '<script src="approve.js" defer></script></html>').encode()
    handler.send_response(200)
    for key, value in {**owner_confirm.HEADERS, 'Content-Type': 'text/html; charset=utf-8',
                       'Content-Length': str(len(data))}.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)
