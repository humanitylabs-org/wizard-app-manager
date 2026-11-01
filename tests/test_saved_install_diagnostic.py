#!/usr/bin/python3
"""LOCAL SYNTHETIC ONLY: real fake executables, no mount/VM/Docker/installer.

The original 35 parser methods exercise this changed parser. Additional cases
exercise the actual guest reader + real subprocess seam on a fake filesystem,
and actual host console transport/checkpointing against an executable fake peer.
Identity fixtures are synthetic, never evidence of native namespace admission.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT/'qa/privsep-storage'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


P = load('saved_parser', QA/'saved_install_diagnostic_parser.py')
VOCAB = P.repertoire({p: (ROOT/p).read_text() for p in P.SOURCE_PATHS})
OLD = load('old_synthetic_cases', ROOT/'tests/test_installer_diagnostic_parser.py')
OLD.P, OLD.VOCAB = P, VOCAB
InstallerProjection = OLD.InstallerProjection
SENTINEL = 'https://secret.invalid/?token=DO-NOT-EXPORT'


def result(raw=b'unknown synthetic message\n', eof=True):
    value = OLD.wrap(P.project(raw, VOCAB, eof=eof))
    value['failed_mount_unmounted'] = True
    return value


class NewParser(unittest.TestCase):
    def test_source_templates_and_sentinels(self):
        samples = [
            'error: GET /apps/wizard-agent-service:wizardapps -> 500: '+SENTINEL,
            'wizard-agent-service: the App Manager could not fetch it from the index (see its logs)',
            'ValueError: maintenance-needed: protected Hermes storage mapping is unavailable or stale',
            'PermissionError: [Errno 13] '+SENTINEL,
        ]
        for sample in samples:
            with self.subTest(sample_id=samples.index(sample)):
                v = result((sample+'\n').encode())
                P.validate_export(v, VOCAB)
                self.assertIsNotNone(v['projection']['first_recognized_failure'])
                self.assertNotIn(SENTINEL, json.dumps(v))
        v = result((samples[0]+'\n').encode())
        self.assertEqual(v['projection']['events'][0]['http_status'], 500)

    def test_source_omission_and_drift(self):
        sources = {p: (ROOT/p).read_text() for p in P.SOURCE_PATHS}
        for path in P.SOURCE_PATHS:
            bad = dict(sources); del bad[path]
            with self.assertRaises(ValueError): P.repertoire(bad)
        bad = dict(sources); bad['wam/cli.py'] += '\n# synthetic drift\n'
        self.assertNotEqual(P.repertoire(bad)['source_sha256'], VOCAB['source_sha256'])

    def test_template_export_negatives(self):
        good = result(b'error: GET /apps/installed -> 500: PRIVATE\n')
        for value in (-1, True, len(VOCAB['wam_templates'])):
            bad = copy.deepcopy(good)
            bad['projection']['events'][0]['template_ids'] = [value]
            with self.assertRaises(AssertionError): P.validate_export(bad, VOCAB)
        for field, value in (('body', SENTINEL), ('http_status', True), ('http_status', 999)):
            bad = copy.deepcopy(good); bad['projection']['events'][0][field] = value
            with self.assertRaises(AssertionError): P.validate_export(bad, VOCAB)

    def test_no_success_from_unrecognized_or_partial(self):
        for raw, eof in ((SENTINEL.encode(), True), (b'error: GET /apps/installed -> 500: PRIVATE', False)):
            v = result(raw, eof)
            self.assertEqual(v['diagnosis'], 'unknown')
            self.assertNotIn(SENTINEL, json.dumps(v))


class Done(Exception): pass


class OSProxy:
    def __init__(self, root): self.root = root
    def __getattr__(self, name): return getattr(os, name)
    def getpid(self): return 1
    def uname(self): return types.SimpleNamespace(release='6.1.0-54-cloud-amd64')
    def mapped(self, path): return self.root/str(path).lstrip('/')
    def mkdir(self, path, mode): return os.mkdir(self.mapped(path), mode)
    def stat(self, path): return os.stat(self.mapped(path))
    def open(self, path, flags, **kw):
        return os.open(self.mapped(path) if str(path).startswith('/') else path, flags, **kw)


class GuestReader(unittest.TestCase):
    def fixture(self, d, variant):
        root = Path(d)
        fixed = {
            'sys/block/vda/serial': 'WZ_MAINT_851_RO',
            'sys/block/vdb/serial': 'WZ_FAILED_INSTALL_RO',
            'sys/block/vda/ro': '1', 'sys/block/vdb/ro': '1',
            'sys/class/block/vda1/dev': '254:1', 'sys/class/block/vdb1/dev': '254:17',
            'proc/self/mountinfo': '1 0 254:1 / / ro - ext4 /dev/vda1 ro,noload\n2 1 254:17 / /run/wz-forensic/failed ro - ext4 /dev/vdb1 ro,noload\n',
            'proc/self/ns/mnt': '', 'proc/self/ns/pid': '', 'proc/1/cmdline': 'synthetic-pid1',
            'proc/2/cmdline': '',
        }
        for p, text in fixed.items():
            f = root/p; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(text)
        (root/'run').mkdir(); (root/'sys/class/net/lo').mkdir(parents=True)
        if variant == 'daemon': (root/'proc/2/cmdline').write_text('synthetic daemon')
        if variant == 'serial': (root/'sys/block/vdb/serial').write_text('WRONG')
        if variant == 'writable': (root/'sys/block/vdb/ro').write_text('0')
        if variant == 'nic': (root/'sys/class/net/eth0').mkdir()
        if variant == 'root-rw':
            p = root/'proc/self/mountinfo'; p.write_text(p.read_text().replace('/ / ro', '/ / rw'))
        source = root/'synthetic-log'; source.write_text('error: GET /apps/installed -> 500: '+SENTINEL+'\n')
        fake = root/'fake-mount'
        fake.write_text('#!'+sys.executable+'\nimport pathlib,sys,time\nr=pathlib.Path('+repr(str(root))+')\na=sys.argv[1:]\nwith (r/"operations").open("a") as f:f.write("mount\\n")\n'
            +'v='+repr(variant)+'\n'
            +'if "ext4" in a:\n'
            +' if v=="mount-fail":sys.exit(1)\n'
            +' p=r/"run/wz-forensic/failed/root/storage-contract-a/installer-private.log";p.parent.mkdir(parents=True);p.write_bytes((r/"synthetic-log").read_bytes())\n'
            +' if v=="symlink":p.unlink();p.symlink_to(r/"synthetic-log")\n'
            +'if len(a)==1 and v=="unmount-fail":sys.exit(1)\n')
        fake.chmod(0o700)
        return root, fake

    def observe(self, variant='ok'):
        with tempfile.TemporaryDirectory() as d:
            root, fake = self.fixture(d, variant)
            g = load('synthetic_guest', QA/'saved_install_diagnostic_guest.py')
            g.Path = lambda p: root/str(p).lstrip('/')
            g.os = OSProxy(root); g.MOUNT = g.UMOUNT = str(fake)
            g.MAX_BYTES, g.CLASSES, g.project, g.VOCAB = P.MAX_BYTES, P.CLASSES, P.project, VOCAB
            out = io.StringIO()
            g.stay_inert = lambda: (_ for _ in ()).throw(Done())
            with contextlib.redirect_stdout(out):
                with self.assertRaises(Done): g.diagnose()
            text = out.getvalue(); self.assertNotIn(SENTINEL, text)
            v = json.loads(text.split('WZ_FORENSIC_RESULT=', 1)[1])
            P.validate_export(v, VOCAB)
            ops = (root/'operations').read_text().splitlines() if (root/'operations').exists() else []
            return v, ops

    def test_real_fake_command_and_reader(self):
        v, ops = self.observe()
        self.assertEqual(len(ops), 3)
        self.assertEqual(v['projection']['events'][0]['http_status'], 500)
        self.assertTrue(v['failed_mount_unmounted'])

    def test_identity_refusal_never_calls_mount(self):
        for variant in ('daemon', 'serial', 'writable', 'nic', 'root-rw'):
            with self.subTest(variant=variant):
                v, ops = self.observe(variant)
                self.assertEqual(v['first_error']['operation'], 'identity')
                self.assertEqual(ops, [])
                self.assertNotIn('projection', v)

    def test_mount_failure_is_not_absence(self):
        v, ops = self.observe('mount-fail')
        self.assertEqual(v['first_error']['operation'], 'mount-log-filesystem')
        self.assertNotIn('projection', v)
        self.assertEqual(len(ops), 2)

    def test_symlink_refused(self):
        v, _ = self.observe('symlink')
        self.assertEqual(v['first_error']['operation'], 'read-allowlisted-log')
        self.assertNotIn('projection', v)

    def test_unmount_failure_retains_observation(self):
        v, _ = self.observe('unmount-fail')
        self.assertFalse(v['failed_mount_unmounted'])
        self.assertEqual(v['projection']['events'][0]['http_status'], 500)


class HostTransport(unittest.TestCase):
    def exercise(self, mode):
        r = load('synthetic_host', QA/'saved_install_diagnostic_run.py')
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d); (stage/'source').mkdir()
            for path in r.SOURCE_FILES[:3]:
                dest = stage/'source'/path; dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((ROOT/path).read_bytes())
            (stage/'source-members.json').write_text(json.dumps(r.inventory(stage/'source')))
            output = result(b'unknown '+SENTINEL.encode()+b'\n')
            if mode == 'invalid': output['forged'] = SENTINEL
            if mode == 'partial': output = result(b'unknown partial', False)
            if mode == 'unmount-false': output['failed_mount_unmounted'] = False
            fake = stage/'fake-console.py'
            fake.write_text('import json,sys,re,time\nns=json.loads(sys.argv[1]);print("WZ_NAMESPACE="+json.dumps({"private":{k:v+100 for k,v in ns.items()},"readonly_input_fds":True,"all_exact_input_binds_ro":True}),flush=True)\nsys.stdout.write("# ");sys.stdout.flush()\na=sys.stdin.buffer.readline();n=int(re.search(rb"read\\(([0-9]+)\\)",a)[1]);print("WZ_INPUT_READY",flush=True)\na=sys.stdin.buffer.read(n)\nprint("WZ_FORENSIC_RESULT="+json.dumps('+repr(output)+'),flush=True)\ntime.sleep(60)\n')
            m = {'inputs': [], 'python': sys.executable, 'vocabulary': VOCAB, 'source_revision': 'a'*40,
                 'synthetic_tests': {'tests': 1, 'failures': 0, 'errors': 0}}
            procs = []
            def launch(argv, **kw):
                kw.pop('preexec_fn', None)
                p = subprocess.Popen([sys.executable, '-I', '-B', str(fake), argv[-1]], **kw)
                procs.append(p); return p
            real_killpg = os.killpg
            def timeout_cleanup(pid, sig):
                saved = json.loads((stage/'run/receipt.json').read_text())
                self.assertIn('first_error' if mode == 'invalid' else 'guest', saved)
                raise subprocess.TimeoutExpired(['synthetic-cleanup'], 15)
            try:
                with patch.object(r, 'preflight', return_value=(m, P, {})), patch.object(r, 'launch', launch), contextlib.redirect_stdout(io.StringIO()):
                    if mode in ('invalid', 'cleanup-timeout'):
                        with patch.object(r.os, 'killpg', timeout_cleanup): rc = r.execute(stage, 'a'*64)
                    else: rc = r.execute(stage, 'a'*64)
                saved = json.loads((stage/'run/receipt.json').read_text())
                self.assertNotIn(SENTINEL, json.dumps(saved))
                return rc, saved
            finally:
                for p in procs:
                    if p.poll() is None: real_killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=3)

    def test_real_fake_console_complete_unknown(self):
        rc, saved = self.exercise('ok')
        self.assertEqual(rc, 0); self.assertEqual(saved['diagnosis'], 'unknown')
        self.assertFalse(saved['full_acceptance']); self.assertTrue(saved['cleanup']['owned_group_absent'])

    def test_first_error_before_cleanup_timeout(self):
        rc, saved = self.exercise('invalid')
        self.assertEqual(rc, 1); self.assertEqual(saved['first_error']['error_class'], 'AssertionError')
        self.assertEqual(saved['cleanup']['error_class'], 'TimeoutExpired')
        self.assertFalse(saved['cleanup']['qemu_reaped'])

    def test_observation_before_cleanup_timeout(self):
        rc, saved = self.exercise('cleanup-timeout')
        self.assertEqual(rc, 1); self.assertIn('guest', saved)
        self.assertEqual(saved['cleanup']['error_class'], 'TimeoutExpired')

    def test_guest_unmount_unproven_is_not_complete(self):
        rc, saved = self.exercise('unmount-false')
        self.assertEqual(rc, 1); self.assertIn('first_error', saved)
        self.assertFalse(saved['guest']['failed_mount_unmounted'])

    def test_partial_is_not_complete(self):
        rc, saved = self.exercise('partial')
        self.assertEqual(rc, 1); self.assertIn('first_error', saved)
        self.assertTrue(saved['guest']['projection']['bounded_incomplete'])


if __name__ == '__main__':
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    outcome = unittest.TextTestRunner(stream=stream).run(suite)
    print(json.dumps({'tests': outcome.testsRun, 'failures': len(outcome.failures),
                      'errors': len(outcome.errors), 'skipped': len(outcome.skipped)}))
    if not outcome.wasSuccessful():
        # Synthetic fixtures only; safe diagnostic tracebacks never contain real data.
        sys.stderr.write(stream.getvalue())
    sys.exit(0 if outcome.wasSuccessful() else 1)
