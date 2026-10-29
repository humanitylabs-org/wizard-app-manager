"""Fast offline runner/payload tests. No Docker, Nest or queue acceptance claim."""
import ast
import base64
import copy
import hashlib
import importlib.util
import inspect
import subprocess
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_installed_path as rig
import manager_seal_installed_recovery as recovery


class PinsAndBounds(unittest.TestCase):
    def test_aggregate_caps_include_control_tools_and_shmem(self):
        self.assertLessEqual(sum(rig.MEMORY.values()), 12*rig.GIB)
        self.assertLessEqual(sum(rig.TMPFS.values()), 8*rig.GIB)
        self.assertEqual(rig.TMPFS['fixture'], 64*rig.MIB)
        self.assertEqual(sum(rig.FOOTPRINT.values()), rig.GIB)
        self.assertGreaterEqual(rig.FOOTPRINT['daemon_overlay_and_metadata_estimate'], 128*rig.MIB)
        self.assertIn('controller', rig.MEMORY)
        self.assertIn('shmem', rig.TMPFS)

    def test_source_manifest_refuses_content_mode_and_source_drift(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp); p = root/'source.py'; p.write_text('synthetic\n'); p.chmod(0o644)
            data = {'source': 'a'*40, 'files': {'source.py': rig.sha(p)}, 'modes': {'source.py': 0o644}}
            (root/'SOURCE.json').write_text(json.dumps(data)); digest = rig.sha(root/'SOURCE.json')
            self.assertEqual(rig.manifest(root, 'a'*40, digest), data)
            p.chmod(0o755)
            with self.assertRaises(AssertionError): rig.manifest(root, 'a'*40, digest)
            p.chmod(0o644); p.write_text('changed')
            with self.assertRaises(AssertionError): rig.manifest(root, 'a'*40, digest)
            with self.assertRaises(AssertionError): rig.manifest(root, 'b'*40, digest)

    def test_real_inherited_build_argv_no_socket_and_lower_shared_limits(self):
        path = Path(rig.__file__).with_name('manager-seal-early.py')
        spec = importlib.util.spec_from_file_location('early_payload_test', path)
        early = importlib.util.module_from_spec(spec); spec.loader.exec_module(early)
        argv = rig.build_command(early, Path('/srv/work/manager-seal-installed-test'), Path('/srv/work/manager-seal-installed-test/installed-results'),
                                 'owner', 1000, 1000, 'a'*40, 'b'*64, 'owned-work', '/var/lib/docker/volumes/owned-fixture/_data')
        self.assertEqual(argv[argv.index('--memory')+1], str(6*rig.GIB))
        self.assertEqual(argv[argv.index('--memory-swap')+1], str(6*rig.GIB))
        self.assertNotIn('--tmpfs', argv)
        self.assertNotIn('docker.sock', ' '.join(argv))
        self.assertIn('type=volume,src=owned-work,dst=/work,volume-nocopy', argv)
        self.assertIn('--pull=never', argv)
        self.assertIn('--inside-build', argv)
        self.assertIn('type=bind,src=/srv/work/manager-seal-installed-test/installed-results/bootstrap-tmp,dst=/tmp,readonly', argv)

    def test_fixture_selector_metadata_literal_identity_and_no_image_tags(self):
        compiled, recipe = rig.fixture('/var/lib/docker/volumes/owned/_data', 'owner', 1000, 1000)
        self.assertEqual(recipe['x-runtipi'], {'schema_version': 2})
        self.assertTrue(recipe['services'][rig.MAIN]['x-runtipi']['is_main'])
        self.assertNotIn('x-runtipi', compiled)
        self.assertEqual(set(compiled['services']), set(rig.SERVICES))
        self.assertEqual(len(compiled['networks']), 4)
        for service in compiled['services'].values():
            self.assertEqual(service['image'], rig.PYTHON)
            self.assertEqual(service['user'], '1000:1000')
            self.assertEqual(service['memswap_limit'], service['mem_limit'])
            self.assertEqual(service['pull_policy'], 'never')
            self.assertFalse(service.get('privileged', False))

    def test_ambiguous_creation_cleanup_only_exact_new_names_metadata_no_recreate(self):
        rows = [{'operation': 'creation-intent', 'kind': kind, 'name': 'owned-new'} for kind in ('container', 'network', 'volume')]
        responses = [types.SimpleNamespace(returncode=0, stdout=v) for v in (b'a'*64+b'\n', b'b'*64+b'\n', b'owned-new\n')]
        with patch.object(rig.subprocess, 'run', side_effect=responses) as run:
            found = rig.reconcile_creation_intents(rows, 'owner')
        self.assertEqual(len(found), 3)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIn('label=wizard.qa.manager-seal=owner', argv)
            self.assertTrue(any(v.startswith('name=^') and v.endswith('owned-new$') for v in argv))
            self.assertNotIn('create', argv); self.assertNotIn('inspect', argv)
        with patch.object(rig.subprocess, 'run') as run:
            with self.assertRaises(AssertionError): rig.reconcile_creation_intents([dict(rows[0], name='/')], 'owner')
            run.assert_not_called()

    def test_http_timeout_is_one_post_no_retry(self):
        r = rig.Rig.__new__(rig.Rig)
        calls = []
        def http(*a): calls.append(a); raise TimeoutError('synthetic ambiguity')
        r.http = http
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r.out = Path(tmp)
            with self.assertRaises(TimeoutError): r.transaction('install', {'skipRun': True}, 'stopped')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'POST')


