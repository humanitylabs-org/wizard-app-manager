"""Light local real Git protocol test, not a manager/bootstrap substitute."""
import http.client
import http.server
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager_seal_store_http import Handler


class RealGit(unittest.TestCase):
    def test_synthetic_store_real_smart_http_clone_and_authority_negatives(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp); source = root/'source'; source.mkdir()
            env = {'PATH': '/usr/bin:/bin', 'HOME': tmp, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
            def git(*args, cwd=source):
                return subprocess.check_output(['git', *args], cwd=cwd, env=env, stderr=subprocess.STDOUT, timeout=10)
            git('init', '-b', 'main'); (source/'synthetic').write_text('No private data.\n')
            git('add', '.'); git('-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid', 'commit', '-m', 'Fixture')
            expected = git('rev-parse', 'HEAD').strip()
            git('clone', '--bare', str(source), str(root/'store.git'))
            class Local(Handler): project_root = tmp
            server = http.server.HTTPServer(('127.0.0.1', 0), Local)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True); thread.start()
            try:
                git('clone', 'http://127.0.0.1:'+str(server.server_port)+'/store.git', str(root/'clone'))
                self.assertEqual(git('rev-parse', 'HEAD', cwd=root/'clone').strip(), expected)
                self.assertEqual((root/'clone/synthetic').read_text(), 'No private data.\n')
                for path, status in [('/unrelated.git/info/refs?service=git-upload-pack', 404),
                                     ('/store.git/info/refs?service=git-receive-pack', 403), ('/etc/passwd', 404)]:
                    c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                    c.request('GET', path); r = c.getresponse(); self.assertEqual(r.status, status); r.read(); c.close()
            finally: server.shutdown(); server.server_close(); thread.join(timeout=1)


if __name__ == '__main__': unittest.main()
