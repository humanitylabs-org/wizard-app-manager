#!/usr/bin/python3
"""Read-only committed synthetic-state fence for Service-only recovery.

Never invokes main, Transaction, relocation, request creation or cold init.
Only this rig's finite synthetic sentinels and root-controlled metadata are read.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import runpy
import stat

ROOT = Path('/root/storage-service-continuation')
PRODUCT = Path('/usr/local/lib/wizard-storage/protected-agent-storage.py')
BOOT = PRODUCT.with_name('maintenance-init')
PROOF = Path('/root/storage-production')


def fingerprint(p):
    s = p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_nlink == 1
    return {'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
            'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}


def committed(binding):
    assert os.geteuid() == 0
    assert Path('/run/wizard-storage-production-synthetic').is_file()
    for p, key in ((PRODUCT,'product_sha256'),(BOOT,'bootstrap_sha256')):
        s = p.lstat()
        assert stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022
        assert fingerprint(p)['sha256'] == binding[key]
    product = runpy.run_path(str(PRODUCT))
    a = pwd.getpwnam('hermes'); logical = Path('/home/hermes/.hermes')
    mapping = product['require_mapping']('hermes',str(logical))
    request = Path('/etc/wizard-agent-storage/request.json')
    assert not os.path.lexists(request), 'committed request must remain retired'
    completion = request.parent/'completed.json'
    journal = Path('/var/lib/wizard-agent-storage')/str(a.pw_uid)/'journal.json'
    # The exact retirement metadata predates the failed image-loading step.
    retired = json.loads((PROOF/'retired-before.json').read_text())
    assert [fingerprint(p) for p in (completion,journal)] == retired
    assert product['protected_json'](completion) == {
        'action':'relocate','agent':'hermes','logical':str(logical)}
    doc = product['protected_json'](journal)
    assert doc['phase'] == 'committed' and doc.get('direction','forward') == 'forward'
    assert doc['uid'] == a.pw_uid and doc['gid'] == a.pw_gid
    assert doc['logical'] == str(logical) and doc['physical'] == mapping['physical']
    assert doc['target_identity'] == mapping['identity']
    original = json.loads((PROOF/'original-agent.json').read_text())
    sentinel = fingerprint(logical/'storage-production-sentinel')
    image = fingerprint(logical/'cache/images/storage-production.png')
    assert sentinel == original['sentinel'] and image == original['image']
    mapping_path = Path('/var/lib/wizard-agent-homes/.mappings')/(str(a.pw_uid)+'.json')
    return {'request_absent':True,'completion':fingerprint(completion),'journal':fingerprint(journal),
            'mapping':mapping,'mapping_file':fingerprint(mapping_path),
            'logical_link':os.readlink(logical),'sentinel':sentinel,'image':image,
            'product':fingerprint(PRODUCT),'bootstrap':fingerprint(BOOT)}


if __name__ == '__main__':
    import sys
    binding = json.loads((ROOT/'binding.json').read_text())
    result = committed(binding)
    target = ROOT/'committed-before.json'
    if sys.argv[1] == 'before':
        assert not target.exists()
        target.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    elif sys.argv[1] == 'after':
        assert result == json.loads(target.read_text()), 'Service checks changed committed storage metadata'
        (ROOT/'committed-after.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    else: raise AssertionError('unknown finite observation')
