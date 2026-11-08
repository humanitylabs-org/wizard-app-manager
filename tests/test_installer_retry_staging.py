"""Bounded synthetic HTTP checks of exact production-generated shell commands.

RETRY_SOURCE_ROOT selects an immutable comparison tree; never source get.sh.
"""
import ast
import http.server
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

import pytest

ROOT = Path(os.environ.get('RETRY_SOURCE_ROOT', Path(__file__).resolve().parents[1]))
SIDE = os.environ.get('RETRY_SIDE', 'candidate')


def command(site, url):
    if site == 'as_user':
        text = (ROOT / 'get.sh').read_text()
        line = next(x.strip() for x in text.splitlines() if 'bash -c "' in x and 'curl -fsSL' in x)
        # Evaluate the actual outer shell quoting, with only its raw-URL helper stubbed.
        shell = 'REPO=' + url + '; REF=main; repo_raw() { printf %s "$REPO"; }; capture() { printf %s "$2"; }; capture' + line[len('bash'):]
        return subprocess.check_output(['bash', '-c', shell], text=True).replace(url + '/raw/main/get.sh', url)
    file = 'updates.py' if site == 'apply' else 'quick_update.py'
    tree = ast.parse((ROOT / 'wam' / file).read_text())
    expr = next(n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) and any(isinstance(v, ast.Constant) and isinstance(v.value, str) and 'curl -fsSL' in v.value for v in n.values))
    return eval(compile(ast.Expression(expr), str(ROOT / 'wam' / file), 'eval'), {'INSTALLER': url, 'env': 'WIZARD_HERMES_PROMOTION= WIZARD_RELEASE=0.3.8 '})


@pytest.mark.parametrize('site', ['as_user', 'apply', 'quick'])
@pytest.mark.parametrize('case', ['partial_timeout_then_success', 'http504_then200', 'always_fail', 'script_exit37', 'stdin_source_and_args'])
def test_staged_retry(tmp_path, site, case):
    requests = []
    # Long prefix makes the marker execute before the deliberately incomplete response stalls.
    body = b'#' + b'x' * 20000 + b'\nprintf "%s|%s|%s|%s\\n" "${BASH_SOURCE[0]-}" "$*" "${WIZARD_RELEASE-}" "${WIZARD_HERMES_PROMOTION-unset}" >> "$MARK"\n'
    if case == 'script_exit37':
        body += b'exit 37\n'

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            assert self.path == '/synthetic.sh'
            fail = case == 'always_fail' or (case == 'http504_then200' and len(requests) == 1)
            self.send_response(504 if fail else 200)
            partial = case == 'partial_timeout_then_success' and len(requests) == 1
            self.send_header('Content-Length', str(0 if fail else len(body) + (100 if partial else 0)))
            self.end_headers()
            if not fail:
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except BrokenPipeError:
                    pass
            if partial:
                time.sleep(.5)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.curlrc').write_text('max-time = 0.2\nnoproxy = "*"\n')
    temps = tmp_path / 'temps'
    temps.mkdir()
    sentinel = temps / 'unrelated'
    sentinel.write_text('keep')
    marker = tmp_path / 'marker'
    url = f'http://127.0.0.1:{server.server_port}/synthetic.sh'
    cmd = command(site, url)
    # All shell downloads go through real curl, but a shell function refuses any unexpected URL.
    guard = f'curl() {{ local x; for x in "$@"; do case "$x" in http*://*) [ "$x" = "{url}" ] || return 97;; esac; done; command curl "$@"; }}; '
    env = {**os.environ, 'HOME': str(home), 'CURL_HOME': str(home), 'TMPDIR': str(temps), 'MARK': str(marker), 'WIZARD_HERMES_PROMOTION': 'inherited'}
    try:
        p = subprocess.run(['bash', '-c', guard + cmd], env=env, capture_output=True, text=True, timeout=14)
    finally:
        server.shutdown()
        server.server_close()
    rows = marker.read_text().splitlines() if marker.exists() else []
    retries = SIDE != 'base'
    expected_requests = (5 if retries else 1) if case == 'always_fail' else ((2 if retries else 1) if case in ('partial_timeout_then_success', 'http504_then200') else 1)
    assert len(requests) == expected_requests
    if case == 'always_fail':
        assert rows == []
        # Base/author apply's inherited pipeline masks curl failure; candidate must propagate it.
        assert p.returncode == (0 if SIDE != 'candidate' and site == 'apply' else 22)
    elif case == 'http504_then200' and SIDE == 'base':
        assert rows == []
        assert p.returncode == (0 if site == 'apply' else 22)
    else:
        count = 2 if SIDE == 'author' and case == 'partial_timeout_then_success' else 1
        assert len(rows) == count
        for row in rows:
            assert row == ('|||inherited' if site == 'as_user' else '|start|0.3.8|')
        expected_rc = 37 if case == 'script_exit37' else (28 if SIDE == 'base' and case == 'partial_timeout_then_success' and site != 'apply' else 0)
        assert p.returncode == expected_rc
    assert sorted(x.name for x in temps.iterdir()) == ['unrelated']
    assert sentinel.read_text() == 'keep'
    print(json.dumps({'side': SIDE, 'site': site, 'case': case, 'requests': len(requests), 'executions': len(rows), 'exit': p.returncode, 'cleanup': True, 'command': cmd}))