class HttpContractAndCapture(unittest.TestCase):
    root = Path(__file__).resolve().parents[2]

    def node(self, code, argument):
        import shutil
        node = str(rig.NODE) if rig.NODE.is_file() else shutil.which('node')
        assert node is not None, 'offline HTTP source regressions require Node'
        if rig.NODE.is_file(): self.assertEqual(rig.sha(node), rig.NODE_SHA)
        result = subprocess.run([node, '--input-type=module', '-e', code, argument],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr[:2048])
        return json.loads(result.stdout)

    def test_exact_shipped_guard_old_missing_origin_rejects_corrected_accepts(self):
        # Only import/decorator/constructor wiring is replaced. All helpers and
        # the actual canActivate body execute unchanged, without Nest/network.
        code = r"""import fs from 'node:fs';import {stripTypeScriptTypes} from 'node:module';
import assert from 'node:assert/strict';
let source=fs.readFileSync(process.argv[1],'utf8').replace(/^import .*;\n/gm,'').replace('@Injectable()','').replace('export class','class').replace('constructor(private readonly logger: LoggerService) {}','constructor(logger) {this.logger=logger;}');
const suffix=`
const guard=new AuthGuard({debug(){}});const results=[];
for(const [name,headers,user,method,expected] of [
 ['old missing origin',{},true,'POST',403],['wrong origin',{origin:'http://evil.invalid'},true,'POST',403],
 ['correct origin',{origin:'http://127.0.0.1:3000'},true,'POST',200],
 ['correct referer',{referer:'http://127.0.0.1:3000/page'},true,'POST',200],
 ['wrong port',{origin:'http://127.0.0.1:3001'},true,'POST',403],
 ['unauthenticated',{origin:'http://127.0.0.1:3000'},false,'POST',401],['session readback',{},true,'GET',200]]) {
 const request={method,url:'/api/app-lifecycle/agent-synthetic:migrated/install',protocol:'http',headers:{host:'127.0.0.1:3000',...headers},user:user?{id:1}:undefined,authMethod:'session'};
 let status=200;try{await guard.canActivate({switchToHttp:()=>({getRequest:()=>request})});}catch(e){status=e.status;}
 assert.equal(status,expected,name);results.push({name,status});
}console.log(JSON.stringify(results));`;
const js=stripTypeScriptTypes(source,{mode:'strip'});
await new Function('ForbiddenException','UnauthorizedException','return (async()=>{'+js+suffix+'})()')(
 class extends Error {status=403},class extends Error {status=401});"""
        results = self.node(code, str(self.root/'packages/backend/src/modules/auth/auth.guard.ts'))
        self.assertEqual(len(results), 7)
        self.assertTrue(recovery.audit_install_http_contract(self.root)['body_fields_permitted'])

    def test_actual_http_javascript_headers_bound_plaintext_and_transport(self):
        prefix = r"""const request=JSON.parse(process.argv[1]);let calls=0;
globalThis.fetch=async(url,options)=>{calls++;if(request[2]?.fail)throw new Error('synthetic secret never export');
if(request[3]&&['POST','PUT','PATCH','DELETE'].includes(request[0])&&options.headers.Origin!=='http://127.0.0.1:3000')throw new Error('missing origin');
if(!request[3]&&options.headers.Cookie)throw new Error('unexpected cookie');
if(request[2]?.partial){let n=0;return new Response(new ReadableStream({pull(c){if(n++)throw new Error('synthetic-pass');c.enqueue(new TextEncoder().encode('partial body'));}}),{status:403});}
return new Response(request[2]?.large?'x'.repeat(20000):'plain rejection',{status:403});};
"""
        for body, expected in [({}, (403, False, None)), ({'large': True}, (403, True, None)),
                               ({'fail': True}, (0, False, 'transport_or_read_error')),
                               ({'partial': True}, (403, False, 'transport_or_read_error'))]:
            value = self.node(prefix+recovery.HTTP_CODE, json.dumps(['POST', '/api/app-lifecycle/'+rig.URN+'/install', body, 'synthetic-cookie']))
            self.assertEqual((value['status'], value['body_truncated'], value['transport_error']), expected)
            self.assertLessEqual(value['captured_bytes'], recovery.HTTP_BODY_CAP)
        source = recovery.HTTP_CODE
        self.assertEqual(source.count('await fetch('), 1)
        self.assertNotIn('r.text()', source)

    def runner(self, root, value):
        r = rig.Rig.__new__(rig.Rig); r.out = root; r.receipt = {}; r.manager = 'owned-id'
        r.secrets = ['synthetic-pass', 'synthetic-sid']; r.cookie = 'runtipi-sid=synthetic-sid'
        r.exec = lambda *a, **kw: json.dumps(value)
        r.state = lambda _: {'Running': True}
        return r

    def envelope(self, **changes):
        return dict(status=403, cookie=None, body_text='{"message":"Invalid request origin","password":"synthetic-pass","headers":{"cookie":"synthetic-sid"}}',
                    captured_bytes=120, body_truncated=False, transport_error=None, **changes)

    def test_rejection_saved_before_assertion_no_cookie_headers_or_secrets(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = self.runner(Path(tmp), self.envelope())
            calls = []
            def execute(*a, **kw): calls.append(a); return json.dumps(self.envelope())
            r.exec = execute
            with self.assertRaisesRegex(AssertionError, 'HTTP transaction rejected'):
                r.transaction('install', {'skipRun': True}, 'stopped')
            self.assertEqual(len(calls), 1)
            raw = (r.out/'http-responses.sanitized.json').read_text()
            row = next(iter(json.loads(raw).values()))['first']
            self.assertEqual(row['status'], 403)
            self.assertEqual(row['body']['message'], 'Invalid request origin')
            for secret in ('synthetic-pass', 'synthetic-sid', 'runtipi-sid='): self.assertNotIn(secret, raw)
            self.assertEqual(row['body']['headers'], '[credential field withheld]')

    def test_non_json_overflow_timeout_and_exec_failure_still_saved(self):
        variants = [dict(status=500, body_text='plain error'), dict(status=201, body_text='x'*8192, body_truncated=True),
                    dict(status=0, transport_error='timeout')]
        for variant in variants:
            with self.subTest(variant=variant.keys()), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                value = self.envelope(); value.update(variant); r = self.runner(Path(tmp), value)
                with self.assertRaises((AssertionError, RuntimeError)): r.http('POST', '/api/app-lifecycle/'+rig.URN+'/install', {})
                row = next(iter(json.loads((r.out/'http-responses.sanitized.json').read_text()).values()))['latest']
                self.assertEqual(row['status'], value['status'])
                self.assertEqual(row['body_truncated'], value['body_truncated'])
                self.assertEqual(row['transport_error'], value['transport_error'])
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = self.runner(Path(tmp), self.envelope())
            r.exec = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('synthetic-pass'))
            with self.assertRaises(RuntimeError): r.http('POST', '/api/app-lifecycle/'+rig.URN+'/install', {})
            self.assertNotIn('synthetic-pass', (r.out/'http-responses.sanitized.json').read_text())

    def test_readiness_failures_cannot_succeed_and_records_bounded_first_latest(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            value = self.envelope(); value.update(status=401, transport_error='transport_or_read_error')
            r = self.runner(Path(tmp), value)
            for _ in range(150):
                self.assertEqual(r.http('GET', '/api/apps/installed', authenticated=False, allow_notready=True), (0, None))
            records = json.loads((r.out/'http-responses.sanitized.json').read_text())
            self.assertEqual(len(records), 1); self.assertEqual(next(iter(records.values()))['attempts'], 150)
            self.assertLess((r.out/'http-responses.sanitized.json').stat().st_size, 4096)

    def test_registration_cookie_in_memory_only_and_success_request_id_preserved(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            value = self.envelope(); value.update(status=201, cookie='runtipi-sid=new-synthetic-sid; HttpOnly', body_text='{"ok":true}')
            r = self.runner(Path(tmp), value)
            self.assertEqual(r.http('POST', '/api/auth/register', {}, authenticated=False), (201, {'ok': True}))
            self.assertEqual(r.cookie, 'runtipi-sid=new-synthetic-sid')
            self.assertIn('new-synthetic-sid', r.secrets)
            self.assertNotIn('new-synthetic-sid', (r.out/'http-responses.sanitized.json').read_text())
            value.update(cookie=None, body_text='{"requestId":"f6f719cb-e92b-4da2-80a6-9a0989d6c946"}')
            r.exec = lambda *a, **kw: json.dumps(value)
            self.assertEqual(r.http('POST', '/api/app-lifecycle/'+rig.URN+'/install', {})[1]['requestId'], 'f6f719cb-e92b-4da2-80a6-9a0989d6c946')


class PackagedStartupSource(unittest.TestCase):
    root = Path(__file__).resolve().parents[2]

    def invocation(self):
        return rig.manager_invocation(1000, 1000, 'owned-net', 'owned-work', '/owned/work',
            'owned-fixture', '/owned/fixture', Path('/owned/shared'), Path('/owned/out'),
            'a'*40, 'synthetic-db-fixture', 'synthetic-queue-fixture')

    def test_actual_runner_source_and_actual_manager_argv(self):
        options, command = self.invocation()
        audit = recovery.audit_packaged_startup(self.root, options, command, 'synthetic-db-fixture')
        self.assertEqual(audit['canonical_runner_workdir'], '/app')
        self.assertEqual(audit['actual_manager_argv_workdir'], '/app')
        self.assertEqual(len(audit['all_cwd_or_module_relative_consumers']), 2)
        self.assertTrue(audit['database_uses_configured_password'])
        self.assertFalse(audit['frontend_proven'])
        self.assertFalse(audit['runtime_executed'])
        self.assertIn('NOT built/copied', audit['asset_groups']['/app/assets/frontend']['runtime_origin'])
        # The real run uses this producer and checks Docker's read-back, not just
        # a test-only argv lookalike. No Docker operation is invoked by this test.
        source = inspect.getsource(rig.Rig.run)
        self.assertIn('options, command = manager_invocation(', source)
        self.assertIn("config['Config']['WorkingDir'] == recovery.MANAGER_WORKDIR", source)
        self.assertIn('dbpw = os.urandom(24).hex()', source)

    def test_original_missing_workdir_and_other_argv_drift_refused(self):
        options, command = self.invocation()
        variants = [options[2:], ['--workdir', '/', *options[2:]],
                    ['--workdir', '/app', *options]]
        for changed in variants:
            with self.subTest(argv=changed[:4]), self.assertRaises(AssertionError):
                recovery.audit_packaged_startup(self.root, changed, command, 'synthetic-db-fixture')
        with self.assertRaises(AssertionError):
            recovery.audit_packaged_startup(self.root, options, ['-c', 'cd /; '+command[1]], 'synthetic-db-fixture')
        with self.assertRaises(AssertionError):
            recovery.audit_packaged_startup(self.root, options, command, 'random-but-incompatible')

    def test_actual_source_mutations_refuse_before_runtime(self):
        import shutil
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)/'source'
            shutil.copytree(self.root, root, ignore=shutil.ignore_patterns('.git', '__pycache__', 'node_modules', 'installed-results', 'RECOVERY-EVIDENCE'))
            options, command = self.invocation()
            cases = [('Dockerfile', 'WORKDIR /app', 'WORKDIR /wrong'),
                     ('packages/backend/src/modules/i18n/i18n.service.ts', "'assets', 'translations'", "'wrong', 'translations'"),
                     ('packages/backend/src/core/database/database.service.ts', '${password}', '${wrong}'),
                     ('packages/backend/src/app.module.ts', "'assets', 'frontend'", "'assets', 'wrong'"),
                     ('packages/backend/src/core/database/drizzle/meta/_journal.json', '"entries": [', '"entries": [{"tag":"absent-file"},')]
            for rel, before, after in cases:
                p = root/rel; raw = p.read_text(); self.assertIn(before, raw)
                p.write_text(raw.replace(before, after))
                with self.subTest(source=rel), self.assertRaises(AssertionError):
                    recovery.audit_packaged_startup(root, options, command, 'synthetic-db-fixture')
                p.write_text(raw)
            p = root/'packages/backend/src/new-cwd-consumer.ts'; p.write_text('const x = process.cwd();\n')
            with self.assertRaises(AssertionError):
                recovery.audit_packaged_startup(root, options, command, 'synthetic-db-fixture')
            p.unlink()
            self.assertTrue(recovery.audit_packaged_startup(root, options, command, 'synthetic-db-fixture')['static_audit_pass'])


class EventProof(unittest.TestCase):
    def setUp(self):
        self.cid = 'a'*64; self.net = 'b'*64
        self.rows = [{'operation': 'created', 'kind': 'container', 'id': self.cid, 'name': 'qa-'+rig.MAIN},
                     {'operation': 'created', 'kind': 'network', 'id': self.net, 'name': rig.PROJECT+'_'+rig.MAIN+'-seal'}]
        self.events = [{'Type': 'container', 'Action': 'create', 'Actor': {'ID': self.cid}},
                       {'Type': 'network', 'Action': 'connect', 'Actor': {'ID': self.net, 'Attributes': {'container': self.cid}}}]

    def test_unlabeled_network_actor_has_exact_id_coverage(self):
        result = rig.reconcile_runtime_events(self.events, self.rows, {self.cid}, 'qa')
        self.assertEqual(result['connect_pair_coverage'], 1)

    def test_missing_create_or_connect_is_not_a_pass(self):
        for events in (self.events[:1], self.events[1:], []):
            with self.assertRaises(AssertionError): rig.reconcile_runtime_events(events, self.rows, {self.cid}, 'qa')

    def test_transient_unrelated_or_public_attachment_refuses(self):
        for net in ('unknown', 'c'*64):
            events = copy.deepcopy(self.events)
            events.append({'Type': 'network', 'Action': 'connect', 'Actor': {'ID': net, 'Attributes': {'container': self.cid}}})
            with self.assertRaises(AssertionError): rig.reconcile_runtime_events(events, self.rows, {self.cid}, 'qa')


class FakeProcess:
    def __init__(self, payload): self.stdout = io.BytesIO(payload); self.stderr = io.BytesIO(); self.code = 0
    def wait(self, timeout=None): return self.code
    def poll(self): return self.code
    def kill(self): self.code = -9


def archive(name='file', size=7, kind=tarfile.REGTYPE):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tf:
        m = tarfile.TarInfo(name); m.size = size if kind == tarfile.REGTYPE else 0; m.type = kind
        if kind != tarfile.REGTYPE: m.linkname = '/unrelated'
        tf.addfile(m, io.BytesIO(b'example') if m.size else None)
    return out.getvalue()


class ArtifactCopy(unittest.TestCase):
    def test_bounded_single_owned_file_streams_real_bytes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            target = Path(tmp)/'out'
            with patch.object(rig.subprocess, 'Popen', return_value=FakeProcess(archive())) as call:
                rig.copy_owned_file('owned-id', '/work/file', target, 7)
            self.assertEqual(target.read_bytes(), b'example')
            self.assertEqual(call.call_args.args[0], ['docker', 'cp', 'owned-id:/work/file', '-'])

    def test_reject_size_links_traversal_before_host_write(self):
        for payload, cap in [(archive(), 6), (archive('../file'), 7), (archive(kind=tarfile.SYMTYPE), 7), (archive(kind=tarfile.LNKTYPE), 7)]:
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                target = Path(tmp)/'out'
                with patch.object(rig.subprocess, 'Popen', return_value=FakeProcess(payload)):
                    with self.assertRaises(AssertionError): rig.copy_owned_file('owned', '/work/file', target, cap)
                self.assertFalse(target.exists())


# Full, unmodified public rabbitmq-env copied from the retained pinned image:
# /srv/work/manager-seal-installed-5269821a0714/installed-results/rabbitmq-env.
# The identical upstream v4.4.0 file was independently retained before the run.
# MPL-2.0; Copyright (c) 2007-2026 Broadcom. See the decoded source header.
# Compression is storage only; tests check all 7117 original bytes, not a facsimile.
RABBIT_PUBLIC_ENV = zlib.decompress(base64.b85decode(
    'c-pO5{c_tz4*#D|fzom)pCi$78>i!((vvJX){JcFB)Ls4?a=&KS$m;KbxB(JsAu{hy_f5g9I&MLA(3+KrkYGl?gGCBu)r<=9(<!)'
    '+|z>vG?;R505Dtd0H%H<Yz7y;!{E{vOTe)pvi{BN5JDfq1%r@@WdQzMzFhmCx$BxRjx3kk5@*P5<^}9IJT&37dD4Wz989qN)u)tc'
    'JcP9wfaizc@E{c2ibCeVnuiM{;DN-Ma~DD0`4QNr2fh`W9RGL>%n%k~xC+j7eHky`^u@i7s5-8vH<4QcT;P&|-!DR_+edJEa`Liq'
    'dUAROUE!OK?Jt|qab1`YMF0~PF!8{gCfXs^5&r!Bj}7UczyAjWn==uR6*i+V@a!g-o}>E$cqlCuaEF_MvmiznF}m2GlWYo~!+TI`'
    'lTNogm|g$V=->Qk3`d<_zo$YQTJXy&GLxqO{sapn;lar(WzG}M#^*fq^kOm?&!$FiFi{Jf2k;YvfQ4|(V8H}FbI;!VkK2q&1|iH*'
    'bX73Zak=*a8gmsa=K5>!7;{M31j<6QTP`E*=O%ZXaySN0D0+;H#&Lhvay8mV2yDSuAvqB);kuu|^=*^3y3XfY6bTIX9{dkTFc)ZF'
    '6FmgM5~AlZ2mT``l1SY5Hcn+DJ`+b0Cbo$&?#!;*YVAaogX;x46q#><=F3r_)!-F4v8LO)pqM%1#L=)?`GRynKg2cBVMe_X*e*ja'
    'l>$bqW{P!rI~>Mx+~sld>%-}5-C+;97rE|T%9!}b4=Q}q2KA)4wefG#-+|8K-sMOUjeT4R@|%Q7g>%sK=HE1U4e9bbOKh7m<<8MC'
    'H(=E)cMcbxxnx^5ztc;x(8nua^PsiVs>&9lgXcr)ns~0s=dw^~r*aidMnPtX<*x?w9t_W4*N>DhF*3R>Xvc@|8i&iqp#z6k=ZDwl'
    'hf_^?%yFy)_rjDX9S)Z(G+s;H88uwy;mntV{NQ6u7cBTZlur_M4%%S=^54&%hUTp2<vdbxBo9LGEfrd?pPg=@6@@j>Z&+K?ic{Qk'
    'm=~gC_4>BBa15udqoY_xIz3A8Fp6>cf2hfxq`Q<Tq8`@1Y%TQ|kXkBepyd_GAakmun;dzx%_YQTv++7*Gl^mqQ^oydLhNp@To|<X'
    'IO&UDv{>~5+u9UfH6P7#?>@&#6m(*)%y3aZf|!p^JQrxFU2Daz9p48I^3m|_Az^1z&z`|*?I>|*#eDPqeDy4JCDoAxVfMm@S{<>D'
    '^jh6A16H6>c4AjDB-2U~tEgfcl~<$dK6zSRXQcm97PgOivx#yj{Um59lJlg5^{BJ>zEBlP1E#p24x5{i8wN@fs&IvASv*|++DK`Y'
    '_yRQ_SbV=kLZAb|gXu<$E}iTO9uIoP<zUz+x3|$Cm<!1KhFr<|ggAIJa1RG#xnxFEyBOVE%9~$u`VZ(l++hLnn)jIMJcl)dWrSBW'
    'zxRBBIhvgEFik>tks?KyR5TTm&@py|X{);e#uj;lV%feo=l79_uh0j~Sm=5W;%ir`n-DNSPvye*mZUu8WD4F(A1P5$KV~Zww4oan'
    'J*9jS<th>@nKM?x<=nF{WsI*n(|$YMJd{-uHTQu1_8WvEV)CdzfNr#0L4P)dQ06x-Uvio0zBs{OF5l`;hQ@X0t%0P*tkWI#r$w>w'
    'dc~{&cFEc?QoLF(b}q^z(9iIsa%5_Dj#HbI_8ybq$ZBGGsWai4-o5UJE8TK^OJAA+<|Q~B20B|tt{M75x0op@4O+jsC=q!)n#}e~'
    'D=FG%FJ63mCIv+RF9GO}uX}Pn+0-0oI<-UJ>GdZ4>9m3+nN^ft|FUyCoM9kd^?SF&{>11GZhFS--59;ZvD6K2$vi>hMwW8c_TZ+N'
    'xl^DDd>=`>T&hf`-e5YL47#_o!RW^5-d<iJ-E{D;e!F%0J^4c-Mip5iLM2M3WF^9mrcbQ(Bwc%txS8VQPdB&MU7Q(R8so|60{0rj'
    'pmp-y_b*<aZKSFoOJu16$<(gm-_d&iLQj#kt0k>cxicGGCsCj6a|ubQsxfnwnlaxKD(1oWk+jRDDh<oq={w`6&R}NDt}t}3M#Em)'
    'qbzJUKzAv3L$w60G-yk(_Jwcd(qBj1eo7B{d0zO-CEl2_oEKSQG3`%&L^e9(I+HhO+vAmu$t1U_k#anSC;btQL)g2ca2$kIo~Gpd'
    'f#c7PWx4Ql%p;n$u0Y#WJ*IZ-RrEh{aQ|x=q;(sOhRoOUDF@~zLAHZ`$5(?A&ewEFZ_H15Fg24f;Zn-eli4uco1N?=H%G}yp0pB-'
    '>?-d*<9cvYhFBm-V2ey&gWY-i1a{}`E;cg#RdQ?JzBqe%s{M7M*6VxZ!P2sILZxNtdRrm3z1nsJ*1l|wJnloC&N?W=9^8x|L(azb'
    'Vq?BDp!n+ypAx==GFz==a__Piqh3F4aFr~U&Sy)cL*>AOH#eh6AJ30BgPYklYGJaiQi>hBp?vzkk&Me>=M5gGUEu1b|4A1>Mb(pF'
    '{VcQG_xtFmtyU}JSJEuGSigg|TkuhpXS&pW`;=6BvUtiBdw5KpYl%Gc+apm%q)`t#E;I=h%gTI()1=yrN{F~HbiiV^86{<>AGtVy'
    '5`EZ~q^WJS!*Z2kl}t%Bu<Hul(Fj9!M92B|_T{F~vvO~bn<KuDMJyK3NzLmp4|2zPT8rlTCujtNN*tG4LE1*ie1XDYIOtMT$(cec'
    'BUgP8O};FqZh^jXqkHZJ=NU>#3*+lB^6d=~c}CKQNF`3*sbkSP{~>vcYkZ4s3c^D7Pd+ZqPZl#gpL?rl!(1498lu#a@f3BxXk_l1'
    '_d#22SUz5)wTofDa}#f+boBD86dE6x@R*BkhPd=82Z6Ch^^7I@?7ow}n$i3S`|g-r-%G6(tUxZRCC^%!Cv0oFv0kk(k#3^=OPbON'
    'M0;K&!{s-9rnqgF>mL><3(c<VQ%}1^4S}ir8nebYk3YQ04+!LY@*~P~>UC-%n6}NSdnrGm1fjn|k7Ra4y&FaLGX4uEeKsQ'
))


class RabbitPublicSource(unittest.TestCase):
    def test_actual_complete_public_bytes_accept_and_original_assertions_reject(self):
        self.assertEqual(len(RABBIT_PUBLIC_ENV), 7117)
        self.assertEqual(hashlib.sha256(RABBIT_PUBLIC_ENV).hexdigest(),
                         '4593a53a99e9115feaeddb272e206b1d39c22fb9532f9c143fc682e78c9d62af')
        original = """assert env_source.count('run_escript() {') == 1
route = env_source.split('run_escript() {', 1)[1].split('\\n}', 1)[0]
assert 'tmp_erl_flags="$RABBITMQ_CTL_ERL_ARGS $default_erl_flags ' in route
assert 'ERL_FLAGS="$tmp_erl_flags" "$escript" "$@"' in route"""
        # Execute the exact old assertions; do not merely assert their text exists.
        with self.assertRaises(AssertionError):
            exec(compile(original, 'original-public-source-preflight', 'exec'),
                 {'env_source': RABBIT_PUBLIC_ENV.decode()})
        route = RABBIT_PUBLIC_ENV.split(b'run_escript()\n{', 1)[1].split(b'\n}', 1)[0]
        self.assertNotIn(b'tmp_erl_flags="$RABBITMQ_CTL_ERL_ARGS $default_erl_flags ', route)
        result = recovery.validate_rabbit_cli_source(RABBIT_PUBLIC_ENV)
        self.assertTrue(result['full_source_pin_verified'])
        self.assertTrue(result['ctl_args_route_verified'])

    def test_changed_missing_ctl_callback_and_function_reject(self):
        mutations = {
            'changed_ctl': (b'$RABBITMQ_CTL_ERL_ARGS -kernel', b'$RABBITMQ_SERVER_ERL_ARGS -kernel'),
            'missing_ctl': (b'$RABBITMQ_CTL_ERL_ARGS -kernel', b'-kernel'),
            'changed_callback': (b'ERL_FLAGS="$tmp_erl_flags" "$escript" "$@"',
                                 b'ERL_FLAGS="$tmp_erl_flags" "$escript"'),
            'missing_callback': (b'ERL_FLAGS="$tmp_erl_flags" "$escript" "$@"', b'true'),
            'changed_function': (b'run_escript()\n{', b'run_escript() {'),
            'changed_boot': (b'-boot $CLEAN_BOOT_FILE ', b''),
            'changed_noinput': (b'-noinput $tmp_erl_flags', b'$tmp_erl_flags'),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(name=name):
                self.assertEqual(RABBIT_PUBLIC_ENV.count(old), 1)
                changed = RABBIT_PUBLIC_ENV.replace(old, new)
                with self.assertRaisesRegex(AssertionError, 'source hash drift'):
                    recovery.validate_rabbit_cli_source(changed)
                # Also prove exact semantic-body checks independently of the pin.
                # This test-only repin is never an acceptance route in the runner.
                with patch.object(recovery, 'RABBIT_ENV_SHA', recovery.digest(changed)):
                    with self.assertRaisesRegex(AssertionError, '(definition|route) drift'):
                        recovery.validate_rabbit_cli_source(changed)

    def test_full_source_pin_refuses_unrelated_byte_and_duplicate_definition(self):
        for changed in (RABBIT_PUBLIC_ENV + b'\n',
                        RABBIT_PUBLIC_ENV.replace(b'## Set defaults', b'## Set defaultX'),
                        RABBIT_PUBLIC_ENV + b'\nrun_escript()\n{\ntrue\n}\n'):
            with self.assertRaisesRegex(AssertionError, 'source hash drift'):
                recovery.validate_rabbit_cli_source(changed)
        duplicated = RABBIT_PUBLIC_ENV + b'\nrun_escript()\n{\ntrue\n}\n'
        with patch.object(recovery, 'RABBIT_ENV_SHA', recovery.digest(duplicated)):
            with self.assertRaisesRegex(AssertionError, 'definition drift'):
                recovery.validate_rabbit_cli_source(duplicated)

    def test_installed_path_executes_shared_validator_on_copied_bytes(self):
        source = Path(rig.__file__).read_text()
        tree = ast.parse(source)
        run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'run')
        call = next(n for n in ast.walk(run) if isinstance(n, ast.Assign)
                    and isinstance(n.value, ast.Call)
                    and ast.unparse(n.value.func) == 'recovery.validate_rabbit_cli_source')
        parent = next(n for n in ast.walk(run) if isinstance(n, ast.If) and call in n.body)
        block = parent.body[parent.body.index(call):]
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = types.SimpleNamespace(out=Path(tmp), receipt={})
            (r.out/'rabbitmq-env').write_bytes(RABBIT_PUBLIC_ENV)
            exec(compile(ast.Module(body=block, type_ignores=[]), 'actual-installed-source-preflight', 'exec'),
                 {'self':r, 'recovery':recovery, 'RABBIT':rig.RABBIT})
            self.assertTrue(r.receipt['rabbit_cli_source']['ctl_args_route_verified'])
            self.assertFalse(r.receipt['rabbit_cli_source']['original_exit153_cause_proven'])
            self.assertEqual(r.receipt['rabbit_cli_source']['sha256'], recovery.RABBIT_ENV_SHA)


class Recovery(unittest.TestCase):
    def test_lowercase_missing_volume_is_exact_daemon_absence(self):
        for message in ('Error response from daemon: get owned-volume: no such volume\n',
                        'Error response from daemon: get owned-volume: No such volume\n'):
            result = types.SimpleNamespace(returncode=1, stdout=b'[]\n', stderr=message.encode())
            self.assertTrue(recovery.exact_missing(result, 'volume', 'owned-volume'))

    def test_missing_does_not_hide_real_errors_or_wrong_object(self):
        good = b'Error response from daemon: get owned-volume: no such volume\n'
        for code, stdout, stderr in [
            (0, b'[]', good), (2, b'[]', good), (1, b'[{"Name":"owned-volume"}]', good),
            (1, b'[]', good.replace(b'owned-volume', b'unrelated-volume')),
            (1, b'[]', good+b'permission denied\n'), (1, b'', b'permission denied: no such volume'),
            (1, b'', b'Cannot connect to the Docker daemon'), (1, b'', b'context not found'),
            (1, b'', b'Error response from daemon: remove owned-volume: volume is in use')]:
            with self.subTest(code=code, stderr=stderr):
                result = types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
                self.assertFalse(recovery.exact_missing(result, 'volume', 'owned-volume'))

    def test_parent_cleanup_lowercase_absence_and_daemon_failures_are_not_conflated(self):
        for missing in (True, False):
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                root = Path(tmp); (root/'installed-results').mkdir()
                (root/'installed-results/controller-ledger.jsonl').write_text(json.dumps({'kind': 'volume', 'id': 'owned-volume'})+'\n')
                def command(argv, **kw):
                    if argv[1:3] == ['volume', 'rm']:
                        return types.SimpleNamespace(returncode=0, stdout=b'owned-volume\n', stderr=b'')
                    self.assertEqual(argv, ['docker', 'volume', 'inspect', 'owned-volume'])
                    error = b'Error response from daemon: get owned-volume: no such volume\n' if missing else b'permission denied\n'
                    return types.SimpleNamespace(returncode=1, stdout=b'[]\n', stderr=error)
                with patch.object(rig.subprocess, 'run', side_effect=command), patch.object(rig.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=rig.capacity.RESERVE)):
                    if missing: rig.emergency_cleanup(root, 'a'*40)
                    else:
                        with self.assertRaises(AssertionError): rig.emergency_cleanup(root, 'a'*40)
                proof = json.loads((root/'parent-cleanup.json').read_text())
                self.assertEqual(proof['parent_cleanup_residual'], [] if missing else [{'kind': 'volume', 'id': 'owned-volume'}])

    def test_container_and_network_missing_require_target_and_kind(self):
        for kind, message in [
            ('container', 'Error: No such object: owned'),
            ('container', 'Error response from daemon: No such container: owned'),
            ('network', 'Error response from daemon: network owned not found'),
            ('network', 'Error: No such network: owned')]:
            r = types.SimpleNamespace(returncode=1, stdout=b'', stderr=message.encode())
            self.assertTrue(recovery.exact_missing(r, kind, 'owned'))
            self.assertFalse(recovery.exact_missing(r, kind, 'other'))
            self.assertFalse(recovery.exact_missing(r, 'volume', 'owned'))

    def test_all_erlang_cli_actions_have_service_user_scheduler_and_rpc_deadline(self):
        for action in ('ping', 'check_running', 'list_queues'):
            argv = recovery.broker_argv('a'*64, '999:999', action)
            self.assertEqual(argv[:7], ['docker', 'exec', '--user', '999:999', '--env', recovery.CLI_ENV, 'a'*64])
            self.assertEqual(argv[8:11], ['--timeout', '10', '-q'])
            self.assertIn('+S 2:2', recovery.CLI_ENV)
            self.assertIn('+SDcpu 1 +SDio 1 +A 4', recovery.CLI_ENV)
            self.assertNotIn('RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS', ' '.join(argv))
        cookie = recovery.broker_argv('a'*64, '999:999', 'cookie_metadata')
        self.assertIn('stat -c', cookie[-1]); self.assertNotIn('cat ', cookie[-1])

    def test_readiness_requires_metadata_ping_and_check_running_not_tcp(self):
        r = rig.Rig.__new__(rig.Rig); r.receipt = {}; r.state = lambda cid: {'Running': True}
        for failed in ('cookie_metadata', 'ping', 'check_running', None):
            calls = []
            def cli(cid, user, action):
                calls.append(action)
                if action == failed: raise RuntimeError('synthetic original exit153')
                return '999 999 600 20' if action == 'cookie_metadata' else ''
            r.broker_cli = cli
            self.assertEqual(r.rabbit_ready('a'*64, '999:999'), failed is None)
            if failed is None: self.assertEqual(calls, ['cookie_metadata', 'ping', 'check_running'])
            else: self.assertEqual(calls[-1], failed)
        r.state = lambda cid: {'Running': False}
        with self.assertRaises(RuntimeError): r.rabbit_ready('a'*64, '999:999')

    def test_cli_failure_preserves_exit_action_and_sanitized_bounded_first_latest(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            r = rig.Rig.__new__(rig.Rig); r.out = Path(tmp); r.receipt = {}; r.secrets = ['synthetic-pass']
            outcome = {'exit_code': 153, 'termination_reason': None, 'stdout': b'',
                       'stderr': b'Failed to create scheduler thread 98\nErlang cookie hash: dont-export\npassword=synthetic-pass\n'}
            with patch.object(recovery, 'bounded_cli', return_value=outcome):
                for _ in range(100):
                    with self.assertRaises(RuntimeError): r.broker_cli('a'*64, '999:999', 'ping')
            raw = (r.out/'rabbit-cli.sanitized.json').read_text(); saved = json.loads(raw)['ping']
            self.assertEqual(saved['attempts'], 100); self.assertEqual(saved['failures'], 100)
            self.assertEqual(saved['first_failure']['exit_code'], 153)
            self.assertIn('Failed to create scheduler thread', raw)
            self.assertNotIn('dont-export', raw); self.assertNotIn('synthetic-pass', raw)
            self.assertLess(len(raw), 4096)
            for reason in ('timeout', 'output_cap'):
                with patch.object(recovery, 'bounded_cli', return_value=dict(outcome, exit_code=0, termination_reason=reason)):
                    with self.assertRaises(RuntimeError): r.broker_cli('a'*64, '999:999', 'ping')

    def test_bounded_pipe_collector_preserves_both_streams_and_exit(self):
        result = recovery.bounded_cli([sys.executable, '-c', 'import sys; print("synthetic stdout"); print("synthetic stderr", file=sys.stderr); sys.exit(153)'])
        self.assertEqual(result['exit_code'], 153); self.assertIsNone(result['termination_reason'])
        self.assertEqual(result['stdout'], b'synthetic stdout\n'); self.assertEqual(result['stderr'], b'synthetic stderr\n')

    def test_bounded_pipe_collector_rejects_output_overflow_and_timeout(self):
        for program, reason in [('import os; os.write(2, b"x"*65536)', 'output_cap'),
                                ('import time; time.sleep(5)', 'timeout')]:
            result = recovery.bounded_cli([sys.executable, '-c', program], timeout=.15)
            self.assertEqual(result['termination_reason'], reason)
            self.assertLessEqual(len(result['stdout']), recovery.CLI_OUTPUT_CAP)
            self.assertLessEqual(len(result['stderr']), recovery.CLI_OUTPUT_CAP)

    def test_reuse_compares_complete_non_qa_inventory_bytes_and_modes(self):
        before = {'source': recovery.PRIOR_SOURCE, 'files': {'packages/backend/build.ts': 'a'*64},
                  'modes': {'packages/backend/build.ts': 0o644}}
        raw = json.dumps(before).encode()
        with patch.object(recovery, 'PRIOR_MANIFEST_SHA', recovery.digest(raw)):
            after = copy.deepcopy(before); after['source'] = 'b'*40
            self.assertFalse(recovery.verify_reuse(raw, after)['backend_rebuilt'])
            for field, value in [('files', 'b'*64), ('modes', 0o755)]:
                changed = copy.deepcopy(after); changed[field]['packages/backend/build.ts'] = value
                with self.assertRaises(AssertionError): recovery.verify_reuse(raw, changed)
            for action in ('add', 'delete'):
                changed = copy.deepcopy(after)
                if action == 'add': changed['files']['packages/backend/extra.ts'] = 'c'*64
                else: del changed['files']['packages/backend/build.ts']
                with self.assertRaises(AssertionError): recovery.verify_reuse(raw, changed)

    def test_bundle_restore_only_exact_pinned_four_files_and_rejects_hostile_members(self):
        names = ['main.js', 'main.js.map', 'cpufeatures-e8x7s2f1.node', 'sshcrypto-a3khcfhj.node']
        meta = {'source': recovery.PRIOR_SOURCE, 'built': True,
                'bundle': {'files': {n: {'bytes': 7, 'sha256': recovery.digest(b'example')} for n in names}}}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            for hostile in (None, '../escape', 'link', 'duplicate', 'missing'):
                payload = io.BytesIO()
                with tarfile.open(fileobj=payload, mode='w:gz') as tf:
                    data = json.dumps(meta).encode(); member = tarfile.TarInfo('build-result.json'); member.size = len(data)
                    tf.addfile(member, io.BytesIO(data))
                    for n in names[:-1] if hostile == 'missing' else names:
                        member = tarfile.TarInfo('source/packages/backend/dist/'+n); member.size = 7
                        tf.addfile(member, io.BytesIO(b'example'))
                    if hostile in ('../escape', 'link', 'duplicate'):
                        member = tarfile.TarInfo('build-result.json' if hostile == 'duplicate' else hostile)
                        if hostile == 'link': member.type = tarfile.SYMTYPE; member.linkname = '/unrelated'
                        tf.addfile(member)
                p = root/'archive.tar.gz'; p.write_bytes(payload.getvalue()); dist = root/str(hostile)
                with patch.object(recovery, 'PRIOR_ARCHIVE_SHA', recovery.digest(payload.getvalue())):
                    if hostile is None:
                        self.assertEqual(recovery.restore_bundle(p, dist), meta)
                        self.assertEqual({n.name for n in dist.iterdir()}, set(names))
                    else:
                        with self.assertRaises((AssertionError, KeyError)): recovery.restore_bundle(p, dist)
                        self.assertFalse(dist.exists())
                with patch.object(recovery, 'PRIOR_ARCHIVE_SHA', '0'*64):
                    with self.assertRaises(AssertionError): recovery.restore_bundle(p, root/'wrong-hash')


class RabbitFileLimit(unittest.TestCase):
    def test_precise_fatal_signature_not_generic_153_timeout_or_cap(self):
        good = dict(exit_code=153, termination_reason=None, stdout=b'', stderr=b'File size limit exceeded\n')
        self.assertEqual(recovery.cli_classification(good), 'fatal_file_size_limit')
        for delta in ({'exit_code': 1}, {'stderr': b'Failed to create scheduler thread\n'},
                      {'stderr': b'prefix File size limit exceeded\n'}, {'stdout': b'unexpected'},
                      {'termination_reason': 'timeout'}, {'termination_reason': 'output_cap'}):
            self.assertNotEqual(recovery.cli_classification(dict(good, **delta)), 'fatal_file_size_limit')

    def test_fatal_readiness_escapes_instead_of_116_retries_transient_stays_retryable(self):
        r = rig.Rig.__new__(rig.Rig); r.state = lambda _: {'Running': True}; r.receipt = {}
        for error in (recovery.FatalBrokerCLI('fixed file-limit diagnostic'), RuntimeError('transient')):
            r.broker_cli = lambda *a: (_ for _ in ()).throw(error)
            if isinstance(error, recovery.FatalBrokerCLI):
                with self.assertRaises(recovery.FatalBrokerCLI): r.rabbit_ready('a'*64, '100:101')
            else: self.assertFalse(r.rabbit_ready('a'*64, '100:101'))

    def test_per_role_finite_bound_and_no_other_budget_raise(self):
        self.assertEqual(recovery.role_fsize('rabbit'), 64*2**20)
        self.assertEqual(recovery.role_fsize('default'), 32*2**20)
        with self.assertRaises(AssertionError): recovery.role_fsize('postgres')
        text = inspect.getsource(rig.Rig.create)
        self.assertIn("role='default'", text); self.assertIn('name.endswith(\'-canary-negative\')', text)
        self.assertIn('fsize={limit}:{limit}', text)
        for user in ('100:101', '999:999'):
            options = recovery.rabbit_options('f'*64, user, 'synthetic-test-password')
            self.assertNotIn('--ulimit', options)
            self.assertIn('--memory-swap', options)
            self.assertEqual(options.count('--tmpfs'), 3)
            self.assertIn('/tmp:size=16777216,uid='+user.split(':')[0]+',gid='+user.split(':')[1]+',mode=0700', options)

    def test_numeric_only_limit_metadata_and_fixed_public_file_pins(self):
        for bound in (32*2**20, 64*2**20):
            raw = '100\n101\n'+f'Max file size             {bound} {bound} bytes\n'*2
            self.assertEqual(recovery.validate_limit_metadata(raw, '100:101', bound)['exec_hard_bytes'], bound)
            for bad in (raw.replace('100\n', '0\n'), raw.replace(str(bound), 'unlimited'), raw+'secret\n'):
                with self.assertRaises(AssertionError): recovery.validate_limit_metadata(bad, '100:101', bound)
        paths = ['/opt/rabbitmq/escript/rabbitmqctl', '/opt/rabbitmq/escript/rabbitmq-diagnostics']
        raw = f'{recovery.RABBIT_ESCRIPT_BYTES}\n'*2+''.join(recovery.RABBIT_ESCRIPT_SHA+'  '+p+'\n' for p in paths)
        self.assertEqual(set(recovery.validate_public_files(raw)), set(paths))
        for bad in (raw.replace(str(recovery.RABBIT_ESCRIPT_BYTES), '1'), raw.replace(recovery.RABBIT_ESCRIPT_SHA, 'f'*64), raw+'extra\n'):
            with self.assertRaises(AssertionError): recovery.validate_public_files(bad)
        argv = recovery.broker_argv('a'*64, '100:101', 'limit_metadata')
        self.assertNotIn('cookie', ' '.join(argv)); self.assertNotIn('env ', argv[-1])
        # Exercise fixed metadata shell against THIS test process only, offline.
        out = subprocess.check_output(['sh', '-c', argv[-1]], text=True)
        self.assertEqual(len(out.splitlines()), 4)

    def test_allowlisted_actual_contract_validation_refuses_policy_drift(self):
        # Synthetic source-only projection; real Docker readback awaits queue.
        r = rig.Rig.__new__(rig.Rig)
        value = {'image': rig.RABBIT, 'user': '100:101', 'readonly': True,
            'memory': rig.GIB, 'memory_swap': rig.GIB, 'pids': 256, 'cpus': 2000000000,
            'shmem': 64*rig.MIB, 'network': 'f'*64, 'caps': ['ALL'], 'security': ['no-new-privileges'],
            'ulimits': [{'Name': 'core', 'Soft': 0, 'Hard': 0}, {'Name': 'fsize', 'Soft': recovery.RABBIT_FSIZE, 'Hard': recovery.RABBIT_FSIZE}],
            'tmpfs': {p: f'size={n},uid=100,gid=101,mode=0700' for p, n in
                (('/var/lib/rabbitmq', 512*rig.MIB), ('/var/log/rabbitmq', 16*rig.MIB), ('/tmp', 16*rig.MIB))}}
        def cmd(label, argv):
            self.assertNotIn('.Config.Env', ' '.join(argv)); return json.dumps(value)
        r.cmd = cmd
        self.assertEqual(r.rabbit_contract('a'*64, '100:101', 'f'*64, recovery.RABBIT_FSIZE), value)
        original = copy.deepcopy(value)
        for key, bad in (('image', 'unpinned'), ('user', '0:0'), ('readonly', False), ('memory_swap', 2*rig.GIB),
                         ('pids', 512), ('security', []), ('caps', []), ('tmpfs', {}), ('ulimits', original['ulimits'][:1])):
            value = dict(original, **{key: bad})
            with self.assertRaises((AssertionError, KeyError)): r.rabbit_contract('a'*64, '100:101', 'f'*64, recovery.RABBIT_FSIZE)

    def test_canary_is_before_dependency_assembly_and_installed_contract_is_compared(self):
        text = inspect.getsource(rig.Rig.run)
        self.assertLess(text.index('self.rabbit_limit_canary('), text.index('volumes = {}'))
        self.assertLess(text.index('self.rabbit_limit_canary('), text.index('build_command('))
        self.assertIn("assert self.receipt['rabbit_limit_canary']['passed']", text)
        self.assertIn('installed broker/canary policy drift', text)
        self.assertEqual(text.count('recovery.rabbit_options('), 1)
        canary = inspect.getsource(rig.Rig.rabbit_limit_canary)
        self.assertIn('for negative in (True, False)', canary)
        self.assertIn('recovery.rabbit_options(', canary)
        self.assertNotIn('pull', canary); self.assertNotIn('strace', canary.split('proof =', 1)[1])

    def test_real_canary_control_flow_with_source_only_runtime_seams(self):
        # This is a source-only orchestrator regression, NOT a real RPC claim.
        for invalid in (None, 'wrong_negative', 'missing_running_rpc', 'cleanup'):
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                r = rig.Rig.__new__(rig.Rig); r.root = Path(tmp); r.out = Path(tmp)
                r.owner = 'msi-offline'; r.receipt = {}; r.ids = []; r.secrets = []
                created = []; removed = []
                def create(name, image, options, command, **kw):
                    self.assertEqual(r.ids, [])  # strictly sequential runtime
                    cid = ('a' if kw['baseline'] else 'b')*64
                    r.ids.append(cid); created.append((name, image, list(options), command, kw)); return cid
                def start(cid, label): (r.out/(label+'.sanitized.log')).write_text('Server startup complete; 3 plugins started.\n')
                def wait(label, predicate, timeout): self.assertTrue(predicate())
                r.create = create; r.start = start; r.wait = wait
                r.state = lambda cid: {'Running': True, 'OOMKilled': False}
                r.cmd = lambda label, args: removed.append(args[-1])
                r.rabbit_contract = lambda cid, user, net, bound: {'fixed': True, 'ulimits': [{'Name': 'fsize', 'Soft': bound, 'Hard': bound}]}
                def cli(cid, user, action):
                    if action == 'limit_metadata':
                        bound = recovery.DEFAULT_FSIZE if cid[0] == 'a' else recovery.RABBIT_FSIZE
                        return f'100\n101\n'+f'Max file size {bound} {bound} bytes\n'*2
                    if action == 'public_files':
                        return f'{recovery.RABBIT_ESCRIPT_BYTES}\n'*2+''.join(recovery.RABBIT_ESCRIPT_SHA+'  /opt/rabbitmq/escript/'+p+'\n' for p in ('rabbitmqctl', 'rabbitmq-diagnostics'))
                    if action == 'cookie_metadata': return '100 101 600 20'
                    if cid[0] == 'a':
                        row = {'attempts': 1, 'failures': 1, 'first_failure': {'classification': 'fatal_file_size_limit'}}
                        r.receipt.setdefault('rabbit_cli', {})[action] = row
                        if invalid == 'wrong_negative': raise RuntimeError('not the precise negative')
                        raise recovery.FatalBrokerCLI('exact negative')
                    r.receipt.setdefault('rabbit_cli', {})[action] = {'success': {'exit_code': 0}}
                    if invalid == 'missing_running_rpc' and action == 'check_running': raise RuntimeError('not running')
                    return ''
                r.broker_cli = cli
                # Use the actual readiness implementation and retain metadata success.
                def ready(cid, user):
                    ok = rig.Rig.rabbit_ready(r, cid, user)
                    if ok: r.receipt['rabbit_cli']['cookie_metadata'] = {'success': {'exit_code': 0}}
                    return ok
                r.rabbit_ready = ready
                missing = types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error: No such object: '+b'a'*64)
                def inspect_missing(argv, **kw):
                    target = argv[-1]
                    if invalid == 'cleanup': return types.SimpleNamespace(returncode=0, stdout=target.encode(), stderr=b'')
                    return types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'Error: No such object: '+target.encode())
                try:
                    with patch.object(rig.subprocess, 'run', side_effect=inspect_missing):
                        if invalid:
                            with self.assertRaises((RuntimeError, AssertionError)): r.rabbit_limit_canary('f'*64, '100:101', 'synthetic')
                            self.assertFalse(r.receipt['rabbit_limit_canary']['passed'])
                        else:
                            r.rabbit_limit_canary('f'*64, '100:101', 'synthetic')
                            proof = r.receipt['rabbit_limit_canary']
                            self.assertTrue(proof['passed']); self.assertTrue(proof['causal_limit_ab_proven'])
                            self.assertEqual(len(created), 2); self.assertEqual(created[0][2], created[1][2])
                            self.assertEqual(r.ids, []); self.assertEqual(len(removed), 2)
                            self.assertFalse(proof['original_specific_file_proven'])
                finally:
                    if hasattr(r, 'canary_holder'): os.close(r.canary_holder)


if __name__ == '__main__': unittest.main()
