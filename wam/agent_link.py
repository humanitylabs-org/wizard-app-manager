"""Fresh mode: connect the box's `hermes-agent` app to the Agent Service's `wizard` app gateway.

Alongside mode does this with `wizard link` against the owner's own Hermes. On a fresh box nothing did, so
the agent had no `wizard` MCP server and no pointer skills, and answered "DeFleur isn't installed" while
DeFleur was running (release 0.2.2 end-to-end test). This is the same three-part link, for the app:

  1. `mcp_servers.wizard` -> http://wizard-agent-service:8787/wizard/mcp with `Bearer ${WIZARD_MCP_TOKEN}`,
     and `WIZARD_MCP_TOKEN` in the agent's `.env`. The token is the Agent Service's own (its env or
     <WIZARD_OS_HOME>/mcp-token); the service's config is never changed.
  2. The service's pointer-skill folder mounted read-only at /opt/wizard-os/skills (additive user-config
     override), plus `skills.external_dirs += /opt/wizard-os/skills`.
  3. The `wizard-os` Hermes plugin (as `wizard link` installs it alongside), pinned to one Agent Service commit and
     installed with stock `hermes plugins install` into the agent's own data, plus `plugins.enabled += wizard-os`
     and `WIZARD_ACCOUNT_CHANGES=chatgpt` in its `.env`: the ChatGPT app adds sign-ins through that plugin's
     account broker. Claude stays off the broker: its sign-in is the Claude app's own Claude Code page, the login
     the agent's Claude subscription provider uses (and the one that sets the agent's model).
  4. A restart of the agent when the mount, plugin or config changed, then one live `initialize` from the agent's
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
HERMES_BIN = '/opt/hermes/.venv/bin/hermes'
PLUGIN_NAME = 'wizard-os'
PLUGIN_SOURCE = 'humanitylabs-org/wizard-agent-service#hermes-plugin/wizard-os'
# One immutable commit (never a moving branch): a box gets the plugin bytes this App Manager was tested with.
PLUGIN_REF = '1f78352d20485c04e3583a42c9b85c04e233aece'
# Providers whose sign-ins the agent's account broker may add/reconnect/remove (wizard-os plugin 0.4.2+).
ACCOUNT_CHANGES = 'chatgpt'

# Runs inside the Hermes container as uid 1000. Installs (or refreshes) the plugin this link put there; a plugin it
# did not install, or one changed since, is the owner's and is left alone. Every install/refresh is journalled in the
# marker *before* Hermes runs: a failed or interrupted one is undone (its partial copy removed, the plugin it replaced
# and Hermes' install metadata put back) on failure or on the next run, never left looking like an owner's plugin.
# Prints one JSON line; `managed` says the link owns the plugin (only then is it enabled).
PLUGIN = r'''
import hashlib, json, os, shutil, subprocess, sys, tempfile
hermes, source, ref, name = sys.argv[1:5]
home = os.environ.get('HERMES_HOME') or '/opt/data'
pdir = os.path.join(home, 'plugins')
root, marker = os.path.join(pdir, name), os.path.join(pdir, '.wizard-fresh-link.json')
backup, meta = os.path.join(pdir, '.wizard-fresh-link.backup'), os.path.join(pdir, '.install-metadata.json')
meta_backup = backup + '.metadata.json'
def tree(path):
    h = hashlib.sha256()
    for d, dirs, files in os.walk(path):
        dirs[:] = sorted(x for x in dirs if x not in ('__pycache__', '.git'))
        for f in sorted(files):
            if f.endswith('.pyc'):
                continue
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, path).encode() + b'\0')
            with open(p, 'rb') as fh:
                h.update(fh.read())
            h.update(b'\0')
    return h.hexdigest()
def save(doc):
    fd, tmp = tempfile.mkstemp(prefix='.wizard-fresh-link.', dir=pdir)
    with os.fdopen(fd, 'w') as f:
        json.dump(doc, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, marker)
def undo(journal):
    if os.path.isdir(root):
        shutil.rmtree(root)
    if os.path.isdir(backup):
        os.replace(backup, root)
    if os.path.exists(meta_backup):
        os.replace(meta_backup, meta)
    elif not journal.get('meta_existed') and os.path.exists(meta):
        os.remove(meta)
    if journal.get('previous'):
        save(journal['previous'])
    elif os.path.exists(marker):
        os.remove(marker)
    return journal.get('previous')
try:
    mine = json.load(open(marker))
except (OSError, ValueError):
    mine = None
if mine and mine.get('state') == 'installing':
    mine = undo(mine)   # an earlier run was interrupted mid-install
extra = []
if not os.path.isdir(root):
    state = 'installed'
elif not mine:
    state = 'owner-managed'
elif mine.get('ref') == ref:
    state = 'current'
elif mine.get('sha256') != tree(root):
    state = 'owner-modified'
else:
    state, extra = 'refreshed', ['--force']
if state in ('installed', 'refreshed'):
    os.makedirs(pdir, exist_ok=True)
    for old in (backup, meta_backup):
        if os.path.isdir(old):
            shutil.rmtree(old)
        elif os.path.exists(old):
            os.remove(old)
    journal = {'state': 'installing', 'ref': ref, 'previous': mine if state == 'refreshed' else None,
               'meta_existed': os.path.exists(meta)}
    if journal['meta_existed']:
        shutil.copy2(meta, meta_backup)
    if state == 'refreshed':
        shutil.copytree(root, backup, symlinks=True)
    save(journal)
    try:
        p = subprocess.run([hermes, 'plugins', 'install', source, '--ref', ref, '--no-enable', '--no-deps'] + extra,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        ok = not p.returncode and os.path.isfile(os.path.join(root, 'plugin.yaml'))
        why = (p.stderr or p.stdout)[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        ok, why = False, type(e).__name__
    if not ok:
        undo(journal)
        raise SystemExit('wizard-os plugin not installed (nothing changed): ' + (why.strip() or 'no plugin.yaml'))
    save({'source': source, 'ref': ref, 'sha256': tree(root)})
    for old in (backup, meta_backup):
        if os.path.isdir(old):
            shutil.rmtree(old)
        elif os.path.exists(old):
            os.remove(old)
print(json.dumps({'plugin': state, 'changed': state in ('installed', 'refreshed'),
                  'managed': state in ('installed', 'refreshed', 'current')}))
'''

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
# The wizard-os plugin, once installed in the agent's own plugins folder: enabled unless the owner disabled it.
plugin = os.environ.get('WIZARD_LINK_PLUGIN', '')
has_plugin = bool(plugin) and os.path.isfile(os.path.join(home, 'plugins', plugin, 'plugin.yaml'))
if has_plugin:
    pl = cfg.get('plugins') or {}
    if not isinstance(pl, dict) or not isinstance(pl.get('enabled') or [], list) or not isinstance(pl.get('disabled') or [], list):
        raise SystemExit('plugins is not a mapping of lists; left alone')
    enabled = list(pl.get('enabled') or [])
    if plugin not in enabled and plugin not in (pl.get('disabled') or []):
        pl['enabled'] = enabled + [plugin]
        cfg['plugins'] = pl
        out['config_changed'] = True
if out['config_changed']:
    atomic(cfgp, yaml.safe_dump(cfg, sort_keys=False), 0o600)
lines = open(envp).read().splitlines() if os.path.exists(envp) else []
keys = [l.split('=', 1)[0].strip() for l in lines]
want = list(lines)
cur = [l for l in lines if l.split('=', 1)[0].strip() == 'WIZARD_MCP_TOKEN']
if cur != ['WIZARD_MCP_TOKEN=' + token]:
    want = [l for l in want if l.split('=', 1)[0].strip() != 'WIZARD_MCP_TOKEN'] + ['WIZARD_MCP_TOKEN=' + token]
# The plugin's account broker, for the providers the link names. An owner's own value (even 0) is kept.
changes = os.environ.get('WIZARD_LINK_ACCOUNT_CHANGES', '')
if has_plugin and changes and 'WIZARD_ACCOUNT_CHANGES' not in keys:
    want.append('WIZARD_ACCOUNT_CHANGES=' + changes)
if want != lines:
    atomic(envp, '\n'.join(want) + '\n', 0o600)
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
    from .network import dump_yaml, load_yaml
    override = Path(c.runtipi_root) / 'user-config' / store / AGENT_APP / 'docker-compose.yml'
    # `wam network` writes `networks: !override` into this file: read and write it with the tag-preserving helpers
    # (safe_load can't construct the tag, and a fresh install's agent never got the `wizard` gateway).
    doc = (load_yaml(override.read_text()) or {}) if override.exists() else {}
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
    _atomic(override, dump_yaml(doc))
    return True


def ensure_plugin(docker, engine):
    """Install the pinned wizard-os plugin into the agent's data (stock `hermes plugins install`, as uid 1000).
    Returns {plugin: installed|refreshed|current|owner-managed|owner-modified, changed}. Raises ValueError."""
    q = subprocess.run(docker + ['exec', '-u', '1000:1000', engine, HERMES_PY, '-c', PLUGIN,
                                 HERMES_BIN, PLUGIN_SOURCE, PLUGIN_REF, PLUGIN_NAME],
                       capture_output=True, text=True, timeout=330)
    if q.returncode:
        raise ValueError((q.stderr or q.stdout).strip()[-300:] or 'wizard-os plugin not installed')
    return json.loads(q.stdout.strip().splitlines()[-1])


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
    try:   # the ChatGPT app's sign-ins; the gateway link above never waits on it
        plugin = ensure_plugin(docker, _container(docker, AGENT_APP + '-engine')[0])
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        plugin = {'plugin': 'error', 'error': str(e)[:200], 'changed': False, 'managed': False}

    def edit(probe):
        eng = _container(docker, AGENT_APP + '-engine')[0]
        # Only a plugin this link installed is enabled: an owner's own copy stays as he left it (opt-in).
        env = ['-e', 'WIZARD_LINK_PLUGIN=' + PLUGIN_NAME, '-e', 'WIZARD_LINK_ACCOUNT_CHANGES=' + ACCOUNT_CHANGES] \
            if plugin.get('managed') else []
        q = subprocess.run(docker + ['exec', '-i', '-u', '1000:1000', *env, eng, HERMES_PY, '-c', EDIT,
                                     url, MCP_NAME, SKILLS_TARGET] + (['probe'] if probe else []),
                           input=token + '\n', capture_output=True, text=True, timeout=90)
        if q.returncode:
            raise ValueError('agent config not linked: ' + (q.stderr or q.stdout)[-300:])
        return json.loads(q.stdout.strip().splitlines()[-1])

    first = edit(probe=False)
    changed = mount_changed or plugin['changed'] or first['config_changed'] or first['env_changed']
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
    return {'gateway': url, 'skills_dir': SKILLS_TARGET, 'mount_changed': mount_changed, 'plugin': plugin['plugin'],
            **({'plugin_error': plugin['error']} if plugin.get('error') else {}),
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
