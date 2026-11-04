"""Validated, credential-free app-provided agent runtime declaration."""
import re

ID = re.compile(r'[a-z0-9][a-z0-9-]{1,40}')

def agent_runtime(manifest):
    if 'agent_runtime' not in manifest:
        return None
    r = manifest['agent_runtime']
    if not isinstance(r, dict) or set(r) != {'id', 'label', 'harness', 'port', 'model'}:
        raise ValueError('agent_runtime requires id, label, harness, port and model')
    if not all(isinstance(r[k], str) and ID.fullmatch(r[k]) for k in ('id', 'harness')) or not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', r['id']) or r['id'] in {'main', 'hermes', 'default', 'new', 'options'}:
        raise ValueError('invalid agent_runtime identity')
    if not isinstance(r['label'], str) or not r['label'].strip() or len(r['label']) > 80 or any(ord(c) < 32 for c in r['label']):
        raise ValueError('invalid agent_runtime label')
    if type(r['port']) is not int or not 1024 <= r['port'] <= 65535:
        raise ValueError('invalid agent_runtime port')
    m = r['model']
    if not isinstance(m, dict) or set(m) != {'provider', 'model'} or not all(isinstance(v, str) and 0 < len(v) <= 120 and not any(ord(c) < 32 for c in v) for v in m.values()):
        raise ValueError('invalid agent_runtime model')
    if manifest.get('type') != 'agent' or manifest.get('privacy', 'standard') != 'standard':
        raise ValueError('app runtimes currently require type agent and standard privacy')
    return r
