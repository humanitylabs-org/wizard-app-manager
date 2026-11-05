#!/usr/bin/env python3
"""Seal an already copied/hash-verified immutable origin fixture; never submit work."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def sha(b):return hashlib.sha256(b).hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--worker',required=True);p.add_argument('--ops-commit',required=True);a=p.parse_args()
    root=a.root.resolve();assert re.fullmatch('[0-9a-f]{40}',a.ops_commit)
    assert a.worker.startswith('/srv/work/ab-origin-') and '..' not in Path(a.worker).parts
    assert not (root/'manifest.json').exists(),'immutable seal: use another named packet after a real defect, never overwrite'
    source=json.loads((root/'source/provenance.json').read_text())
    required=['origin_qa.py','activate_origin.py','metadata.Caddyfile','metadata.live.Caddyfile','source/release.tar','source/raw.tar','source/main_compare.py','source/comparison-input.json','source/comparison.json','source/default-logger.caddy','source/static-headers.conf','playwright-path.txt','deps-closure.json','caddy-private-gateway']
    for n in required:assert (root/n).is_file(),n
    inputs={str(f.relative_to(root)):sha(f.read_bytes()) for f in sorted(root.rglob('*')) if f.is_file() and f.relative_to(root).parts[0] not in {'node','browser'} and '__pycache__' not in f.parts}
    assert not any(n.startswith(('results-','runtime-','owner','carrier/')) for n in inputs)
    manifest={'schema':1,'fixture_commit':source['fixture_commit'],'ops_source_commit':a.ops_commit,'inputs':inputs,'parent_admission_required':True,'live_head_not_an_eligibility_pin':True}
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');digest=sha((root/'manifest.json').read_bytes())
    spec={'host':'forum','lane':'light','key':'about-origin-'+source['fixture_commit'][:12]+'-'+digest[:12],'cwd':a.worker,'argv':['python3','-B',a.worker+'/origin_qa.py','--manifest-sha256',digest],'timeout_seconds':900}
    handoff={'status':'immutable-origin-candidate-ready-for-parent-native-admission','ready_for_parent_admission':True,'runtime_accepted':False,'activation_eligible':False,'native_job_submitted':False,'native_browser_or_container_tests_run_by_child':False,'live_modified':False,'fixture_commit':source['fixture_commit'],'ops_source_commit':a.ops_commit,'manifest_sha256':digest,'sealed_input_count':len(inputs),'dependency_file_count':len(json.loads((root/'deps-closure.json').read_text())),'root':str(root),'worker':a.worker,'one_parent_job':spec,'live_acceptance_pending':True,'publication_caveat':source['publisher_caveat'],'parent_must_read_receipt_and_screenshots':True,'candidate_not_invalidated_by_unrelated_live_release':True}
    (root/'parent-handoff.json').write_text(json.dumps(handoff,indent=2)+'\n')
    print(json.dumps(handoff))

if __name__=='__main__':main()
