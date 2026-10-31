"""Actual failed bytes + local synthetic Git HTTP replay; never runtime proof."""
import ast
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manager_seal_update_continuation as update
from manager_seal_store_http import Handler

FIXTURE=Path(__file__).parent/'fixtures/manager-seal-update-9a016bb8c339'

class VersionDiagnostics(unittest.TestCase):
    def setUp(self):
        pins=json.loads((FIXTURE/'SHA256.json').read_text())
        for name,pin in pins.items():
            raw=(FIXTURE/name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),pin['sha256'])
            self.assertEqual(len(raw),pin['bytes'])
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')); self.addCleanup(self.tmp.cleanup)
        self.out=Path(self.tmp.name)
        self.log=self.out/'manager.sanitized.log'
        self.log.write_bytes((FIXTURE/'manager.sanitized.log').read_bytes())
        self.saved=json.loads((FIXTURE/'terminal-update-failure.json').read_text())
        self.rig=Mock(out=self.out,manager='synthetic',receipt={})
        self.rig.sql.return_value=self.saved['database_status_version']
        self.rig.owned_fixture_ids.return_value=self.saved['owned_fixture_ids']
        self.rig.http.return_value=(200,{})
        self.rig.exec.return_value=json.dumps({'installed':{'tipi_version':1,'schema_valid':True,'available':True}})
        self.offset=self.log.read_bytes().index(b'09:59:21')
        self.request=self.saved['request_id']

    def test_retained_old_classifier_replayed_not_relabelled_as_pass(self):
        tree=ast.parse((FIXTURE/'old-continuation.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='ready')
        env={'URN':update.URN,'json':json,'re':__import__('re')}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'retained-old-ready','exec'),env)
        with self.assertRaisesRegex(RuntimeError,'terminal update failure'):
            env['ready'](self.rig,set(),self.offset,self.request)
        self.assertFalse(self.rig.receipt['terminal_update_failure']['synthetic_failure_log_seen'])
        self.assertIn('10:00:16',self.log.read_text())

    def test_actual_completed_wrong_version_still_fails_with_evidence(self):
        with self.assertRaisesRegex(RuntimeError,'terminal update failure'):
            update.ready(self.rig,set(),self.offset,self.request)
        saved=json.loads((self.out/'terminal-update-failure.json').read_text())
        self.assertTrue(saved['handler_success_log_seen'])
        self.assertFalse(saved['synthetic_failure_log_seen'])
        self.assertEqual(saved['finding'],'stale-installed-metadata')
        self.assertIn('database_row',saved)
        self.assertIn('synthetic_metadata',saved)
        self.assertIn('update completed successfully',saved['manager_log_last_200_lines'])
        self.assertTrue(saved['read_only'])

    def test_running_old_without_completion_is_not_terminal(self):
        self.log.write_bytes(self.log.read_bytes().split(b'10:00:16')[0])
        self.assertFalse(update.ready(self.rig,set(),self.offset,self.request))
        self.rig.http.assert_not_called();self.rig.exec.assert_not_called()

    def test_prior_request_success_is_outside_offset(self):
        self.assertFalse(update.ready(self.rig,set(),self.log.stat().st_size,self.request))
        self.rig.http.assert_not_called()

    def test_installed_valid_v2_db1_reports_product_defect_not_pass(self):
        self.rig.exec.return_value=json.dumps({'installed':{'tipi_version':2,'schema_valid':True,'available':True}})
        with self.assertRaises(RuntimeError):update.ready(self.rig,set(),self.offset,self.request)
        self.assertEqual(self.rig.receipt['terminal_update_failure']['finding'],'product-version-write-defect')

    def test_diagnostic_subreads_fail_independently(self):
        self.rig.exec.side_effect=RuntimeError('unavailable')
        self.rig.http.side_effect=RuntimeError('unavailable')
        with self.assertRaisesRegex(RuntimeError,'terminal update failure'):
            update.ready(self.rig,set(),self.offset,self.request)
        saved=json.loads((self.out/'terminal-update-failure.json').read_text())
        self.assertEqual(saved['synthetic_metadata']['diagnostic_error'],'RuntimeError')
        self.assertIn('manager_log_last_200_lines',saved)

    def test_success_marker_resamples_database_after_capture(self):
        self.rig.sql.side_effect=['running|1','running|2']
        self.assertTrue(update.ready(self.rig,{'0'*64},self.offset,self.request))
        self.rig.http.assert_not_called()

    def test_diagnostic_tail_is_bounded(self):
        self.log.write_text(('PUBLIC synthetic\n'*300)+'App '+update.URN+' update completed successfully\n')
        update.diagnose(self.rig,self.request,'running|1',self.log.read_text(),'test')
        saved=json.loads((self.out/'terminal-update-failure.json').read_text())
        self.assertEqual(len(saved['manager_log_last_200_lines'].splitlines()),200)
        self.assertLess((self.out/'terminal-update-failure.json').stat().st_size,128*1024)

    def test_retained_http_has_v1_both_installed_and_repository_metadata(self):
        receipt=json.loads((FIXTURE/'receipt.json').read_text())
        get=receipt['http_responses']['GET /api/apps/'+update.URN+' authenticated']['latest']
        body=get['body']
        self.assertEqual(body['app']['version'],1)
        self.assertEqual(body['info']['tipi_version'],1)
        self.assertEqual(body['metadata']['latestVersion'],1)
        self.assertEqual(receipt['cleanup_errors'],[])
        self.assertEqual(json.loads((FIXTURE/'cleanup.json').read_text())['parent_cleanup_residual'],[])

