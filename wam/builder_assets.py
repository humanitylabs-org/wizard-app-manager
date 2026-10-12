"""Deterministic reference-template source packaging for Runtipi (no public registry)."""
import base64
import json
from pathlib import Path
import yaml


def package_inline(root):
    root = Path(root)
    # Every file of the view ships, so a new kit file or app asset can never be silently left out.
    files = ['app/server.py', 'openapi.json'] + sorted(
        'web/' + p.name for p in (root / 'web').iterdir() if p.is_file() and not p.name.startswith('.'))
    payload = {p: (root / p).read_text() for p in files}
    blob = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    code = ("import base64,json,pathlib,runpy\n"
            "root=pathlib.Path('/tmp/wizard-app');root.mkdir(exist_ok=True)\n"
            f"files=json.loads(base64.b64decode('{blob}'))\n"
            "for name,text in files.items():\n"
            " p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)\n"
            "runpy.run_path(str(root/'app/server.py'),run_name='__main__')\n")
    doc = yaml.safe_load((root / 'wizard.md').read_text().split('---', 2)[1])
    compose = {'services': {doc['id']: {'image': 'python:3.12-alpine', 'user': '1000:1000',
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'mem_limit': '128m', 'tmpfs': ['/tmp'], 'environment': {'PORT':'8799','DATA_DIR':'/data'},
        'volumes': ['${APP_DATA_DIR}/data:/data'], 'read_only': True, 'command': ['python', '-c', code],
        'x-runtipi': {'is_main': True, 'internal_port':8799}}},
        'x-runtipi': {'schema_version':2, 'overrides':[]}}
    (root / 'runtipi/docker-compose.yml').write_text(yaml.safe_dump(compose, sort_keys=False, width=100000))
