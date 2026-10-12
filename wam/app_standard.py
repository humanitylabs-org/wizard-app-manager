"""Opt-in checks for OS PR24; approved wizardmd.check remains unchanged.

Rule 14: missing API is warning in 0.2. Rules 15/16 validate declarations
only in proposal mode. 0.3 is still unknown to the approved checker.
"""
import re
from pathlib import Path
from urllib.parse import urlsplit


def _local(root: Path, value, directory=False):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return False
    p = (root / value).resolve()
    return p.is_relative_to(root.resolve()) and (p.is_dir() if directory else p.is_file())


def _port(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v < 65536


def _path(v):
    return isinstance(v, str) and v.startswith('/') and not v.startswith('//') and '?' not in v and '#' not in v


def _spec(root, value):
    if not isinstance(value, str):
        return False
    u = urlsplit(value)
    return (u.scheme in {'http', 'https'} and bool(u.hostname) and not u.username) or _local(root, value)


def check(root: Path, doc: dict) -> list[dict]:
    rows = []
    def rule(name, ok, detail='', warning=False):
        rows.append({'rule': name, 'ok': bool(ok), 'severity': 'warning' if warning else 'error', 'detail': detail})
    pure = doc.get('type') == 'view' and doc.get('backend') == 'none'
    mcp = doc.get('mcp')
    if not pure:
        rule('proposal 5: every backend declares MCP', isinstance(mcp, dict) and _port(mcp.get('port')) and _path(mcp.get('path')))
    api = doc.get('api')
    if mcp and not api:
        rule('proposal 14: documented API', False, 'Missing API; transition warning for wizard 0.2.', warning=float(doc.get('wizard', 0)) <= 0.2)
    elif api is not None:
        rule('proposal 14: API port/path/spec', isinstance(api, dict) and _port(api.get('port')) and _path(api.get('path')) and _spec(root, api.get('spec')))
    view = doc.get('view') or {}
    src = view.get('source') if isinstance(view, dict) else None
    if src is not None:
        ok = isinstance(src, dict) and all(isinstance(src.get(k), str) and src[k].strip() for k in ('path', 'build', 'dist'))
        if ok:
            for k in ('path', 'dist'):
                p = Path(src[k])
                ok = ok and not p.is_absolute() and '..' not in p.parts
            if src.get('repo'):
                # Immutable SHA or version tag; branches deliberately fail this conservative check.
                ref = str(src.get('ref', ''))
                ok = ok and bool(re.fullmatch(r'[\w.-]+/[\w.-]+', str(src['repo']))) and bool(re.fullmatch(r'(?:[0-9a-f]{40}|v?\d+\.\d+\.\d+(?:[-+.][\w.-]+)?)', ref))
            else:
                ok = ok and _local(root, src['path'], directory=True) and _local(root, src['dist'], directory=True)
        rule('proposal 15: view.source complete and confined', ok)
    if 'backend' in doc:
        rule('proposal 16: backend none only for pure view', pure and api is None and mcp is None)
    if 'replaces_view' in doc:
        rule('proposal: replaces_view is a view app id', doc.get('type') == 'view' and bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,40}', str(doc['replaces_view']))))
    return rows
