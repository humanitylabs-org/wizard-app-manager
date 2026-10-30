"""Finite cleanup proof, using a synthetic daemon protocol peer, never Docker."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
A, B, OTHER = 'a'*64, 'b'*64, 'c'*64


def guest(tmp_path):
    spec = importlib.util.spec_from_file_location('cleanup_guest_test', REPO/'qa/privsep-storage/production_guest.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    return module


class Daemon:
    def __init__(self, owned=(A, B), unowned=()):
        self.rows = {cid: {'Id': cid, 'Labels': {'com.docker.compose.project': 'runtipi'}} for cid in owned}
        self.rows.update({cid: {'Id': cid, 'Labels': {'com.docker.compose.project': 'unowned'}} for cid in unowned})
        self.calls = []
        self.delete_failure = None
        self.inspect_status = 404
        self.inventory_status = 200
        self.info_count = 0
        self.change_engine = False
    def request(self, method, path):
        self.calls.append((method, path))
        if path == '/info':
            self.info_count += 1
            return 200, {'ID': 'engine-2' if self.change_engine and self.info_count > 1 else 'engine-1'}
        if path == '/containers/json?all=true':
            return self.inventory_status, list(self.rows.values())
        cid = path.split('?', 1)[0].split('/')[2]
        assert len(cid) == 64
        if method == 'DELETE':
            if cid == self.delete_failure:
                return 403, {'message': 'permission denied'}
            self.rows.pop(cid, None)
            return 204, None
        assert method == 'GET' and path.endswith('/json')
        return self.inspect_status, {'message': 'Docker 29 arbitrary translated missing wording'}


def readback(tmp_path):
    return json.loads((tmp_path/'container-cleanup.json').read_text())


def test_full_id_structural_absence_ignores_cli_wording(tmp_path):
    m = guest(tmp_path); daemon = Daemon(); m.daemon_request = daemon.request
    m.cleanup_containers()
    result = readback(tmp_path)
    assert result['passed'] and result['readback_absent']
    assert result['attempted_exact_ids'] == result['removed_exact_ids'] == result['absent_exact_ids'] == [A, B]
    assert daemon.info_count == 2


@pytest.mark.parametrize('status', [200, 401, 403, 500, 503])
def test_inspect_error_or_survival_is_never_absence(tmp_path, status):
    m = guest(tmp_path); daemon = Daemon(owned=(A,)); daemon.inspect_status = status
    m.daemon_request = daemon.request
    with pytest.raises(AssertionError):
        m.cleanup_containers()
    result = readback(tmp_path)
    assert result['passed'] is False and result['absent_exact_ids'] == []
    assert not result['readback_absent']
    assert result['removed_exact_ids'] == [A]


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_inventory_failure_cannot_remove_or_prove_absence(tmp_path, status):
    m = guest(tmp_path); daemon = Daemon(); daemon.inventory_status = status
    m.daemon_request = daemon.request
    with pytest.raises(AssertionError):
        m.cleanup_containers()
    assert not any(method == 'DELETE' for method, _ in daemon.calls)
    assert not readback(tmp_path)['readback_absent']


def test_unavailable_daemon_is_failure_not_empty_guest(tmp_path):
    m = guest(tmp_path)
    def unavailable(*args):
        raise ConnectionRefusedError()
    m.daemon_request = unavailable
    with pytest.raises(ConnectionRefusedError):
        m.cleanup_containers()
    assert not readback(tmp_path)['readback_absent']


def test_unknown_project_survives_while_exact_owned_ids_are_removed(tmp_path):
    m = guest(tmp_path); daemon = Daemon(unowned=(OTHER,)); m.daemon_request = daemon.request
    with pytest.raises(AssertionError, match='incomplete'):
        m.cleanup_containers()
    assert set(daemon.rows) == {OTHER}
    assert not any(OTHER in path for method, path in daemon.calls if method == 'DELETE')
    result = readback(tmp_path)
    assert result['removed_exact_ids'] == result['absent_exact_ids'] == [A, B]
    assert result['unowned_exact_ids'] == [OTHER] and not result['passed']


def test_partial_removal_retains_progress_and_first_failure(tmp_path):
    m = guest(tmp_path); daemon = Daemon(); daemon.delete_failure = A
    m.daemon_request = daemon.request
    with pytest.raises(AssertionError, match='removal failed'):
        m.cleanup_containers()
    result = readback(tmp_path)
    assert result['attempted_exact_ids'] == [A, B]
    assert result['removed_exact_ids'] == result['absent_exact_ids'] == [B]
    assert not result['readback_absent'] and not result['passed']


def test_changed_daemon_invalidates_empty_readback(tmp_path):
    m = guest(tmp_path); daemon = Daemon(); daemon.change_engine = True
    m.daemon_request = daemon.request
    with pytest.raises(AssertionError, match='daemon changed'):
        m.cleanup_containers()
    assert not readback(tmp_path)['passed']


def test_malformed_or_short_ids_refuse_before_deletion(tmp_path):
    m = guest(tmp_path); daemon = Daemon(owned=()); daemon.rows[A[:12]] = {'Id': A[:12]}
    m.daemon_request = daemon.request
    with pytest.raises(AssertionError):
        m.cleanup_containers()
    assert not any(method == 'DELETE' for method, _ in daemon.calls)


def test_real_unix_http_transport_records_404_and_exact_full_ids(tmp_path):
    from http.server import BaseHTTPRequestHandler
    import socketserver
    import tempfile
    import threading
    m = guest(tmp_path)
    daemon = Daemon(owned=(A,))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self):
            status, value = daemon.request(self.command, self.path)
            raw = b'' if value is None else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        do_GET = reply
        do_DELETE = reply
    # Keep the Unix socket short; pytest's full node directory can exceed SUN_PATH.
    with tempfile.TemporaryDirectory(prefix='cleanup-api-', dir='/run' if os.geteuid() == 0 else None) as folder:
        address = str(Path(folder)/'s')
        with socketserver.UnixStreamServer(address, Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            transport = m.daemon_request
            m.daemon_request = lambda method, path: transport(method, path, socket_path=address)
            try:
                m.cleanup_containers()
            finally:
                server.shutdown(); thread.join(timeout=5)
                assert not thread.is_alive()
    assert readback(tmp_path)['absent_exact_ids'] == [A]
    assert ('GET', '/containers/' + A + '/json') in daemon.calls


def test_cleanup_runner_exports_partial_report_before_refusal():
    import ast
    tree = ast.parse((REPO/'qa/privsep-storage/production_run.py').read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'cleanup_guest')
    code = ast.unparse(fn)
    assert "180, False)" in code
    assert code.index("self.collect_json('container-cleanup')") < code.index('assert rc == 0')
