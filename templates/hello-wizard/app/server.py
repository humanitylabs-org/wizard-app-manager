"""Smallest useful Wizard app MCP server: streamable-HTTP JSON-RPC, stdlib only."""
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [{"name": "wizard_time", "description": "Current UTC time on the Wizard box.",
          "inputSchema": {"type": "object", "properties": {}}},
         {"name": "wizard_echo", "description": "Echo text back. Proves the agent reached this app.",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]

def handle(msg):
    m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        r = {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
             "serverInfo": {"name": "hello-wizard", "version": os.environ.get("APP_VERSION", "0.1.0")}}
    elif m == "tools/list":
        r = {"tools": TOOLS}
    elif m == "tools/call":
        n, a = p.get("name"), p.get("arguments") or {}
        if n == "wizard_time":
            t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        elif n == "wizard_echo":
            t = str(a.get("text", ""))
        else:
            return {"jsonrpc": "2.0", "id": i, "error": {"code": -32602, "message": f"unknown tool {n}"}}
        r = {"content": [{"type": "text", "text": t}]}
    elif m == "ping":
        r = {}
    elif i is None:
        return None  # notification
    else:
        return {"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": f"unknown method {m}"}}
    return {"jsonrpc": "2.0", "id": i, "result": r}

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None):
        b = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/healthz": return self._send(200, {"ok": True})
        self._send(405 if self.path == "/mcp" else 404, {"error": "use POST /mcp"})
    def do_POST(self):
        if self.path != "/mcp": return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        out = [x for x in map(handle, body if isinstance(body, list) else [body]) if x]
        if not out: return self._send(202)
        self._send(200, out if isinstance(body, list) else out[0])
    def log_message(self, *a): pass

ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8799"))), H).serve_forever()
