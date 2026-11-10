"""GET /api/system/devices: every machine this Wizard runs on or can reach (wizard-os spec/DEVICES.md).

The shell's Resources > Devices tab and its Wizard switcher (wizard-ux core/resources.js, core/workspaces.js) read
this route at the shell origin. On a standard install nothing served it there (wizard-app-manager#568), so this host
process serves it on its loopback API beside /api/system/load and `wam links` publishes the exact path.

REUSE: a straight port of wizard-ux tools/system-devices.mjs (cleanRegistry, PROBE/parseProbe, tailnetStatus,
createDevicesReader), the reader the bespoke resources sidecar runs. Keep the two in step: same registry
(`~/.wizard-os/devices.json`, owner_devices.registry_path), same allowlisted output, including the managed Wizards'
url/kind/role/org (wizard-ux#1186). Read-only: probes run one fixed script over `sh -s` (this box) or
`ssh -T <host> sh -s` (registered machines); the host is validated and passed as one argv item, never via a shell.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import posixpath
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

PATH = '/api/system/devices'
GiB = 1024 ** 3
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
HOST_RE = re.compile(r'^(?!-)[A-Za-z0-9._@:-]{1,253}$')
GROUPS = {'agent', 'human', 'client', 'other'}
USES = {'standby', 'helper'}
EXPOSURE = {'tailscale', 'internet', 'unchecked'}
_CTRL = re.compile(r'[\u0000-\u001f\u007f]')
LINUX_SKIP = re.compile(r'^/(boot|snap|run|dev|sys|proc|var/lib/(docker|containers|kubelet)|var/snap)(/|$)')
MAC_SKIP = re.compile(r'^/Volumes/(Recovery|Preboot|VM|Update|com\.apple)')

PROBE = r'''
LC_ALL=C; export LC_ALL
os=$(uname -s); echo "os=$os"
if [ "$os" = Darwin ]; then
  echo "cpus=$(sysctl -n hw.ncpu)"
  echo "memtotal=$(sysctl -n hw.memsize)"
  ps_=$(sysctl -n hw.pagesize)
  vm_stat | awk -v ps="$ps_" '/^Pages active/{a=$3}/^Pages wired down/{w=$4}/occupied by compressor/{c=$5}END{gsub(/\./,"",a);gsub(/\./,"",w);gsub(/\./,"",c);printf "memused=%.0f\n",(a+w+c)*ps}'
  top -l 2 -n 0 -s 1 | awk '/^CPU usage/{for(i=1;i<=NF;i++) if($(i+1) ~ /^idle/) {v=$i}}END{gsub(/%/,"",v); if(v!="") print "cpuidle=" v}'
  bt=$(sysctl -n kern.boottime | sed 's/^[^0-9]*\([0-9]*\).*/\1/'); echo "uptime=$(( $(date +%s) - bt ))"
else
  echo "cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)"
  awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}END{printf "memtotal=%.0f\nmemused=%.0f\n",t*1024,(t-a)*1024}' /proc/meminfo
  a=$(head -1 /proc/stat); sleep 1; b=$(head -1 /proc/stat)
  echo "$a
$b" | awk '{i=$5+$6; t=0; for(k=2;k<=NF;k++) t+=$k; if(NR==1){i0=i;t0=t}else{dt=t-t0; if(dt>0) printf "cpuidle=%.1f\n",(i-i0)*100/dt}}'
  echo "uptime=$(cut -d. -f1 /proc/uptime)"
fi
df -kP 2>/dev/null | awk 'NR>1{m=$6; for(i=7;i<=NF;i++) m=m" "$i; print "disk="$1"|"$2"|"$3"|"$4"|"m}'
'''

PROBE_WIN = r'''
$ErrorActionPreference='SilentlyContinue'
$cs=Get-CimInstance Win32_ComputerSystem; $os=Get-CimInstance Win32_OperatingSystem
"os=Windows"
"cpus=$($cs.NumberOfLogicalProcessors)"
"memtotal=$([int64]$os.TotalVisibleMemorySize*1024)"
"memused=$(([int64]$os.TotalVisibleMemorySize-[int64]$os.FreePhysicalMemory)*1024)"
$l=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
if($l -ne $null){"cpuidle=$(100-$l)"}
"uptime=$([int64]((Get-Date)-$os.LastBootUpTime).TotalSeconds)"
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object { "wdisk=$($_.DeviceID)|$($_.Size)|$($_.FreeSpace)" }
'''


def _clip(s, n: int) -> str:
    return _CTRL.sub('', '' if s is None else str(s)).strip()[:n]


def _jround(x) -> int:
    """JavaScript Math.round (half up), so both readers give the same numbers."""
    return int(math.floor(x + 0.5))


def peer_group(os_name, name, reg: dict) -> str:
    return reg['groups'].get(str(name).lower()) or (
        'human' if re.fullmatch(r'(ios|android|macos|ipados)', os_name or '', re.I) else 'other')


# ---------------------------------------------------------------- registry
def _real_day(v) -> bool:
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', str(v or ''))
    if not m:
        return False
    try:
        _dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return False
    return m[0] <= _dt.datetime.now(_dt.timezone.utc).date().isoformat()   # today (UTC) at the latest


def clean_access(a):
    if not isinstance(a, dict):
        return None
    exposure = a.get('exposure') if a.get('exposure') in EXPOSURE else 'unchecked'
    details = [x for x in (_clip(x, 140) for x in (a.get('details') if isinstance(a.get('details'), list) else [])) if x][:8]
    how = _clip(a.get('how'), 80)
    return {'exposure': exposure, **({'checked': a['checked']} if _real_day(a.get('checked')) else {}),
            **({'how': how} if how else {}), **({'details': details} if details else {})}


def _with_access(a) -> dict:
    c = clean_access(a)
    return {'access': c} if c else {}


def _safe_url(v) -> str:
    """A saved address is navigation only, never permission to manage the target."""
    s = _clip(v, 2048)
    try:
        u = urlsplit(s)
        if u.scheme in ('http', 'https') and u.hostname and not u.username and not u.password:
            return u.geturl() if u.path else u._replace(path='/').geturl()
    except ValueError:
        pass
    return ''


def clean_registry(raw) -> dict:
    j = raw if isinstance(raw, dict) else {}

    def names(v):
        return [x for x in (_clip(x, 64).lower() for x in (v if isinstance(v, list) else [])) if x][:8]

    t = j.get('this') if isinstance(j.get('this'), dict) else {}
    this_box = {'name': _clip(t.get('name'), 40) or None, 'role': _clip(t.get('role'), 60) or None,
                'tailnet': names(t.get('tailnet')),
                **({'wizard': _clip(t.get('wizard'), 40)} if _clip(t.get('wizard'), 40) else {}),
                **({'owner': _clip(t.get('owner'), 40)} if _clip(t.get('owner'), 40) else {}),
                **_with_access(t.get('access'))}
    seen, devices = {'this'}, []
    for d in j.get('devices') if isinstance(j.get('devices'), list) else []:
        if not isinstance(d, dict):
            continue
        did = _clip(d.get('id'), 32).lower()
        if not ID_RE.match(did) or did in seen:
            continue
        c = d.get('connect') if isinstance(d.get('connect'), dict) else {}
        if c.get('type') != 'ssh' or not HOST_RE.match(str(c.get('host') or '')):
            continue
        seen.add(did)
        devices.append({'id': did, 'name': _clip(d.get('name'), 40) or did, 'role': _clip(d.get('role'), 60) or None,
                        'group': d.get('group') if d.get('group') in GROUPS else 'agent',
                        **({'use': d['use']} if d.get('use') in USES else {}),
                        'connect': {'type': 'ssh', 'host': str(c['host']),
                                    **({'shell': 'powershell'} if c.get('shell') == 'powershell' else {})},
                        'tailnet': names(d.get('tailnet')), **_with_access(d.get('access'))})
        if len(devices) >= 24:
            break
    groups = {}
    for k, v in list((j.get('groups') if isinstance(j.get('groups'), dict) else {}).items())[:100]:
        n = _clip(k, 64).lower()
        if n and isinstance(v, str) and v in GROUPS:
            groups[n] = v
    managed, mseen = [], set()
    for m in j.get('managed') if isinstance(j.get('managed'), list) else []:
        if not isinstance(m, dict):
            continue
        mid, name = _clip(m.get('id'), 32).lower(), _clip(m.get('name'), 40)
        if not ID_RE.match(mid) or not name or mid in mseen:
            continue
        mseen.add(mid)
        where, peer, url = _clip(m.get('where'), 120), _clip(m.get('peer'), 64).lower(), _safe_url(m.get('url'))
        people = [x for x in (_clip(x, 40) for x in (m.get('people') if isinstance(m.get('people'), list) else [])) if x][:24]
        n = _num(m.get('peopleCount')) if not isinstance(m.get('peopleCount'), bool) else None
        count = n if type(n) is int and len(people) < n <= 999 else 0
        managed.append({'id': mid, 'name': name, **({'url': url} if url else {}),
                        **({'kind': m['kind']} if m.get('kind') in ('company', 'managed', 'remote') else {}),
                        **({'role': m['role']} if m.get('role') in ('owner', 'technical-admin', 'member') else {}),
                        **({'org': _clip(m.get('org'), 60)} if _clip(m.get('org'), 60) else {}),
                        **({'where': where} if re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', where, re.I) else {}),
                        **({'peer': peer, 'peerName': _clip(m.get('peerName'), 40) or peer} if peer else {}),
                        **({'people': people} if people else {}), **({'peopleCount': count} if count else {}),
                        **_with_access(m.get('access'))})
        if len(managed) >= 12:
            break
    return {'version': 1, 'this': this_box, 'devices': devices, 'hide': names(j.get('hide')), 'groups': groups,
            **({'managed': managed} if managed else {})}


def read_registry(path: Path) -> dict:
    try:
        return clean_registry(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return clean_registry({})


# ---------------------------------------------------------------- probe
def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return int(n) if n.is_integer() else n


def parse_probe(text) -> dict:
    kv, rows, wrows = {}, [], []
    for line in str(text).split('\n')[:400]:
        i = line.find('=')
        if i < 1:
            continue
        k, v = line[:i], line[i + 1:].strip()
        if k == 'disk':
            rows.append(v.split('|'))
        elif k == 'wdisk':
            wrows.append(v.split('|'))
        else:
            kv[k] = v
    mac, linux, win = kv.get('os') == 'Darwin', kv.get('os') == 'Linux', kv.get('os') == 'Windows'
    if not (mac or linux or win):
        raise ValueError('unsupported platform')
    disks, devs = [], set()
    for w in wrows if win else []:
        drive, size, av = (w + [None, None, None])[:3]
        size, av = _num(size), _num(av)
        if not re.fullmatch(r'[A-Z]:', str(drive)) or size is None or size < GiB or av is None:
            continue
        disks.append({'label': 'System disk' if drive == 'C:' else _clip(drive, 4), 'mount': _clip(drive, 4),
                      'usedGB': _jround((size - av) / GiB), 'sizeGB': _jround(size / GiB), 'freeGB': _jround(av / GiB),
                      'pct': _jround((size - av) / size * 100)})
    for r in rows:
        if len(r) < 4:
            continue
        fs, blocks, used, avail, rest = r[0], r[1], r[2], r[3], r[4:]
        mount = '|'.join(rest)
        size, av, us = ((_num(x) or 0) * 1024 if _num(x) is not None else None for x in (blocks, avail, used))
        if not fs.startswith('/dev/') or not mount or size is None or size < GiB or av is None:
            continue
        if mac:
            # APFS volumes share one container: the Data volume shows the real fill of the internal disk.
            if mount == '/System/Volumes/Data':
                label = 'Internal disk'
            elif mount.startswith('/Volumes/') and not MAC_SKIP.match(mount):
                label = posixpath.basename(mount)
            else:
                continue
        else:
            if fs.startswith('/dev/loop') or LINUX_SKIP.match(mount):
                continue
            label = 'System disk' if mount == '/' else posixpath.basename(mount)
        if fs in devs:
            continue
        devs.add(fs)
        # Linux reserves root blocks, so fill = used/(used+avail) (what df and Runtipi report); APFS = (size-avail)/size.
        used_b = size - av if mac else (us or 0)
        total = size if mac else used_b + av
        disks.append({'label': _clip(label, 40), 'mount': _clip(mount, 120), 'usedGB': _jround(used_b / GiB),
                      'sizeGB': _jround(size / GiB), 'freeGB': _jround(av / GiB),
                      'pct': _jround(used_b / total * 100) if total > 0 else None})
    disks.sort(key=lambda d: d['label'] not in ('Internal disk', 'System disk'))   # stable, like the JS sort
    if mac and not any(d['label'] == 'Internal disk' for d in disks):
        root = next((r for r in rows if '|'.join(r[4:]) == '/'), None)
        if root and _num(root[1]) is not None and _num(root[3]) is not None:
            size, av = _num(root[1]) * 1024, _num(root[3]) * 1024
            if size >= GiB:
                disks.insert(0, {'label': 'Internal disk', 'mount': '/', 'usedGB': _jround((size - av) / GiB),
                                 'sizeGB': _jround(size / GiB), 'freeGB': _jround(av / GiB),
                                 'pct': _jround((size - av) / size * 100)})
    mem_total, mem_used, idle = _num(kv.get('memtotal')), _num(kv.get('memused')), _num(kv.get('cpuidle'))

    def pct(v):
        return None if v is None else max(0, min(100, _jround(v)))

    return {'platform': 'macos' if mac else 'windows' if win else 'linux', 'cpus': _num(kv.get('cpus')),
            'cpu': None if idle is None else pct(100 - idle),
            'memory': pct(mem_used / mem_total * 100) if mem_total and mem_used is not None else None,
            'memoryTotalGB': _jround(mem_total / GiB) if mem_total else None,
            'memoryUsedGB': _jround(mem_used / GiB) if mem_total and mem_used is not None else None,
            'uptimeSec': _num(kv.get('uptime')), 'disks': disks[:8]}


def _run(args: list[str], stdin: str = '', timeout: float = 9, max_out: int = 65536) -> str:
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('timed out') from None
    except OSError as e:
        raise RuntimeError(str(e)) from None
    if p.returncode:
        raise RuntimeError((p.stderr or '').strip().split('\n')[-1] or f'exit {p.returncode}')
    if len(p.stdout) > max_out:
        raise RuntimeError('reading too large')
    return p.stdout


def probe_local() -> dict:
    return parse_probe(_run(['sh', '-s'], PROBE))


def probe_ssh(host: str, shell: str | None = None) -> dict:
    if not HOST_RE.match(host or ''):
        raise RuntimeError('invalid host')
    win = shell == 'powershell'
    remote = 'powershell -NoProfile -NonInteractive -Command -' if win else 'sh -s'
    out = _run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'ServerAliveInterval=5', '--',
                host, remote], PROBE_WIN if win else PROBE, timeout=20)
    return parse_probe(out.replace('\r', ''))


# ---------------------------------------------------------------- tailnet
def tailnet_status():
    from . import links
    try:
        j = json.loads(links._run(['tailscale', 'status', '--json'], timeout=4).stdout or '{}')
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(j, dict) or not isinstance(j.get('Self'), dict):
        return None

    def peer(p):
        seen = str(p.get('LastSeen') or '')
        return {'name': _clip(p.get('HostName'), 64), 'dns': _clip(str(p.get('DNSName') or '').split('.')[0], 64).lower(),
                'os': _clip(p.get('OS'), 16), 'online': bool(p.get('Online')),
                'lastSeen': seen if seen and not seen.startswith('0001') else None}
    peers = j.get('Peer') if isinstance(j.get('Peer'), dict) else {}
    return {'self': peer({**j['Self'], 'Online': True}),
            'peers': [peer(p) for p in list(peers.values())[:100] if isinstance(p, dict)]}


# ---------------------------------------------------------------- assembly
def _iso(ms: float) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(ms % 1000):03d}Z'


def _pretty(s):
    return s[:1].upper() + s[1:] if s else s


class Reader:
    """`Reader(registry_path)()` -> the devices payload. Each reading is cached for ttl seconds (one probe at a time
    per machine), so a phone and a desktop polling together never stack ssh sessions."""

    def __init__(self, registry_path, probe_local_impl=None, probe_ssh_impl=None, tailnet_impl=None,
                 now=lambda: time.time() * 1000, ttl_ms: float = 25000):
        self.registry_path, self.now, self.ttl = registry_path, now, ttl_ms
        # Unset impls resolve to the module functions at call time (so a test can patch them).
        self.probe_local = probe_local_impl or (lambda: probe_local())
        self.probe_ssh = probe_ssh_impl or (lambda host, shell=None: probe_ssh(host, shell))
        self.tailnet = tailnet_impl or (lambda: tailnet_status())
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _reading(self, key, fn) -> dict:
        with self._lock:
            c = self._cache.setdefault(key, {'at': None, 'data': None, 'error': None, 'okAt': 0, 'lock': threading.Lock()})
        with c['lock']:
            if c['at'] is not None and self.now() - c['at'] < self.ttl:
                return c
            try:
                c['data'], c['error'], c['okAt'] = fn(), None, self.now()
            except Exception as e:   # noqa: BLE001 - any probe failure is a status, never a crash
                c['error'] = _clip(e, 120) or 'unavailable'
            c['at'] = self.now()
            return c

    def __call__(self) -> dict:
        reg = read_registry(self.registry_path)

        def tn():
            t = self.tailnet()
            if not t:
                raise RuntimeError('no tailnet')
            return t
        jobs = [('_tailnet', tn), ('this', self.probe_local)] + [
            ('ssh:' + d['connect']['host'], (lambda d=d: self.probe_ssh(d['connect']['host'], d['connect'].get('shell'))))
            for d in reg['devices']]
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            tnc, local, *remote = list(ex.map(lambda kf: self._reading(*kf), jobs))
        net = tnc['data']

        def find_peer(keys):
            return next((p for p in (net or {}).get('peers', []) if p['name'].lower() in keys or p['dns'] in keys), None)

        def shape(base, c, peer):
            fresh = c['data'] is not None and not c['error']
            if fresh:
                status = 'online'
            elif c['data'] is not None:
                status = 'offline' if peer and not peer['online'] else 'stale'
            elif peer and not peer['online']:
                status = 'offline'
            elif c['error']:
                status = 'unreachable' if peer and peer['online'] else 'offline'
            else:
                status = 'connecting'
            return {**base, 'status': status, 'error': c['error'] if c['error'] and not fresh else None,
                    'readAt': _iso(c['okAt']) if c['okAt'] else None,
                    'lastSeen': peer['lastSeen'] if not fresh and peer and peer.get('lastSeen') else None,
                    **(c['data'] or {'platform': None, 'cpu': None, 'memory': None, 'memoryTotalGB': None, 'disks': []})}

        used = {k for k in [*reg['this']['tailnet'], net and net['self']['name'].lower(), net and net['self']['dns']] if k}
        t = reg['this']
        devices = [shape({'id': 'this', 'kind': 'this', 'group': 'agent',
                          'name': t['name'] or _pretty(net and net['self']['name']) or _pretty(socket.gethostname().split('.')[0]),
                          'role': t['role'] or 'Wizard runs here', **({'access': t['access']} if t.get('access') else {})},
                         local, None)]
        for d, c in zip(reg['devices'], remote):
            keys = {str(s).lower().split('.')[0] for s in [d['id'], d['name'], d['connect']['host'], *d['tailnet']] if s}
            peer = find_peer(keys)
            if peer:
                used.add(peer['name'].lower())
            devices.append(shape({'id': d['id'], 'kind': 'ssh', 'group': d['group'], 'name': d['name'], 'role': d['role'],
                                  **({'use': d['use']} if d.get('use') else {}),
                                  **({'access': d['access']} if d.get('access') else {})}, c, peer))
        hide = set(reg['hide'])
        if net:
            peers = [{'name': p['name'], 'os': p['os'], 'online': p['online'], 'lastSeen': None if p['online'] else p['lastSeen'],
                      'group': peer_group(p['os'], p['dns'] if p['dns'] in reg['groups'] else p['name'], reg)}
                     for p in net['peers'] if not ({p['name'].lower(), p['dns']} & (used | hide))]
            peers.sort(key=lambda p: (not p['online'], p['name'].lower(), p['name']))
            network = {'available': True, 'peers': peers}
        else:
            network = {'available': False, 'peers': []}
        managed = []
        for m in reg.get('managed') or []:
            p = m.get('peer') and next((x for x in (net or {}).get('peers', []) if x['name'].lower() == m['peer'] or x['dns'] == m['peer']), None)
            managed.append({**m, **({'peerOnline': bool(p['online']) if p else None} if m.get('peer') else {})})
        me = {k: t[k] for k in ('wizard', 'owner') if t.get(k)}
        return {'at': _iso(self.now()), 'devices': devices, 'network': network,
                **({'this': me} if me else {}), **({'managed': managed} if managed else {})}


_readers: dict[str, Reader] = {}
_readers_lock = threading.Lock()


def reader_for(home) -> Reader:
    """One cached reader per registry file, shared by every request thread."""
    from .owner_devices import registry_path
    path = str(registry_path(Path(home)))
    with _readers_lock:
        if path not in _readers:
            _readers[path] = Reader(path)
        return _readers[path]
