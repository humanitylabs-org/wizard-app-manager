"""Explicit owner-run legacy migration. Never imported by the nightly updater.

Data trees are opaque: only cp -a reads their bytes. Python opens a finite set of
manager metadata and Compose files, never app-data, agent homes or recordings.
The protected snapshot remains until the owner deliberately deletes it. Rollback
is destructive of changes since the snapshot; do not use it as a backup restore.
"""
from __future__ import annotations

import argparse
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

BASE = Path('/var/lib/wizard-os')
CONTROL = Path('/var/lib/wizard-os-migration')
SYSTEMD = Path('/etc/systemd/system')
STEPS = ('inventory', 'quiesce', 'snapshot', 'relocate', 'wiring', 'prepare',
         'units', 'start', 'doctor', 'grant', 'remove_docker', 'complete')
# The host dashboard and stock Hermes gateway intentionally stay at the agent uid.
USER_UNITS = ('wizard-app-manager-api.service', 'wizard-troubleshooter.service',
              'wizard-guardian.service', 'wizard-guardian.timer', 'wizard-backup.service',
              'wizard-backup.timer', 'wizard-os-update.service',
              'wizard-os-update.timer', 'wizard-store-data.service')
NEW_UNITS = ('wizard-runtipi.service', *USER_UNITS)
HELPER = '/usr/local/libexec/wizard-os/server-access'