@pytest.mark.parametrize('site', ['install_start', 'named_host'])
def test_kept_file_output_truncates_partial(tmp_path, site):
    text = (ROOT / 'get.sh').read_text()
    if site == 'install_start':
        line = next(x.strip() for x in text.splitlines() if 'else curl -fsSL' in x)
        cmd = line.removeprefix('else ').removesuffix('; fi')
        dest = tmp_path / 'get.sh.tmp'
    else:
        cmd = next(x.strip() for x in text.splitlines() if 'curl -fsSL' in x and '-o "$helper"' in x)
        dest = tmp_path / 'helper'
    count = []
    payload = b'#' + b'x' * 20000 + b'\nprintf synthetic\n'
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            count.append(1)
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload) + (100 if len(count) == 1 else 0)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            if len(count) == 1:
                time.sleep(.5)
        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/fixture'
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.curlrc').write_text('max-time = 0.2\nnoproxy = "*"\n')
    # Keep file-output command verbatim; intercept URL construction only.
    prefix = 'repo_raw() { printf %s "$URL"; }; curl() { local x; for x in "$@"; do case "$x" in http*://*) [ "$x" = "$URL" ] || return 97;; esac; done; command curl "$@"; }; '
    env = {**os.environ, 'HOME': str(home), 'CURL_HOME': str(home), 'URL': url, 'WIZARD_GET_SH_URL': url, 'WOS_DIR': str(tmp_path), 'helper': str(dest), 'REPO': url.rsplit('/fixture', 1)[0], 'REF': 'main'}
    # Base has no repo_raw: use the exact old expression's URL as the server's accepted URL.
    if SIDE == 'base' and site == 'named_host':
        prefix = prefix.replace('[ "$x" = "$URL" ]', '[ "$x" = "$REPO/raw/$REF/scripts/prepare-named-user.sh" ]')
    try:
        p = subprocess.run(['bash', '-c', prefix + cmd], env=env, capture_output=True, timeout=6)
    finally:
        server.shutdown()
        server.server_close()
    assert dest.read_bytes() == payload
    assert len(count) == (1 if SIDE == 'base' else 2)
    assert p.returncode == (28 if SIDE == 'base' else 0)
    # Exercise the exact existing completion operations, with synthetic content only.
    sentinel = tmp_path / 'unrelated'
    sentinel.write_text('keep')
    if site == 'install_start':
        finish = next(x.strip() for x in text.splitlines() if x.strip() == 'mv "$WOS_DIR/get.sh.tmp" "$WOS_DIR/get.sh"')
    else:
        finish = '\n'.join(next(x.strip() for x in text.splitlines() if x.strip() == command)
                           for command in ['bash "$helper" "$ALONG_USER"', 'rm -f "$helper"'])
    completed = subprocess.run(['bash', '-c', finish], env=env, capture_output=True, timeout=3)
    assert completed.returncode == 0
    assert not dest.exists()
    if site == 'install_start':
        assert (tmp_path / 'get.sh').read_bytes() == payload
    else:
        assert completed.stdout == b'synthetic'
    assert sentinel.read_text() == 'keep'