class SyntheticGitRefresh(unittest.TestCase):
    def test_dirty_clone_reverts_but_committed_http_upstream_survives_refresh(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp); seed=root/'seed';seed.mkdir(); old=root/'old';old.mkdir()
            def git(*args):
                return subprocess.check_output(['git',*map(str,args)],stderr=subprocess.PIPE,timeout=10).decode().strip()
            git('init','-b','main',seed)
            config=seed/'config.json';config.write_text('{"tipi_version":1}')
            git('-C',seed,'add','config.json')
            git('-C',seed,'-c','user.name=QA','-c','user.email=qa@example.invalid','commit','-m','v1')
            git('clone','--bare',seed,old/'store.git')
            published=root/'published'
            class SyntheticHandler(Handler):
                project_root=str(old)
                published_root=str(published)
            server=http.server.HTTPServer(('127.0.0.1',0),SyntheticHandler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                clone=root/'consumer';git('clone',f'http://127.0.0.1:{server.server_port}/store.git',clone)
                (clone/'config.json').write_text('{"tipi_version":2}') # old publication bug
                git('-C',clone,'fetch','origin','main');git('-C',clone,'reset','--hard','origin/main')
                self.assertEqual(json.loads((clone/'config.json').read_text())['tipi_version'],1)
                config.write_text('{"tipi_version":2}');git('-C',seed,'add','config.json')
                git('-C',seed,'-c','user.name=QA','-c','user.email=qa@example.invalid','commit','-m','v2')
                nextroot=root/'published.next';nextroot.mkdir();git('clone','--bare',seed,nextroot/'store.git')
                nextroot.rename(published)
                git('-C',clone,'fetch','origin','main');git('-C',clone,'reset','--hard','origin/main')
                self.assertEqual(json.loads((clone/'config.json').read_text())['tipi_version'],2)
                # Another product-style refresh remains v2, not just first readback.
                git('-C',clone,'fetch','origin','main');git('-C',clone,'reset','--hard','origin/main')
                self.assertEqual(json.loads((clone/'config.json').read_text())['tipi_version'],2)
            finally:
                server.shutdown();server.server_close();thread.join(2)
                self.assertFalse(thread.is_alive())

if __name__=='__main__':unittest.main()
