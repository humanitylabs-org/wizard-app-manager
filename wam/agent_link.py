"""Fresh mode: connect the box's `hermes-agent` app to the Agent Service's `wizard` app gateway.

Alongside mode does this with `wizard link` against the owner's own Hermes. On a fresh box nothing did, so
the agent had no `wizard` MCP server and no pointer skills, and answered "DeFleur isn't installed" while
DeFleur was running (release 0.2.2 end-to-end test). This is the same three-part link, for the app:

  1. `mcp_servers.wizard` -> http://wizard-agent-service:8787/wizard/mcp with `Bearer ${WIZARD_MCP_TOKEN}`,
     and `WIZARD_MCP_TOKEN` in the agent's `.env`. The token is the Agent Service's own (its env or
     <WIZARD_OS_HOME>/mcp-token); the service's config is never changed.
  2. The service's pointer-skill folder mounted read-only at /opt/wizard-os/skills (additive user-config
     override), plus `skills.external_dirs += /opt/wizard-os/skills`.
  3. A restart of the agent when the mount or config changed, then one live `initialize` from the agent's
     container to the gateway.

Hermes' files are edited inside its own container as its own user (uid 1000), so host ownership of the app
data never matters. An existing `mcp_servers.wizard` that is not a Wizard gateway is left alone and reported.
Idempotent: a second run changes nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

AGENT_APP = 'hermes-agent'
SERVICE_APP = 'wizard-agent-service'
GATEWAY_URL = 'http://wizard-agent-service:8787/wizard/mcp'
SKILLS_TARGET = '/opt/wizard-os/skills'
MCP_NAME = 'wizard'
HERMES_PY = '/opt/hermes/.venv/bin/python'

# Runs inside the Hermes container as uid 1000. Token on stdin (first line); prints one JSON line.
EDIT = r'''
import json, os, sys, tempfile, urllib.request
import yaml
token = sys.stdin.readline().strip()
url, name, skills = sys.argv[1], sys.argv[2], sys.argv[3]
home = os.environ.get('HERMES_HOME') or '/opt/data'
cfgp, envp = os.path.join(home, 'config.yaml'), os.path.join(home, '.env')
def atomic(path, text, mode):
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path), dir=home)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else mode)
    os.replace(tmp, path)
out = {'config_changed': False, 'env_changed': False}
raw = open(cfgp).read() if os.path.exists(cfgp) else ''
cfg = yaml.safe_load(raw) or {}
if not isinstance(cfg, dict):
    raise SystemExit('config.yaml is not a mapping; left alone')
servers = cfg.get('mcp_servers') or {}
if not isinstance(servers, dict):
    raise SystemExit('mcp_servers is not a mapping; left alone')
want = {'url': url, 'headers': {'Authorization': 'Bearer ${WIZARD_MCP_TOKEN}'}}
have = servers.get(name)
if have is not None and not (isinstance(have, dict) and '/wizard/mcp' in str(have.get('url', ''))):
    raise SystemExit("an MCP server called '%s' that is not Wizard OS exists; left it alone" % name)
if have != want:
    servers[name] = want
    cfg['mcp_servers'] = servers
    out['config_changed'] = True
sk = cfg.get('skills') or {}
if not isinstance(sk, dict) or not isinstance(sk.get('external_dirs', []) or [], list):
    raise SystemExit('skills.external_dirs is not a list; left alone')
dirs = list(sk.get('external_dirs') or [])
if skills not in dirs:
    sk['external_dirs'] = dirs + [skills]
    cfg['skills'] = sk
    out['config_changed'] = True
if out['config_changed']:
    atomic(cfgp, yaml.safe_dump(cfg, sort_keys=False), 0o600)
lines = open(envp).read().splitlines() if os.path.exists(envp) else []
cur = [l for l in lines if l.split('=', 1)[0].strip() == 'WIZARD_MCP_TOKEN']
if cur != ['WIZARD_MCP_TOKEN=' + token]:
    keep = [l for l in lines if l.split('=', 1)[0].strip() != 'WIZARD_MCP_TOKEN']
    atomic(envp, '\n'.join(keep + ['WIZARD_MCP_TOKEN=' + token]) + '\n', 0o600)
    out['env_changed'] = True
if len(sys.argv) > 4 and sys.argv[4] == 'probe':
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'wam-agent-link', 'version': '1'}}}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out['gateway_status'] = r.status
    except Exception as e:
        out['gateway_status'] = getattr(e, 'code', None) or type(e).__name__
out['skills_visible'] = sorted(n for n in (os.listdir(skills) if os.path.isdir(skills) else []) if n.startswith('wizard-job-'))
print(json.dumps(out))
'''


def _docker():
    """`docker`, or `sudo -n docker` on a fresh box before group membership is active (as doctor does)."""
    try:
        subprocess.run(['docker', 'ps', '-q'], capture_output=True, text=True, timeout=15, check=True)
        return ['docker']
    except (OSError, subprocess.CalledProcessError):
        return ['sudo', '-n', 'docker']


def _container(docker, service):
    """(id, compose service name) of the one running container whose compose service is `service`
    (Runtipi may suffix it, e.g. hermes-agent-engine). The compose service name is its network alias."""
    p = subprocess.run(docker + ['ps', '--format', '{{.ID}} {{.Label "com.docker.compose.service"}}'],
                       capture_output=True, text=True, timeout=15, check=True)
    rows = [l.split(None, 1) for l in p.stdout.splitlines() if len(l.split(None, 1)) == 2]
    hits = [r for r in rows if r[1] == service] or [r for r in rows if r[1].startswith(service + '-')]
    if len(hits) != 1:
        raise ValueError(f'expected one running {service}; found {len(hits)}')
    return hits[0][0], hits[0][1]


def skills_mount(c, store='wizardapps'):
    return {'type': 'bind', 'source': str(Path(c.runtipi_root) / 'app-data' / store / SERVICE_APP / 'data/wizard-os/skills'),
            'target': SKILLS_TARGET, 'read_only': True}


def add_skills_mount(c, store='wizardapps'):
    """Additive user-config override on the agent's engine service. True when it changed."""
    from .builder_bundle import _atomic
    override = Path(c.runtipi_root) / 'user-config' / store / AGENT_APP / 'docker-compose.yml'
    doc = (yaml.safe_load(override.read_text()) or {}) if override.exists() else {}
    service = doc.setdefault('services', {}).setdefault(AGENT_APP + '-engine', {})
    vols = service.setdefault('volumes', [])
    mount = skills_mount(c, store)
    for v in vols:
        target = v.get('target') if isinstance(v, dict) else (str(v).split(':')[1] if ':' in str(v) else '')
        if target == SKILLS_TARGET and v != mount:
            raise ValueError(f'{SKILLS_TARGET} mount collision in {override}; preserving override')
    if mount in vols:
        return False
    if not Path(mount['source']).is_dir():
        raise ValueError(f"{mount['source']} is missing: start the Agent Service once so it writes its skills")
    vols.append(mount)
    _atomic(override, yaml.safe_dump(doc, sort_keys=False))
    return True


