"""Unprivileged, stdlib-only wam transport. Never falls back to local Docker."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def settings():
    home = Path(os.environ.get('WIZARD_OS_HOME', Path.home() / '.wizard-os'))
    doc = json.loads((home / 'client.json').read_text())
    url = doc['url'].rstrip('/')
    u = urlsplit(url)
    if u.scheme != 'http' or u.hostname != '127.0.0.1' or not u.port or u.path or u.query or u.fragment or u.username:
        raise ValueError('wam client needs a loopback HTTP origin')
    token = (home / 'agent-token').read_text().strip()
    if len(token) < 32:
        raise ValueError('invalid agent token')
    return url, token


def request(method, path, body=None):
    url, token = settings()
    req = Request(url + path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method=method)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=1500) as r:
            return json.load(r)
    except HTTPError as e:
        try:
            payload = json.load(e)
        except ValueError:
            payload = {'error': 'HTTP ' + str(e.code)}
        print(json.dumps(payload), file=sys.stderr)
        raise SystemExit(77 if e.code == 409 else 1) from None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'hermes':
        # This operation belongs to the unprivileged host account, not wam serve.
        if argv[1:] == ['update']:
            os.execvp('hermes', ['hermes', 'update'])
        print('Use hermes update or hermes doctor for this account.', file=sys.stderr)
        return 2
    if argv[:1] == ['app']:
        argv = argv[1:]
    supported = {'apps', 'installed', 'search', 'install', 'uninstall', 'update', 'start', 'stop', 'restart', 'logs', 'doctor'}
    if argv and argv[0] not in supported and argv[0] not in ('-h', '--help'):
        print(f'wam {argv[0]}: not yet available without server access; ask the owner, not sudo or Docker.', file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog='wam', description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('apps', 'installed', 'doctor'):
        sub.add_parser(name).add_argument('--json', action='store_true')
    s = sub.add_parser('search'); s.add_argument('query', nargs='?', default=''); s.add_argument('--json', action='store_true')
    for name in ('install', 'uninstall', 'update', 'start', 'stop', 'restart', 'logs'):
        s = sub.add_parser(name); s.add_argument('app'); s.add_argument('--json', action='store_true')
        if name == 'install':
            s.add_argument('--set', action='append', default=[])
        if name == 'logs':
            s.add_argument('--tail', type=int, default=100)
    args = p.parse_args(argv)
    try:
        if args.cmd in ('apps', 'installed'):
            result = request('GET', '/api/apps')
        elif args.cmd == 'search':
            result = request('GET', '/api/search?' + urlencode({'q': args.query}))
        elif args.cmd == 'doctor':
            result = request('GET', '/api/doctor')
        elif args.cmd == 'logs':
            result = request('GET', '/api/apps/' + quote(args.app, safe='') + '/logs?' + urlencode({'tail': args.tail}))
        elif args.cmd == 'uninstall':
            result = request('DELETE', '/api/apps/' + quote(args.app, safe=''))
        else:
            body = {'app': args.app}
            if args.cmd == 'install':
                body['set'] = {}
                for pair in args.set:
                    k, sep, v = pair.partition('=')
                    if not sep or not k:
                        p.error('--set needs KEY=VALUE')
                    body['set'][k] = v
            path = '/api/install' if args.cmd == 'install' else '/api/app/' + args.cmd
            result = request('POST', path, body)
        if args.cmd == 'doctor' and not args.json:
            for row in result:
                print(f"{'PASS' if row['ok'] else 'FAIL'}  {row['check']}  {row['detail']}")
        elif args.cmd == 'logs' and not args.json:
            print(result['logs'])
        else:
            print(json.dumps(result, indent=2))
        if args.cmd == 'doctor':
            return 0 if all(row['ok'] for row in result) else 1
        return 1 if isinstance(result, dict) and result.get('error') else 0
    except (OSError, ValueError, URLError) as e:
        print('wam client: ' + str(e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
