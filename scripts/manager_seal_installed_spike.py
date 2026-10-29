"""Offline installed-lifecycle seam probe; NEVER starts Nest/Docker or admits QA.

This is an executable authority/interface blocker diagnostic, not an installed
backend rig. It pins the real saved output, compares embedded production source,
and evaluates the exact install-event expression with JavaScript semantics.
Exit 2 means demonstrated blocker; exit 1 means invalid evidence/probe failure.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

from manager_seal_bundle import SAVED_BUNDLE, regular_bytes

MIB = 1024**2
ARCHIVE_SHA = '168a63a43777a34a1bf3b5a3fa6b5daef6f52339828f9a761a3a36024754de44'
PINS_SHA = 'a5d930a57350507c50c1767a1afcac85a6288fddcd99560b8dbab1c3dd8bcc16'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def probe(repo, archive, node):
    pins_bytes = regular_bytes(repo/'docs/manager-seal-installed-spike-pins.json', MIB)
    assert sha(pins_bytes) == PINS_SHA, 'source manifest drift'
    pins = json.loads(pins_bytes)
    texts = {}
    for rel, expected in pins['source_sha256'].items():
        payload = regular_bytes(repo/rel, MIB)
        assert sha(payload) == expected, 'source drift: '+rel
        texts[rel] = payload.decode()
    payload = regular_bytes(archive, 128*MIB)
    assert sha(payload) == ARCHIVE_SHA, 'saved archive drift'
    wanted = {'source/packages/backend/dist/'+name: name for name in SAVED_BUNDLE}
    saved = {}; seen = set(); total = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as tf:
        for member in tf:
            assert member.name not in seen
            seen.add(member.name); total += member.size
            assert len(seen) <= 100 and 0 <= member.size <= 32*MIB and total <= 64*MIB
            assert member.isdir() or member.isreg()
            assert not member.linkname and member.sparse is None
            if member.name not in wanted:
                continue
            name = wanted[member.name]
            stream = tf.extractfile(member)
            assert stream is not None
            data = stream.read(member.size+1)
            assert (len(data), sha(data)) == SAVED_BUNDLE[name]
            saved[name] = data
    assert set(saved) == set(SAVED_BUNDLE)
    sm = json.loads(saved['main.js.map'])
    assert len(sm['sources']) == len(sm['sourcesContent'])
    # Compare the exact same code in saved bundle source maps, not just checkout.
    embedded = {}
    for rel in [
        'packages/backend/src/app.service.ts',
        'packages/backend/src/modules/app-lifecycle/app-lifecycle.controller.ts',
        'packages/backend/src/modules/app-lifecycle/handlers/install-app.handler.ts',
        'packages/backend/src/modules/docker/docker.module.ts',
    ]:
        key = '../src/'+rel.split('/src/', 1)[1]
        assert sm['sources'].count(key) == 1
        content = sm['sourcesContent'][sm['sources'].index(key)]
        assert content == texts[rel], 'embedded source drift: '+rel
        embedded[key] = sha(content.encode())
    app = texts['packages/backend/src/app.service.ts']
    main = texts['packages/backend/src/main.ts']
    docker = texts['packages/backend/src/modules/docker/docker.module.ts']
    controller = texts['packages/backend/src/modules/app-lifecycle/app-lifecycle.controller.ts']
    handler = texts['packages/backend/src/modules/app-lifecycle/handlers/install-app.handler.ts']
    assert 'await this.docker.pruneNetworks();' in app
    assert app.index('await this.databaseService.migrate();') < app.index('await this.docker.pruneNetworks();') < app.index('await this.copyAssets();')
    assert 'await appService.bootstrap();' in main
    assert 'new Dockerode()' in docker
    assert 'this.appLifecycleService.installApp({ appUrn: castAppUrn(urn), form: body })' in controller
    assert 'const { form, skipRun } = params ?? { form: {} };' in handler
    expression = re.findall(r'form: (\{ \.\.\.parsedForm, skipRun: skipRun \?\? false \})', handler)
    assert len(expression) == 1
    # Pure expression only: no mocked service, fake queue, fake DB or handler.
    code = ('const parsedForm={skipRun:true,skipPull:true,openPort:false}; '
            'const skipRun=undefined; const eventForm='+expression[0]+'; '
            'if(eventForm.skipRun!==false)process.exit(1); '
            'console.log(JSON.stringify({dto_skipRun:parsedForm.skipRun,event_skipRun:eventForm.skipRun}));')
    observed = json.loads(subprocess.check_output([node, '--input-type=module', '-e', code], timeout=5))
    assert observed == {'dto_skipRun': True, 'event_skipRun': False}
    return {
        'schema': 1, 'kind': 'offline exact-source authority/interface blocker',
        'production_source': pins['production_source'],
        'source_manifest_sha256': sha(pins_bytes), 'runner_sha256': sha(Path(__file__).read_bytes()),
        'archive_sha256': ARCHIVE_SHA, 'bundle_files_verified': sorted(saved),
        'embedded_sources_verified': embedded, 'source_files_verified': len(texts),
        'javascript_install_expression': observed,
        'blockers': [
            {'code': 'UNSCOPED_BOOTSTRAP_NETWORK_PRUNE', 'source': 'packages/backend/src/app.service.ts:37-40',
             'operation': 'Dockerode.pruneNetworks()', 'ownership_filter': False,
             'effect': 'Host daemon-wide unused-network mutation exceeds synthetic-only authority.'},
            {'code': 'HTTP_INSTALL_SKIPRUN_OVERWRITTEN', 'source': 'packages/backend/src/modules/app-lifecycle/handlers/install-app.handler.ts:81-89',
             'effect': 'install-then-enroll sequence cannot suppress pre-enrollment up over HTTP.'},
        ],
        'native_load_tested': False, 'nest_bootstrap_tested': False,
        'installed_backend_http_transaction_pass': False, 'single_accepted_update_pass': False,
        'rollback_transaction_pass': False, 'successive_accepted_updates_pass': False,
        'official_image_pass': False, 'full_docker_acceptance': False, 'RUNTIPI_ADMITTED': False,
        'heavy_submitted': False, 'docker_calls': 0, 'resources_created': [], 'cleanup_required': False,
        'stopped_at': 'source-bound authority/interface preflight; no installed rig staged',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--node', default='node')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = probe(Path(__file__).resolve().parents[1], args.archive, args.node)
    # Never replace the original failed receipts or a previous diagnostic.
    with args.output.open('x') as out:
        out.write(json.dumps(result, sort_keys=True, indent=2)+'\n')
    print(json.dumps({'output': str(args.output), 'sha256': sha(args.output.read_bytes()),
                      'demonstrated_blockers': len(result['blockers']), 'installed_pass': False}))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
