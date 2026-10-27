"""Protected alongside layout. This module never opens the agent's home."""
from pathlib import Path
import json
import re

BASE = Path('/var/lib/wizard-os')


def enabled(home):
    return (Path(home) / 'privsep.json').is_file()


def service_override(st, wizard_home, stt_origin=None):
    # Identity and paths were selected once by root, not discovered from agent config.
    a = st['agent']
    uid, gid = int(a['uid']), int(a['gid'])
    hh = a['hermes_home']
    state = f'/var/lib/wizard-agent/{uid}'
    stt_origin = stt_origin or 'http://127.0.0.1:9'
    if not re.fullmatch(r'http://127\.0\.0\.1:\d{2,5}', stt_origin):
        raise ValueError('Transcriber must have a loopback origin')
    checkout = BASE / 'wizard-app-manager'
    from .alongside import image_lines, service_port
    # Reset the recipe's mounts/command/entrypoint. Only protected wiring is read
    # by Docker; the launcher reads link.json *after* dropping to the agent uid.
    return f'''services:
  wizard-agent-service:{image_lines(st, 'wizard-agent-service')}
    user: "{uid}:{gid}"
    cap_drop: !override [ALL]
    group_add: !override ["{int(st.get('files_gid', gid))}"]
    security_opt: !override [no-new-privileges:true]
    network_mode: host
    networks: !reset []
    ports: !reset []
    entrypoint: !override [node, /privsep/agent-service-start.mjs]
    command: !reset []
    environment:
      WIZARD_HOST: 127.0.0.1
      WIZARD_PORT: "{service_port(st)}"
      WIZARD_CONTAINER: "0"
      WIZARD_SETUP_MODE: alongside
      WIZARD_APPS_JSON_PATH: /registry/apps.json
      WIZARD_OS_HOME: /state
      HERMES_HOME: /hermes
      HERMES_ORIGIN: {a['origin']}
      WIZARD_STT_ORIGIN: {stt_origin}
      WIZARD_APPS_MCP_ADDRESS: url
      WIZARD_MEDIA_AGENT_ROOT: {BASE}/runtipi/media
      WIZARD_HERMES_STATE_PATH: /hermes/state.db
      WIZARD_PRIVSEP_LINK: /state/link.json
    volumes: !override
      - {state}:/state
      - {hh}:/hermes
      - {BASE}/runtipi/app-data/wizard-os:/registry:ro
      - {checkout}/scripts/agent-service-start.mjs:/privsep/agent-service-start.mjs:ro
      - {BASE}/runtipi/media:/media
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:{service_port(st)}/wizard/health >/dev/null || exit 1"]
'''


def doctor(c):
    """Never inspect Hermes config/key/history at the manager uid."""
    from . import alongside, cli, index
    st = alongside.load(c.home)
    out = []
    try:
        rt = c.runtipi()
        rt.installed()
        cli.ensure_store(c, rt)
        out.append(('App Manager API reachable', True, rt.base))
        hits = rt.req('GET', '/marketplace/apps/search?pageSize=3&storeId=wizardapps').get('data') or []
        out.append(('App Store lists index apps', bool(hits), f'{len(hits)} shown'))
    except Exception as e:
        out.append(('App Manager API reachable', False, str(e)[:200]))
    out.append(('API is box-only', c.bind == '127.0.0.1', f'{c.bind}:{c.port}'))
    out.append(('API token present (0600)', c.token_file.is_file() and not c.token_file.stat().st_mode & 0o077, str(c.token_file)))
    out.append(('agent token present (0600)', (c.home / 'agent-token').is_file() and not (c.home / 'agent-token').stat().st_mode & 0o077, 'scoped bearer; not an owner credential'))
    out.append(('apps.json written', (c.home / 'apps.json').is_file(), str(c.home / 'apps.json')))
    if st:
        out.append(('alongside: host Hermes API answers', alongside.api_up(st['agent']['origin']), st['agent']['origin']))
        override = c.runtipi_root / 'user-config/wizardapps/wizard-agent-service/docker-compose.yml'
        out.append(('alongside: Agent Service uses agent uid', override.is_file() and f'user: "{st["agent"]["uid"]}:{st["agent"]["gid"]}"' in override.read_text(), str(override)))
    # The Service itself, not a root read of the agent's key/config, proves the handoff.
    if st:
        import urllib.request
        origin = 'http://127.0.0.1:' + str(alongside.service_port(st)) + '/wizard/health'
        try:
            with urllib.request.urlopen(origin, timeout=10) as response:
                up = response.status == 200
        except OSError:
            up = False
        out.append(('alongside: Agent Service answers', up, origin))
    # Ordinary network/policy checks still run; no builder bundle or agent-owned config reads.
    from . import network, host_policy
    out.extend(host_policy.grandfathered_health(c.home))
    try:
        rep = network.report(c)
        errors = rep.get('errors') or []
        drift = [d for a in rep.get('apps', []) for d in a.get('drift', [])]
        out.append(('app networks match the declared connections', not errors and not drift, str(errors + drift)[:200]))
    except Exception as e:
        out.append(('app networks match the declared connections', False, str(e)[:200]))
    return out