def run(argv, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("text", True)
    return subprocess.run([str(x) for x in argv], **kwargs)


def atomic(path, value, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('refusing symlink ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(value)
        f.flush(); os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    d = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(d)
    finally: os.close(d)


def copy(source, target):
    """Archive copy preserves numeric container UIDs, links, modes and xattrs."""
    source, target = Path(source), Path(target)
    if source.exists() or source.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        run(['cp', '-a', '--reflink=auto', '--', source, target])


def remove(path):
    path = Path(path)
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.exists(): shutil.rmtree(path)


def relocate_text(text, old, new):
    # Longest first so nested paths do not get double-rewritten. No home-wide
    # replacement: the Hermes home and Claude login share remain at the agent uid.
    for a, b in sorted(zip(old, new), key=lambda pair: -len(str(pair[0]))):
        text = text.replace(str(a), str(b))
    return text


def timer_template(text):
    """Keep timing, not a user-written Unit= target or executable directive."""
    import configparser
    cfg = configparser.ConfigParser(interpolation=None, strict=True)
    cfg.optionxform = str
    cfg.read_string(text)
    allowed = {'OnCalendar','OnBootSec','OnUnitActiveSec','OnUnitInactiveSec',
               'OnActiveSec','AccuracySec','RandomizedDelaySec','Persistent'}
    values = dict(cfg['Timer'])
    if set(values)-allowed: raise ValueError('Custom timer needs an owner mapping')
    return '[Unit]\nDescription=Wizard scheduled maintenance\n[Timer]\n' + ''.join(k+'='+v+'\n' for k,v in values.items()) + '[Install]\nWantedBy=timers.target\n'


class Migration:
    def __init__(self, agent=None, hermes_home=None, ref=None):
        self.path = CONTROL / 'journal.json'
        self.doc = json.loads(self.path.read_text()) if self.path.exists() else {
            'schema': 1, 'agent': agent, 'hermes_home': hermes_home, 'ref': ref,
            'done': [], 'state': 'migrating'}
        if agent and (self.doc['agent'], self.doc['hermes_home'], self.doc['ref']) != (agent, hermes_home, ref):
            raise ValueError('Migration belongs to another identity or source; use its original arguments.')
        if not self.doc['agent']:
            raise ValueError('No migration snapshot. Ask the owner to run host preparation first.')
        self.agent = pwd.getpwnam(self.doc['agent'])
        self.old = Path(self.agent.pw_dir)
        self.snapshot = CONTROL / 'snapshot'
        self.tree = BASE / 'wizard-app-manager'

    def save(self):
        atomic(self.path, json.dumps(self.doc, indent=2) + '\n')

    def user(self, *args, **kwargs):
        env = {'HOME': str(self.old), 'PATH': '/usr/local/bin:/usr/bin:/bin',
               'XDG_RUNTIME_DIR': f'/run/user/{self.agent.pw_uid}',
               'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{self.agent.pw_uid}/bus'}
        kwargs.setdefault('cwd', self.old)
        return run(['runuser', '-u', self.agent.pw_name, '--', 'env', '-i',
                    *[k + '=' + v for k, v in env.items()], *args], **kwargs)

    def system(self, code, **kwargs):
        return run(['runuser', '-u', 'wizard-system', '--', 'env', '-i',
                    'PATH=/usr/local/bin:/usr/bin:/bin', f'HOME={BASE}',
                    f'WIZARD_OS_HOME={BASE}/.wizard-os', f'RUNTIPI_ROOT={BASE}/runtipi',
                    '/usr/bin/python3', '-c', code], cwd=self.tree, **kwargs)

    def inventory(self):
        if BASE.exists(): raise ValueError('Destination already exists; refusing adoption without a journal.')
        if not Path(self.doc['hermes_home']).is_relative_to(self.old):
            raise ValueError('Hermes home must be inside the selected account home.')
        if self.agent.pw_uid == 0: raise ValueError('Cannot migrate a root agent.')
        if any((SYSTEMD/u).exists() for u in NEW_UNITS):
            raise ValueError('Existing system units conflict with the legacy-only migration.')
        if not (self.old/'.wizard-os/alongside.json').is_file():
            raise ValueError('Legacy alongside metadata is missing; owner mapping is required before moving anything.')
        for p in (self.old, self.old/'runtipi', self.old/'.wizard-os'):
            if not p.is_dir() or p.is_symlink(): raise ValueError('Expected a real legacy directory: ' + str(p))
        groups = [grp.getgrgid(g).gr_name for g in os.getgrouplist(self.agent.pw_name, self.agent.pw_gid)]
        if grp.getgrgid(self.agent.pw_gid).gr_name == 'docker':
            raise ValueError('Docker is the primary group; owner must map a normal primary group first.')
        if set(groups) & {'sudo', 'wheel', 'admin'}:
            raise ValueError('Remove administrator groups explicitly before migration; never silently revoke owner access.')
        if self.user('sudo', '-n', 'true', check=False, capture_output=True).returncode == 0:
            raise ValueError('Existing sudo policy needs an owner review before migration.')
        # Unrecognised services that consume these trees must not be silently
        # left running as writers. Inspect unit metadata only, never its data.
        unitdir = self.old/'.config/systemd/user'
        if unitdir.resolve() != unitdir:
            raise ValueError('Linked user-unit directories need an owner mapping')
        for unit in USER_UNITS:
            if unit.endswith('.timer') and (unitdir/unit).is_file():
                timer_template((unitdir/unit).read_text())
        for p in unitdir.glob('*.service'):
            if p.is_symlink():
                if os.readlink(p) == '/dev/null': continue
                raise ValueError('Linked legacy unit needs an owner mapping: '+p.name)
            if p.name in USER_UNITS: continue
            text = p.read_text()
            if str(self.old/'runtipi') in text or str(self.old/'.wizard-os') in text:
                raise ValueError('Owner must map this additional legacy service before migration: '+p.name)
        self.doc['groups'] = groups
        self.doc['units'] = {}
        for unit in USER_UNITS:
            self.doc['units'][unit] = {
                'enabled': self.user('systemctl', '--user', 'is-enabled', unit, check=False, capture_output=True).returncode == 0,
                'active': self.user('systemctl', '--user', 'is-active', unit, check=False, capture_output=True).returncode == 0}
        self.doc['operator'] = json.loads(run(['tailscale', 'debug', 'prefs'], capture_output=True).stdout).get('OperatorUser') or ''
        settings_path = self.old/'runtipi/state/settings.json'
        settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
        if settings.get('appDataPath') and settings['appDataPath'] != str(self.old/'runtipi'):
            raise ValueError('External app storage needs an explicit owner mapping before migration.')
        if self.doc['operator'] not in ('', self.agent.pw_name):
            raise ValueError('Another Tailscale operator needs a separate owner review.')
        self.doc['serve'] = json.loads(run(['tailscale', 'serve', 'status', '--json'], capture_output=True).stdout or '{}')
        # Only container metadata is read, never docker inspect Env or container files.
        rows = []
        for label in ('runtipi.appurn','com.docker.compose.project=runtipi'):
            rows += run(['docker','ps','-q','--filter','label='+label],capture_output=True).stdout.split()
        rows = sorted(set(rows))
        self.doc['containers'] = rows
        self.doc['running_apps'] = run(['docker', 'ps', '--format', '{{.Label "runtipi.appurn"}}'], capture_output=True).stdout.split()
        self.doc['created_at'] = time.time()

    def quiesce(self):
        for unit in USER_UNITS:
            self.user('systemctl', '--user', 'disable', '--now', unit, check=False, capture_output=True)
            if self.user('systemctl', '--user', 'is-active', '--quiet', unit, check=False).returncode == 0:
                raise ValueError('A legacy service did not stop: ' + unit)
        ids = []
        for label in ('runtipi.appurn', 'com.docker.compose.project=runtipi'):
            ids += run(['docker','ps','-q','--filter','label='+label], capture_output=True).stdout.split()
        if ids: run(['docker', 'stop', '--time', '60', *sorted(set(ids))])

    def snapshot_step(self):
        # Each item has its own atomic completion record; a partial cp is removed
        # before retry. Snapshot is protected from the agent even though cp keeps
        # the original file ownership inside it.
        items = {'runtipi': self.old/'runtipi', 'state': self.old/'.wizard-os',
                 'user-units': self.old/'.config/systemd/user',
                 'wam': self.old/'.local/bin/wam', 'wizard': self.old/'.local/bin/wizard'}
        # Opaque copy only of Wizard-owned main-agent integration. Never recurse
        # into other agents' homes in Python, nor snapshot history/recordings.
        hh = Path(self.doc['hermes_home'])
        for name in ('config.yaml', '.env', 'plugins'):
            items['hermes-' + name] = hh/name
        self.doc.setdefault('snapshotted', [])
        for name, source in items.items():
            if name in self.doc['snapshotted']: continue
            target = self.snapshot/name
            remove(target); copy(source, target)
            self.doc['snapshotted'].append(name); self.save()
        # Preserve all root integration targets that this migration can replace.
        paths = [SYSTEMD/u for u in NEW_UNITS]
        paths += [Path('/usr/local/bin/wam'), Path('/usr/local/bin/wizard'),
                  Path('/etc/wizard-os'), Path('/usr/local/libexec/wizard-os'),
                  Path('/var/lib/wizard-os-server-access'),
                  Path('/etc/sudoers.d/90-wizard-server-access-helper'),
                  Path('/etc/sudoers.d/91-wizard-agent-access'),
                  SYSTEMD/'wizard-agent-access-expiry.service', SYSTEMD/'wizard-agent-access-expiry.timer']
        self.doc['root_paths'] = [str(p) for p in paths]
        for p in paths:
            name = 'root:' + str(p)
            if name in self.doc['snapshotted']: continue
            target = self.snapshot/'root'/str(p).lstrip('/')
            remove(target); copy(p, target)
            self.doc['snapshotted'].append(name); self.save()

    def relocate(self):
        BASE.mkdir(exist_ok=True, mode=0o755)
        if BASE.is_symlink(): raise ValueError('Unsafe destination symlink')
        for source, name in ((self.snapshot/'runtipi', 'runtipi'), (self.snapshot/'state', '.wizard-os')):
            # Replay from the immutable snapshot, not the possibly-partial new copy.
            remove(BASE/name); copy(source, BASE/name)
        remove(self.tree); copy(CONTROL/'source', self.tree)
        try: system = pwd.getpwnam('wizard-system')
        except KeyError:
            run(['useradd', '--system', '--home-dir', BASE, '--shell', '/usr/sbin/nologin', 'wizard-system'])
            system = pwd.getpwnam('wizard-system')
        if system.pw_dir != str(BASE): raise ValueError('wizard-system home does not match the protected layout')
        # Management trees get the service identity. Container data and agent
        # homes keep EVERY numeric owner, even when it equals the legacy uid.
        os.chown(BASE, system.pw_uid, system.pw_gid)
        for p in (self.tree, BASE/'.wizard-os', BASE/'runtipi'):
            for current, dirs, files in os.walk(p, followlinks=False):
                rel = Path(current).relative_to(BASE)
                if rel.parts[:2] in (('runtipi', 'app-data'), ('runtipi', 'data'), ('runtipi', 'media')) or rel.parts[:2] == ('.wizard-os', 'agents'):
                    dirs[:] = []; continue
                os.chown(current, system.pw_uid, system.pw_gid, follow_symlinks=False)
                for file in files:
                    os.chown(Path(current)/file, system.pw_uid, system.pw_gid, follow_symlinks=False)
        atomic(BASE/'.wizard-privsep', self.agent.pw_name+'\n', 0o644)
        os.chown(BASE/'.wizard-privsep', system.pw_uid, system.pw_gid)

    def wiring(self):
        old = [self.old/'runtipi', self.old/'.wizard-os', self.old/'wizard-app-manager']
        new = [BASE/'runtipi', BASE/'.wizard-os', self.tree]
        rt, state = BASE/'runtipi', BASE/'.wizard-os'
        # The registry projection is manager metadata, not an application's data.
        # Leave every app payload owner alone, including the legacy agents sibling.
        projection = rt/'app-data/wizard-os'
        system = pwd.getpwnam('wizard-system')
        if projection.is_symlink(): raise ValueError('Unsafe registry projection symlink')
        if projection.exists():
            os.chown(projection, system.pw_uid, system.pw_gid)
            for name in ('apps.json', 'apps.json.tmp', 'skills'):
                p = projection/name
                if p.is_symlink(): raise ValueError('Unsafe registry metadata symlink')
                if p.is_file(): os.chown(p, system.pw_uid, system.pw_gid)
                elif p.is_dir():
                    for directory, dirs, files in os.walk(p, followlinks=False):
                        os.chown(directory, system.pw_uid, system.pw_gid)
                        for file in files:
                            os.chown(Path(directory)/file, system.pw_uid, system.pw_gid, follow_symlinks=False)
        # Finite metadata surface only. Never glob app-data or agents. Compose
        # overrides include builder, agent_link mounts, extra agents and Claude.
        paths = [rt/'.env', rt/'state/settings.json', state/'alongside.json',
                 state/'backup.json', state/'backup-state.json', state/'app-manager.json',
                 state/'apps.json', state/'app-sources.json']
        paths += [rt/'docker-compose.yml']
        paths += list((rt/'apps').glob('*/*/docker-compose.yml'))
        paths += list((rt/'user-config').glob('*/*/docker-compose.yml'))
        paths += list((rt/'user-config').glob('*/*/app.env'))
        paths += [rt/'user-config/tipi-compose.yml']
        for p in paths:
            if p.resolve() != p: raise ValueError('Unsafe manager metadata symlink: ' + str(p))
            if p.is_file():
                text = p.read_text(); changed = relocate_text(text, old, new)
                if changed != text: p.write_text(changed)
        st = json.loads((state/'alongside.json').read_text())
        st['agent'].update(uid=self.agent.pw_uid, gid=self.agent.pw_gid,
                           hermes_home=self.doc['hermes_home'])
        # Privileged policy regeneration must not inspect private registries or
        # app.env files inside app-data during migration.
        st['migration_metadata_only'] = True
        st['migration'] = {'legacy_home': str(self.old)}
        (state/'alongside.json').write_text(json.dumps(st))
        for token in ('api-token', 'agent-token'):
            p = state/token
            if p.resolve() != p: raise ValueError('Unsafe legacy token symlink')
            p.write_text(secrets.token_urlsafe(32)+'\n'); p.chmod(0o600)
        # Seal grandfathering BEFORE Config's ordinary startup migration. A later
        # install cannot enroll new permissions. Existing approvals are retained.
        self.system('from pathlib import Path; from types import SimpleNamespace; from wam import host_policy; '
                    f'c=SimpleNamespace(home=Path({str(state)!r}),runtipi_root=Path({str(rt)!r}),migration_metadata_only=True); '
                    'host_policy.migrate(c)')

    def prepare(self):
        if not (CONTROL/'agent-handoff-ready').exists():
            remove(self.old/'.wizard-os')
            (self.old/'.wizard-os').mkdir(mode=0o700)
            os.chown(self.old/'.wizard-os', self.agent.pw_uid, self.agent.pw_gid)
            for name in ('linked.json', 'link-backup', 'mcp-token'):
                copy(self.snapshot/'state'/name, self.old/'.wizard-os'/name)
            atomic(CONTROL/'agent-handoff-ready', 'ready\n')
        run(['/usr/bin/python3', '-I', self.tree/'scripts/prepare-alongside.py',
             self.agent.pw_name, self.doc['hermes_home'], self.doc['ref'], '--migration'])

    def auxiliary_units(self):
        """Regenerate from protected shipped generators, never adopt ExecStart."""
        present = [u for u in self.doc['units'] if (self.snapshot/'user-units'/u).exists()]
        if not set(present) & {'wizard-troubleshooter.service','wizard-guardian.service','wizard-store-data.service'}:
            return {}
        code = '''import json,os
from pathlib import Path
from wam import troubleshooter as t,store_data
base=Path('/var/lib/wizard-os'); state=base/'.wizard-os'
p=t.paths(base); repo,ref,_=t.pinned_source(); t.fetch(repo,ref,p['app'])
node=t.node(p)
lay=t.layout(state,base,base/'runtipi')
# System repair never opens the linked agent's authentication files.
env=t.environment(lay,'',[],t.tailnet_owner(),base)
env['CHATGPT_AUTH_FILE']=''; env['HERMES_UNIT']=''
p['env'].write_text(''.join(k+'='+str(v)+'\\n' for k,v in env.items())); p['env'].chmod(0o600)
out=t._units(p['app'],node,p['env'])
reg=json.loads((state/'apps.json').read_text())
sha=store_data.shell_commit(reg)
if sha:
 tools=store_data.fetch_tools(sha,base/'.local/share/wizard-store-data')
 out['wizard-store-data.service']=store_data.unit_text(node,tools,sha,service='http://127.0.0.1:'+str(lay['agent_port']))
print(json.dumps(out))
'''
        response = self.system(code, capture_output=True)
        generated = json.loads(response.stdout.splitlines()[-1])
        return {unit: text.replace('[Service]','[Service]\nUser=wizard-system\nGroup=wizard-system').replace('WantedBy=default.target','WantedBy=multi-user.target')
                for unit,text in generated.items()}

    def units(self):
        # Fixed templates only: NEVER promote user-written ExecStart to root or
        # the Docker-capable service account. The known non-core services need
        # protected installed source; unsupported legacy units stop migration.
        generated = self.auxiliary_units()
        commands = {'wizard-backup.service': '-m wam backup run --scheduled',
                    'wizard-os-update.service': '-m wam updates --apply-if-auto'}
        for unit, flags in self.doc['units'].items():
            if not (self.snapshot/'user-units'/unit).exists(): continue
            if unit == 'wizard-app-manager-api.service': continue  # prepare installs this template
            if unit in generated:
                text = generated[unit]
            elif unit.endswith('.timer'):
                text = timer_template((self.snapshot/'user-units'/unit).read_text())
            elif unit in commands:
                text = (f'[Unit]\nDescription=Wizard scheduled maintenance\n[Service]\nType=oneshot\nUser=wizard-system\n'
                        f'WorkingDirectory={self.tree}\nEnvironment=HOME={BASE}\nEnvironment=WIZARD_OS_HOME={BASE}/.wizard-os\n'
                        f'Environment=RUNTIPI_ROOT={BASE}/runtipi\nExecStart=/usr/bin/python3 {commands[unit]}\n')
            else:
                raise ValueError('This legacy service needs an explicit protected template: '+unit)
            atomic(SYSTEMD/unit, text, 0o644)
            if flags['enabled']: run(['systemctl', 'enable', unit])
        for unit in USER_UNITS:
            self.user('rm', '-f', str(self.old/'.config/systemd/user'/unit))
        self.user('systemctl', '--user', 'daemon-reload')
        # Local bin precedes /usr/local/bin on normal Hermes installs.
        self.user('ln', '-sf', '/usr/local/bin/wam', str(self.old/'.local/bin/wam'))
        self.user('ln', '-sf', '/usr/local/bin/wizard', str(self.old/'.local/bin/wizard'))
        run(['systemctl', 'daemon-reload'])

    def start(self):
        self.system('from wam.cli import Config,recover; c=Config(); r=recover(c, running='+repr(self.doc['running_apps'])+'); print(r); assert not r["failed"],r')
        # Recreate old running apps through WAM, not docker start (which would
        # retain old bind sources). Runtipi base was recreated by host preparation.
        for unit, flags in self.doc['units'].items():
            if flags['active'] and (SYSTEMD/unit).exists(): run(['systemctl', 'start', unit])
        self.check_links()

    def check_links(self):
        actual = json.loads(run(['tailscale','serve','status','--json'], capture_output=True).stdout or '{}')
        if actual != self.doc['serve']:
            raise ValueError('Published app links changed; leave Docker membership intact and roll back or repair.')
        # Only loopback endpoints from the saved serve metadata; never fetch
        # arbitrary Internet URLs or anything in app storage.
        import urllib.request
        for server in actual.get('Web', {}).values():
            for handler in server.get('Handlers', {}).values():
                url = handler.get('Proxy', '')
                if re.fullmatch(r'http://127\.0\.0\.1:\d+(?:/[A-Za-z0-9_./-]*)?', url):
                    with urllib.request.urlopen(url, timeout=15) as response:
                        if response.status >= 400: raise ValueError('An existing app link no longer answers')

    def doctor(self):
        self.system('import json; from wam.cli import Config,doctor; rows=doctor(Config()); '
                    'print(json.dumps(rows)); assert rows and all(ok for _,ok,_ in rows), "Doctor failed: Docker membership kept"')
        self.check_links()
        self.doc['doctor_passed'] = True

    def grant(self):
        if 'docker' not in self.doc['groups']: return
        # Consumed on the first migration, never renewed by resume or by another
        # migration after rollback. A failed partial helper call is a hard stop.
        ledger = Path('/var/lib/wizard-os-server-access/migration-used')
        if (CONTROL/'migration-used').exists():
            self.doc['grant_already_used'] = True
            return
        if ledger.exists():
            status = json.loads(run([HELPER, 'status'], capture_output=True).stdout)
            if not status.get('active') or not status.get('migration'):
                raise ValueError('The one-time access handoff was interrupted; owner review is required before removing Docker access.')
            atomic(CONTROL/'migration-used', 'used\n')
            return
        self.system('from wam.developer import migration_grant; print(migration_grant(None, True))')
        atomic(CONTROL/'migration-used', 'used\n')

    def remove_docker(self):
        if not self.doc.get('doctor_passed'): raise ValueError('Doctor must pass before removing Docker access.')
        # The old tree is no longer mounted. Its immutable snapshot remains
        # root-only for rollback; do not leave a writable duplicate behind.
        remove(self.old/'runtipi')
        if 'docker' in self.doc['groups']:
            if 'docker' in run(['id','-nG',self.agent.pw_name],capture_output=True).stdout.split():
                run(['gpasswd', '-d', self.agent.pw_name, 'docker'])
        # Existing processes keep supplementary groups. Restart the selected user
        # manager after the grant is in place, so stock Hermes cannot keep its old
        # Docker-capable process credentials. SSH owner login remains available.
        run(['loginctl','terminate-user',self.agent.pw_name])
        run(['systemctl','start',f'user@{self.agent.pw_uid}.service'])

    def complete(self):
        self.doc['state'] = 'migrated'

    def migrate(self, stop_after=None):
        if self.doc['state'] == 'rolling_back':
            raise ValueError('Finish the interrupted rollback before migrating again.')
        if self.doc['state'] == 'rolled_back' and 'snapshot' not in self.doc['done']:
            self.doc['done'] = []; self.doc['state'] = 'migrating'; self.save()
        if self.doc['state'] == 'rolled_back':
            # Re-use the exact original snapshot; never snapshot the thin client.
            self.doc['done'] = ['inventory', 'quiesce', 'snapshot']
            self.doc['doctor_passed'] = False
            self.doc['state'] = 'migrating'
            self.quiesce(); remove(CONTROL/'agent-handoff-ready'); self.save()
        for step in STEPS:
            if step in self.doc['done']: continue
            self.doc['current'] = step; self.save()
            getattr(self, 'snapshot_step' if step == 'snapshot' else step)()
            self.doc['done'].append(step); self.save()
            if step == stop_after: break
        return self.doc

    def rollback(self):
        if self.doc['state'] == 'rolled_back': return self.doc
        if 'inventory' not in self.doc['done']:
            raise ValueError('Inventory did not finish; no changes have been authorized to restore.')
        if 'snapshot' not in self.doc['done']:
            # Before the data copy, only units and containers were stopped.
            for unit, flags in self.doc.get('units', {}).items():
                if flags['enabled']: self.user('systemctl','--user','enable',unit)
                if flags['active']: self.user('systemctl','--user','start',unit)
            if self.doc.get('containers'): run(['docker','start',*self.doc['containers']])
            self.doc['done'] = []; self.doc['state'] = 'rolled_back'; self.save(); return self.doc
        self.doc['state'] = 'rolling_back'; self.doc['rollback_current'] = 'stop services'; self.save()
        # A previous rollback may have already restarted the legacy user units.
        # Stop them again before replaying the immutable snapshot.
        self.quiesce()
        if Path('/var/lib/wizard-os-server-access/migration-used').exists():
            atomic(CONTROL/'migration-used', 'used\n')
        for unit in (*NEW_UNITS, 'wizard-agent-access-expiry.timer'):
            run(['systemctl','disable','--now',unit], check=False, capture_output=True)
        ids = []
        for label in ('runtipi.appurn','com.docker.compose.project=runtipi'):
            ids += run(['docker','ps','-q','--filter','label='+label],capture_output=True).stdout.split()
        ids = sorted(set(ids))
        if ids: run(['docker','stop','--time','60',*ids])
        if Path(HELPER).exists(): run([HELPER,'revoke'])
        self.doc['rollback_current'] = 'restore original data and user units'; self.save()
        for name, target in {'runtipi': self.old/'runtipi', 'state': self.old/'.wizard-os',
                             'user-units': self.old/'.config/systemd/user',
                             'wam': self.old/'.local/bin/wam', 'wizard': self.old/'.local/bin/wizard',
                             **{'hermes-'+n: Path(self.doc['hermes_home'])/n for n in ('config.yaml','.env','plugins')}}.items():
            remove(target); copy(self.snapshot/name,target)
        self.doc['rollback_current'] = 'restore administrator integration'; self.save()
        for name in self.doc['root_paths']:
            target = Path(name); remove(target); copy(self.snapshot/'root'/name.lstrip('/'),target)
        # Retain the one-time grant ledger across rollback; rollback must never be
        # a way to mint another seven days. Policy/tokens themselves are restored.
        self.doc['rollback_current'] = 'restore groups and network operator'; self.save()
        run(['usermod','-G',','.join(g for g in self.doc['groups'] if g != grp.getgrgid(self.agent.pw_gid).gr_name),self.agent.pw_name])
        run(['tailscale','set','--operator='+self.doc['operator']])
        run(['systemctl','daemon-reload'])
        self.user('systemctl','--user','daemon-reload')
        # Recreate from original Compose metadata at the restored location. cp
        # restored app/database data including ownership; do not inspect it.
        self.doc['rollback_current'] = 'restart legacy layout'; self.save()
        self.user('docker','compose','-p','runtipi','-f','docker-compose.yml','-f','user-config/tipi-compose.yml','up','-d', cwd=self.old/'runtipi')
        for unit, flags in self.doc['units'].items():
            if flags['enabled']: self.user('systemctl','--user','enable',unit)
            if flags['active']: self.user('systemctl','--user','start',unit)
        # Restore applications with the original user-owned CLI at its original
        # UID, after Docker membership returns. Running the new Config against
        # restored legacy state would silently re-enroll approvals during rollback.
        for urn in self.doc['running_apps']:
            self.user(str(self.old/'.local/bin/wam'), 'start', urn)
        # Retain the protected code for the documented root rollback/status CLI.
        # Drop the migrated runtime so a subsequent migration replays the snapshot.
        for name in ('runtipi', '.wizard-os', '.wizard-privsep'):
            remove(BASE/name)
        self.check_links()
        self.doc['rollback_current'] = 'complete'
        self.doc['state'] = 'rolled_back'; self.save()
        return self.doc


def main(argv=None):
    parser = argparse.ArgumentParser(prog='wam privilege')
    parser.add_argument('action', choices=('migrate','rollback','status'))
    parser.add_argument('--agent'); parser.add_argument('--hermes-home'); parser.add_argument('--ref')
    parser.add_argument('--stop-after', choices=STEPS, help='Owner checkpoint: resume with the same migrate command')
    args = parser.parse_args(argv)
    if os.getuid() != 0:
        raise ValueError('Ask the owner to run the explicit root host-preparation migration; use the thin wam client for app actions.')
    CONTROL.mkdir(mode=0o700, exist_ok=True)
    if CONTROL.is_symlink() or CONTROL.stat().st_uid != 0 or CONTROL.stat().st_mode & 0o077:
        raise ValueError('Migration control directory must be root-owned 0700')
    with (CONTROL/'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        m = Migration(args.agent,args.hermes_home,args.ref)
        value = m.doc if args.action == 'status' else (m.migrate(args.stop_after) if args.action == 'migrate' else m.rollback())
        print(json.dumps(value,indent=2))
    return 0
