#!/usr/bin/env python3
"""Private loopback synthetic Git smart HTTP, using the real git backend."""
import http.server
import os
import subprocess
import urllib.parse


class Handler(http.server.BaseHTTPRequestHandler):
    project_root = '/app'
    def log_message(self, *args): pass
    def do_GET(self): self.forward()
    def do_POST(self): self.forward()
    def forward(self):
        p = urllib.parse.urlsplit(self.path)
        if p.path not in ('/store.git/info/refs', '/store.git/git-upload-pack'):
            self.send_error(404); return
        length = self.headers.get('Content-Length', '0')
        if not length.isdigit(): self.send_error(400); return
        n = int(length)
        if n > 2**20: self.send_error(413); return
        if self.command == 'GET' and p.query != 'service=git-upload-pack': self.send_error(403); return
        if self.command == 'POST' and p.query: self.send_error(403); return
        env = {'PATH': '/usr/bin:/bin', 'GIT_PROJECT_ROOT': self.project_root, 'GIT_HTTP_EXPORT_ALL': '1',
               'PATH_INFO': p.path, 'QUERY_STRING': p.query, 'REQUEST_METHOD': self.command,
               'CONTENT_TYPE': self.headers.get('Content-Type', ''), 'CONTENT_LENGTH': str(n),
               'REMOTE_ADDR': '127.0.0.1'}
        result = subprocess.run(['git', 'http-backend'], input=self.rfile.read(n), capture_output=True,
                                env=env, timeout=10)
        if result.returncode or len(result.stdout) > 4*2**20: self.send_error(502); return
        headers, body = result.stdout.split(b'\r\n\r\n', 1)
        rows = [line.decode().split(': ', 1) for line in headers.split(b'\r\n')]
        status = next((int(v.split()[0]) for k, v in rows if k.lower() == 'status'), 200)
        self.send_response(status)
        for k, v in rows:
            if k.lower() not in ('status', 'connection', 'content-length'): self.send_header(k, v)
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


if __name__ == '__main__':
    http.server.HTTPServer(('127.0.0.1', 8080), Handler).serve_forever()
