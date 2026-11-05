#!/usr/bin/env python3
"""Bounded source/security regressions; no browser, Docker or listening sockets."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('origin_qa',HERE/'origin_qa.py');assert spec and spec.loader
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

class OriginContract(unittest.TestCase):
    def test_exact_route_guards_precede_actual_nginx_origin(self):
        text=(HERE/'metadata.Caddyfile').read_text()
        expected=['handle /wizard-os-home/build.json {','request_header -X-Wizard-Owner-Proof','forward_auth 127.0.0.1:19447','@badBuildMethod not method GET','@foreignBuildOrigin expression','@crossSiteBuild header','@buildQuery expression','reverse_proxy __SHELL_STATIC_UPSTREAM__']
        positions=[text.index(x) for x in expected]
        self.assertEqual(positions,sorted(positions))
        for bad in ['{file.', '__CARRIER__', '__SERVED_SHA__', 'file_server', 'handle_path', '/api/', 'X-Wizard-Owner-Proof {file.']:
            self.assertNotIn(bad,text)
        self.assertIn('header_up -Cookie',text)
        self.assertIn('header_up -Authorization',text)
        self.assertEqual(text.count('handle '),1)

    def test_native_transport_is_exclusively_loopback(self):
        with mock.patch.object(qa.http.client,'HTTPConnection') as connect:
            c=connect.return_value;c.getresponse.return_value.status=200;c.getresponse.return_value.read.return_value=b'{}';c.getresponse.return_value.getheaders.return_value=[]
            qa.request(12345,owner=False)
            self.assertEqual(connect.call_args.args,('127.0.0.1',12345))
            self.assertEqual(connect.call_args.kwargs['source_address'],('127.0.0.2',0))
            self.assertTrue(c.close.called)

    def test_native_job_is_not_a_live_capture_or_activator(self):
        source=(HERE/'origin_qa.py').read_text()
        self.assertIn("assert str(ROOT).startswith('/srv/work/ab-')",source)
        self.assertIn("'live_acceptance':False",source)
        self.assertIn("source_address=('127.0.0.1' if owner else '127.0.0.2',0)",source)
        self.assertNotIn('curl',source)
        self.assertNotIn('.local/share/wizard-ux/current',source)
        self.assertNotIn('ssh',source)
        self.assertNotIn('wizard_heavy_submit',source)
        self.assertIn("'--pull=never'",source)

    def test_candidate_closure_is_immutable_not_a_whole_live_head_pin(self):
        source=(HERE/'browser.cjs').read_text()
        self.assertIn('QA_TRANSPORT_ORIGIN',source)
        self.assertIn("assert.equal(process.env.QA_PHASE,'candidate')",source)
        self.assertIn('127\\.0\\.0\\.1',source)
        self.assertIn('row.refused_network.length===0',source)
        self.assertNotIn('route.continue(',source)
        self.assertNotIn('route.fallback(',source)
        self.assertNotIn("if(process.env.QA_PHASE==='candidate')return await json(build)",source)
        self.assertIn('await route.fetch(fetchOptions(u))',source)

    def test_failed_legacy_packets_cannot_be_promoted_as_new_route_security(self):
        source=(HERE/'origin_qa.py').read_text()
        self.assertIn("'legacy_43_checks_inherited':False",source)
        self.assertIn('fixture.check_guards_and_logger()',source)
        self.assertIn('unfiltered positive proof sentinel',source)
        self.assertIn('runtime proof key and value deleted',source)
        self.assertIn('nonproof error headers preserved',source)

    def test_cleanup_requires_target_absence_not_successful_signal(self):
        source=(HERE/'origin_qa.py').read_text()
        tree=ast.parse(source)
        body=ast.get_source_segment(source,next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='stop_group'))
        assert body is not None
        self.assertIn('os.killpg(proc.pid,0)',body)
        self.assertIn('return False',body)
        self.assertIn("assert p.returncode!=0,'fixture container still exists'",source)

    def test_standalone_case_uses_real_css_and_real_renderer(self):
        source=(HERE/'browser.cjs').read_text()
        self.assertIn('data-presentation="standalone"',source)
        for path in ['kit/wizard-tokens.css','kit/wizard-ui.css','core/settings.css','core/box-source.js']:
            self.assertIn(path,source)
        self.assertIn('fallback.top&&!fallback.hosted&&fallback.skin===null',source)
        self.assertIn('contrast(fallback.ink)>=4.5&&contrast(fallback.muted)>=4.5',source)
        self.assertIn("'fallback'",source)
        self.assertNotIn('bypassCSP:true',source)
        self.assertNotIn('await page.waitForFunction(',source)

    def test_activation_keeps_existing_cas_lock_and_parent_read_gate(self):
        source=(HERE/'activate_origin.py').read_text()
        for token in ['fcntl.LOCK_EX|fcntl.LOCK_NB','wizard-app-promotions/wam.lock','parent-reviewed-receipt-sha256','helpers.reload_gateway','helpers.atomic','actual_serving_commit','comparison module changed','metadata_source_closure']:
            self.assertIn(token,source)
        self.assertNotIn(".local/share/wizard-ux/current",source)
        self.assertNotIn("'restart'",source)
        self.assertNotIn('docker run',source)

if __name__=='__main__':
    unittest.main()
