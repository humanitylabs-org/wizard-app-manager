"""Unprivileged, stdlib-only wam transport. Never falls back to local Docker."""
from __future__ import annotations
import argparse
import base64
import stat
import subprocess
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


def source_bundle(source):
    """Snapshot at the agent uid; the server receives bytes, never a host path."""
    rows, total = [], 0
    flags = os.O_RDONLY | os.O_NOFOLLOW
    def walk(fd, prefix=''):
        nonlocal total
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError('source directory must belong to this account')
        for name in sorted(os.listdir(fd)):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise ValueError('source symlink refused: ' + prefix + name)
            if name in ('.git', '__pycache__', '.pytest_cache', '.data', '.builder.json'):
                continue
            if st.st_uid != os.getuid():
                raise ValueError('source must belong to this account')
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(name, flags | os.O_DIRECTORY, dir_fd=fd)
                try: walk(sub, prefix + name + '/')
                finally: os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                f = os.open(name, flags | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(f, 'rb') as stream:
                    now = os.fstat(stream.fileno())
                    if not stat.S_ISREG(now.st_mode) or now.st_uid != os.getuid():
                        raise ValueError('source changed during capture')
                    data = stream.read(10 * 1024 * 1024 + 1)
                total += len(data)
                if len(data) > 10 * 1024 * 1024 or total > 25 * 1024 * 1024 or len(rows) >= 4096:
                    raise ValueError('source exceeds 25 MiB / 4096 files / 10 MiB per file')
                rows.append({'path': prefix + name, 'data': base64.b64encode(data).decode('ascii')})
            else:
                raise ValueError('source accepts regular files only')
    fd = os.open(source, flags | os.O_DIRECTORY)
    try: walk(fd)
    finally: os.close(fd)
    return rows


def scoped_command(argv):
    p = argparse.ArgumentParser(prog='wam ' + argv[0])
    if argv[0] == 'backup':
        sub = p.add_subparsers(dest='action', required=True)
        for verb in ('run', 'snapshots', 'undo'):
            sub.add_parser(verb).add_argument('--json', action='store_true')
        imp = sub.add_parser('import')
        imp.add_argument('file', nargs='?')
        imp.add_argument('--from-repo', nargs='?', const='latest')
        imp.add_argument('--yes', action='store_true'); imp.add_argument('--json', action='store_true')
        a = p.parse_args(argv[1:])
        if a.action == 'import':
            if a.file or not a.from_repo:
                p.error('importing a backup FILE is owner-only; use --from-repo for this box')
            return request('POST', '/api/backup/restore', {'snapshot': a.from_repo})
        return request('POST', '/api/backup/' + a.action, {})
    if argv[0] == 'links':
        p.add_argument('action', choices=['publish', 'unpublish'])
        p.add_argument('app'); p.add_argument('--json', action='store_true')
        a = p.parse_args(argv[1:])
        return request('POST', '/api/links/' + a.action, {'app': a.app})
    p.add_argument('action', choices=['install-local', 'update-local', 'check'])
    p.add_argument('source'); p.add_argument('--stage-only', action='store_true')
    p.add_argument('--run', action='store_true'); p.add_argument('--port', type=int, default=18799)
    p.add_argument('--trust-origin', action='append', default=[]); p.add_argument('--json', action='store_true')
    a = p.parse_args(argv[1:])
    return request('POST', '/api/builder/' + a.action,
                   {'files': source_bundle(a.source), 'run': a.run, 'port': a.port, 'trusted_origins': a.trust_origin})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'hermes':
        # This operation belongs to the unprivileged host account, not wam serve.
        if argv[1:] == ['update']:
            os.execvp('hermes', ['hermes', 'update'])
        if argv[1:] in (['update', '--check'], ['update', '--dry-run']):
            # Stock --version reports installed version/update status without
            # fetching, installing or restarting. Never proxy Hermes via the API.
            return subprocess.run(['hermes', '--version'], check=False).returncode
        print('Use wam hermes update [--check|--dry-run] for this account.', file=sys.stderr)
        return 2
    if argv[:1] == ['app']:
        argv = argv[1:]
    if argv[:1] and argv[0] in ('install-local', 'update-local', 'check'):
        argv.insert(0, 'builder')
    if argv[:1] and argv[0] in ('backup', 'links', 'builder'):
        try:
            result = scoped_command(argv)
            print(json.dumps(result, indent=2))
            return 1 if isinstance(result, dict) and (result.get('error') or result.get('ok') is False) else 0
        except SystemExit as exc:
            return int(exc.code or 0)
        except (OSError, ValueError, URLError) as exc:
            print('wam client: ' + str(exc), file=sys.stderr)
            return 1
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
