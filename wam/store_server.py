"""Serve the local Wizard store read-only over git smart-HTTP (Runtipi's isomorphic-git needs smart HTTP).

Runs inside the wizard-store container on the Runtipi network, so only Runtipi (and other containers on that
network) can reach it. Stdlib only; uses git's own http-backend for upload-pack (fetch). Pushes are refused.
"""
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/srv"  # the store is mounted at /srv/store
PORT = int(os.environ.get("PORT", "8080"))
BACKEND = "/usr/lib/git-core/git-http-backend"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _git(self):
        path, _, query = self.path.partition("?")
        if not path.startswith("/store.git/") or "receive-pack" in path or "receive-pack" in query:
            self.send_response(404); self.end_headers(); return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
        env = {"GIT_PROJECT_ROOT": ROOT, "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path.replace("/store.git", "/store", 1),
               "REQUEST_METHOD": self.command, "QUERY_STRING": query, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
               "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": self.client_address[0], "HOME": "/tmp", "PATH": "/usr/bin:/bin",
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
        r = subprocess.run([BACKEND], input=body, env=env, capture_output=True, timeout=120)
        head, sep, payload = r.stdout.partition(b"\r\n\r\n")
        if not sep:
            head, _, payload = r.stdout.partition(b"\n\n")
        code, headers = 200, []
        for line in head.decode(errors="replace").splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                code = int(v.strip().split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok"); return
        self._git()

    do_POST = _git


ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
