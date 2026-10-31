"""Bounded live, owned-only Docker trace. Protocol tests are not engine proof."""
import json
from pathlib import Path
import socket
import threading
import time

from manager_seal_forward_guard import UnixHTTP, ID, decode


class RuntimeEventStream:
    def __init__(self, socket_path, output, authority, *, byte_cap=16*2**20,
                 row_cap=32768, seconds=500):
        self.output = Path(output)
        self.authority = authority
        self.byte_cap = byte_cap; self.row_cap = row_cap; self.seconds = seconds
        self.rows = []; self.total = 0; self.error = None
        self.ready = threading.Event(); self.stopping = threading.Event()
        self.lock = threading.Lock(); self.connection = UnixHTTP(str(socket_path), timeout=seconds)
        self.socket = None; self.thread = None; self.drained = False; self.timer = None

    def accept(self, line):
        """Validate before retaining raw bytes; foreign metadata is never saved."""
        if len(line) > 65536 or not line.endswith(b'\n'):
            raise RuntimeError('event line incomplete or oversized')
        self.total += len(line)
        if self.total > self.byte_cap or len(self.rows) >= self.row_cap:
            raise RuntimeError('event trace cap')
        row = decode(line)
        actor = row.get('Actor') or {}; attrs = actor.get('Attributes') or {}
        owned = self.authority()
        if (row.get('Type') not in ('container', 'network') or
                not ID.fullmatch(actor.get('ID', '')) or actor['ID'] not in owned or
                (attrs.get('container') and attrs['container'] not in owned)):
            raise RuntimeError('unowned downstream event')
        with self.lock:
            self.rows.append(row)

    def start(self):
        def expire():
            if self.stopping.is_set(): return
            self.error = 'event stream deadline'
            if self.socket is not None:
                try: self.socket.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        self.timer = threading.Timer(self.seconds, expire); self.timer.daemon = True
        self.timer.start()
        def read():
            try:
                # Nanosecond floor avoids replaying the earlier probe's teardown.
                now = time.time_ns(); since = f'{now//10**9}.{now%10**9:09d}'
                self.connection.request('GET', '/events?since='+since)
                self.socket = self.connection.sock
                response = self.connection.getresponse()
                if response.status != 200: raise RuntimeError('event stream rejected')
                self.ready.set()
                deadline = time.monotonic()+self.seconds
                while not self.stopping.is_set():
                    if time.monotonic() >= deadline: raise RuntimeError('event stream deadline')
                    line = response.readline(65537)
                    if not line:
                        if self.stopping.is_set(): return
                        raise RuntimeError('premature event EOF')
                    self.accept(line)
            except Exception as exc:
                if not (self.stopping.is_set() and isinstance(exc, OSError)):
                    # Fixed diagnostic classes only; never include row contents.
                    self.error = type(exc).__name__
            finally:
                self.ready.set(); self.connection.close()
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            self.error = 'event stream readiness timeout'
        if self.error:
            self.finish()
        return self

    def barrier(self, complete, *, timeout=10):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.error or not self.thread.is_alive(): break
            with self.lock: rows = list(self.rows)
            if complete(rows): return
            time.sleep(.05)
        if not self.error: self.error = 'event readiness sentinel timeout'
        raise RuntimeError(self.error)

    def drain(self, complete, *, timeout=20, settle=.75):
        """Creation-ledger coverage sentinel + quiet settle, never elapsed-time only."""
        deadline = time.monotonic()+timeout; covered_since = None; last_count = -1
        while time.monotonic() < deadline:
            if self.error or not self.thread.is_alive(): break
            with self.lock: rows = list(self.rows)
            if complete(rows):
                # A coverage predicate remaining true is not quietness. Reset
                # settle on every new owned event, then join before final audit.
                fingerprint = len(rows)
                if fingerprint != last_count: covered_since = None
                last_count = fingerprint
                covered_since = covered_since or time.monotonic()
                if time.monotonic()-covered_since >= settle:
                    self.drained = True; return
            else: covered_since = None
            time.sleep(.05)
        if not self.error: self.error = 'event coverage sentinel timeout'

    def finish(self):
        self.stopping.set()
        if self.timer is not None: self.timer.cancel()
        if self.socket is not None:
            try: self.socket.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if self.thread is not None: self.thread.join(timeout=3)
        if self.thread is not None and self.thread.is_alive(): self.error = 'event reader did not stop'
        # Persist every successfully decoded owned event BEFORE asserting errors
        # or coverage. This also runs on failed start/restart transactions.
        with self.lock: rows = list(self.rows)
        self.output.write_text(json.dumps(rows, separators=(',', ':'))+'\n')
        if self.error: raise RuntimeError(self.error)
        if not self.drained: raise RuntimeError('event stream not drained')
        return rows
