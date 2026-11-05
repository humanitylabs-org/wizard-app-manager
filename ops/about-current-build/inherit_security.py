#!/usr/bin/env python3
"""Hash-bound reuse of accepted security COMPONENTS, never the failed aggregate/UI."""
import argparse,ast,hashlib,json,shutil
from pathlib import Path

def sha(b):return hashlib.sha256(b).hexdigest()
def read(p):return json.loads(p.read_text())
def logger(p):
    s=p.read_text();return s[s.index('    log default {'):s.index('    admin off')].encode()
def security_code(p):
    s=p.read_text();lines=s.splitlines(keepends=True);tree=ast.parse(s)
    names={'error_headers','redaction_pair','fixture','PROOF','CONTROL','PROBE_HEADERS'}
    chunks={}
    for n in tree.body:
        name=n.name if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) else n.targets[0].id if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) else ''
        if name in names:chunks[name]=''.join(lines[n.lineno-1:n.end_lineno])
    assert set(chunks)==names
    return json.dumps(chunks,sort_keys=True).encode()

def verify(root):
    pin=read(root/'security-inheritance.json');old=root/'inherited/r10'
    for n,h in pin['artifacts'].items():assert sha((old/n).read_bytes())==h,'inherited evidence changed '+n
    m=read(old/'manifest.json')
    assert sha((old/'manifest.json').read_bytes())==pin['prior_manifest_sha256']
    for n in ['forum_qa.py','test_forum_qa.py','metadata.Caddyfile','Caddyfile.candidate','provenance.json','caddy-private-gateway']:
        assert sha((old/n).read_bytes())==m['inputs'][n],'prior source closure drift '+n
    receipt=read(old/'results-candidate/receipt.json');fixture=read(old/'results-candidate/fixture-results.json');unit=read(old/'results-candidate/source-regressions.stdout')
    assert receipt['accepted'] is False and receipt['manifest_sha256']==pin['prior_manifest_sha256']
    assert receipt['failure']=='AssertionError: browser failed: inspect synthetic result only'
    assert receipt['fixture']==fixture and receipt['source_regressions']==unit
    assert fixture['accepted'] and fixture['all_processes_stopped'] and fixture['check_count']==len(fixture['checks'])==43
    assert all(x['pass'] for x in fixture['checks'])
    assert sha((old/'results-candidate/fixture.log').read_bytes())==fixture['log_sha256']
    assert unit['accepted'] and unit['test_count']==len(unit['expected_names'])==11 and not any(unit[k] for k in ['skipped','failures','errors'])
    closure={
      'security_function_bytes':sha(security_code(root/'forum_qa.py')),
      'default_logger_bytes':sha(logger(root/'Caddyfile.candidate')),
      'auth_route_template_bytes':sha((root/'metadata.Caddyfile').read_bytes()),
      'caddy_binary_bytes':sha((root/'caddy-private-gateway').read_bytes()),
      'unit_source_bytes':sha((root/'test_forum_qa.py').read_bytes()),
    }
    prior={
      'security_function_bytes':sha(security_code(old/'forum_qa.py')),
      'default_logger_bytes':sha(logger(old/'Caddyfile.candidate')),
      'auth_route_template_bytes':sha((old/'metadata.Caddyfile').read_bytes()),
      'caddy_binary_bytes':sha((old/'caddy-private-gateway').read_bytes()),
      'unit_source_bytes':sha((old/'test_forum_qa.py').read_bytes()),
    }
    assert closure==prior==pin['unchanged_security_closure'],'security inheritance does not apply'
    # New carriers bind the current SHA; only the exact canonical writer is reused.
    assert read(root/'provenance.json')['generator']['line_sha256']==read(old/'provenance.json')['generator']['line_sha256']
    return {'accepted':True,'inherited':True,'prior_aggregate_accepted':False,'prior_manifest_sha256':pin['prior_manifest_sha256'],'prior_receipt_sha256':pin['artifacts']['results-candidate/receipt.json'],'fixture':fixture,'source_regressions':unit,'unchanged_security_closure':closure,'current_ui_inherited':False,'current_source_capture_required':True}

def prepare(root,prior):
    old=root/'inherited/r10';old.mkdir(parents=True,exist_ok=False)
    names=['manifest.json','forum_qa.py','test_forum_qa.py','metadata.Caddyfile','Caddyfile.candidate','provenance.json','source/main_compare.py','caddy-private-gateway','results-candidate/receipt.json','results-candidate/fixture-results.json','results-candidate/source-regressions.stdout','results-candidate/fixture.log']
    for n in names:
        p=old/n;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(prior/n,p)
    pin={'prior_root':str(prior),'prior_manifest_sha256':sha((old/'manifest.json').read_bytes()),'artifacts':{n:sha((old/n).read_bytes()) for n in names},'unchanged_security_closure':{'security_function_bytes':sha(security_code(old/'forum_qa.py')),'default_logger_bytes':sha(logger(old/'Caddyfile.candidate')),'auth_route_template_bytes':sha((old/'metadata.Caddyfile').read_bytes()),'caddy_binary_bytes':sha((old/'caddy-private-gateway').read_bytes()),'unit_source_bytes':sha((old/'test_forum_qa.py').read_bytes())}}
    (root/'security-inheritance.json').write_text(json.dumps(pin,indent=2)+'\n')
    result=verify(root)
    return {'inheritance_verified':result['accepted'],'prior_aggregate_accepted':False,'fixture_checks':43,'unit_checks':11,'current_ui_inherited':False,'prior_receipt_sha256':result['prior_receipt_sha256']}

def comparison_proof(root,actual,fresh):
    """Match actual cached metadata, not a different instant of mutable GitHub main.

    Prefer the fresh deployed-module result. Otherwise accept ONLY the identical
    live cached response independently proven by that same module in r10. Never
    rebuild a GitHub response from desired counts or inherit rendering evidence.
    """
    fields=['commit','base','behind','own']
    if 'error' not in actual and 'error' not in fresh and all(actual.get(k)==fresh.get(k) for k in fields):
        return {'mode':'fresh-deployed-module','expected':fresh,'same_input_module_equal':True}
    component=verify(root);old=root/'inherited/r10';m=read(old/'manifest.json')
    h=m['inputs']['source/main_compare.py']
    assert sha((old/'source/main_compare.py').read_bytes())==h==sha((root/'source/main_compare.py').read_bytes()),'comparison module differs from cached snapshot proof'
    previous=read(old/'results-candidate/receipt.json')['capture']
    assert previous['same_input_module_equal'] is True
    assert actual==previous['comparison'],'live comparison is not the exact previously module-proven cached response'
    expected=previous['module_comparison']
    assert 'error' not in actual and all(actual.get(k)==expected.get(k) for k in fields)
    assert actual['commit']==read(root/'manifest.json')['commit']==previous['source_commit']
    return {'mode':'identical-live-cache-with-prior-exact-module-proof','expected':expected,'same_input_module_equal':True,'fresh_uncached_module':fresh,'prior_receipt_sha256':component['prior_receipt_sha256'],'module_sha256':h,'live_cached_response_unchanged_including_checked_at':True,'current_ui_inherited':False}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--prior',type=Path);a=p.parse_args()
    x=prepare(a.root,a.prior) if a.prior else verify(a.root)
    print(json.dumps({k:v for k,v in x.items() if k not in ['fixture','source_regressions']}))