def link(c, store='wizardapps', restart=True):
    """Connect the hermes-agent app to the gateway. Returns a receipt without secrets."""
    from . import alongside
    if alongside.load(c.home):
        raise ValueError('alongside mode: the owner\'s Hermes is connected with `wizard link`')
    docker = _docker()
    svc, alias = _container(docker, SERVICE_APP)
    url = GATEWAY_URL.replace('wizard-agent-service', alias, 1)
    p = subprocess.run(docker + ['exec', svc, 'sh', '-c',
                                 'if [ -n "$WIZARD_MCP_TOKEN" ]; then printf %s "$WIZARD_MCP_TOKEN"; '
                                 'else cat "${WIZARD_OS_HOME:-/state/wizard-os}/mcp-token"; fi'],
                       capture_output=True, text=True, timeout=30)
    token = p.stdout.strip()
    if p.returncode or not token:
        raise ValueError('the Agent Service has no gateway token yet (start it once, then retry)')
    mount_changed = add_skills_mount(c, store)

    def edit(probe):
        eng = _container(docker, AGENT_APP + '-engine')[0]
        q = subprocess.run(docker + ['exec', '-i', '-u', '1000:1000', eng, HERMES_PY, '-c', EDIT,
                                     url, MCP_NAME, SKILLS_TARGET] + (['probe'] if probe else []),
                           input=token + '\n', capture_output=True, text=True, timeout=90)
        if q.returncode:
            raise ValueError('agent config not linked: ' + (q.stderr or q.stdout)[-300:])
        return json.loads(q.stdout.strip().splitlines()[-1])

    first = edit(probe=False)
    changed = mount_changed or first['config_changed'] or first['env_changed']
    restarted = False
    if changed and restart:
        rt = c.runtipi()
        urn = f'{AGENT_APP}:{store}'
        rt.req('POST', f'/app-lifecycle/{urn}/stop', {}, timeout=300)
        rt.wait_status(urn, {'stopped'}, timeout=600)
        rt.req('POST', f'/app-lifecycle/{urn}/start', {}, timeout=300)
        rt.wait_status(urn, {'running'}, timeout=900)
        restarted = True
    check = edit(probe=True)
    return {'gateway': url, 'skills_dir': SKILLS_TARGET, 'mount_changed': mount_changed,
            'config_changed': first['config_changed'], 'env_changed': first['env_changed'],
            'restart_required': changed and not restarted, 'restarted': restarted,
            'gateway_status': check.get('gateway_status'), 'skills_visible': check.get('skills_visible'),
            'linked': check.get('gateway_status') == 200 and not check['config_changed'] and not check['env_changed']}


def main(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog='wam agent-link', description=(__doc__ or '').split('\n\n')[0])
    ap.add_argument('--no-restart', action='store_true', help='leave the agent running; report restart_required')
    a = ap.parse_args(argv)
    from .cli import Config
    try:
        out = link(Config(), restart=not a.no_restart)
    except (OSError, ValueError, subprocess.SubprocessError, yaml.YAMLError) as e:
        print(f'agent-link: {e}', file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0 if out['linked'] or out['restart_required'] else 1
