"""Structural ownership of the approval address, independent of the HTTP worker.

EXTEND owner_confirm: PID 1 retains the only listener. There is no bind fallback.
Only the owner-approved host installer writes these system units; the agent has
neither unit installation nor Serve setup authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import subprocess

PORT = 7792
SOCKET = 'wizard-owner-approval.socket'
SERVICE = 'wizard-app-manager-api.service'
NAME = 'wizard-owner-approval'
UNITS = Path('/etc/systemd/system')
SOCKET_TEXT = f'''[Unit]
Description=Wizard owner approval address custody
Before=tailscaled.service
RefuseManualStop=yes
IgnoreOnIsolate=yes
StartLimitIntervalSec=0

[Socket]
ListenStream=127.0.0.1:{PORT}
Accept=no
Service={SERVICE}
FileDescriptorName={NAME}
ReusePort=no
FlushPending=yes
TriggerLimitIntervalSec=0

[Install]
WantedBy=sockets.target
'''
TAILSCALE_TEXT = f'''[Unit]
Requires={SOCKET}
After={SOCKET}
'''


def _show(unit):
    # The system bus/manager is the authority, never a caller-supplied FD or file.
    r = subprocess.run(['/usr/bin/systemctl', '--system', 'show', unit],
                       capture_output=True, text=True, timeout=10, check=True,
                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
    return dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)


def _trusted_file(path, text):
    path = Path(path)
    try:
        for p in (path, *path.parents):
            s = p.lstat()
            if s.st_uid != 0 or s.st_mode & 0o022 or stat.S_ISLNK(s.st_mode):
                return False
        return path.read_text() == text
    except OSError:
        return False


def listener_row(proc=Path('/proc/net/tcp')):
    """Exactly one IPv4 listener on this port, root-created, exact loopback."""
    rows = []
    for line in Path(proc).read_text().splitlines()[1:]:
        f = line.split()
        if len(f) >= 10 and f[3] == '0A' and int(f[1].split(':')[1], 16) == PORT:
            rows.append((f[1].split(':')[0], int(f[7]), int(f[9])))
    if len(rows) != 1 or rows[0][:2] != ('0100007F', 0):
        raise ValueError('approval address is not the unique root-created loopback socket')
    # A wildcard/dual-stack IPv6 bind must not shadow the protected address.
    tcp6 = Path(proc).with_name('tcp6')
    if tcp6.exists():
        for line in tcp6.read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and f[3] == '0A' and int(f[1].split(':')[1], 16) == PORT:
                raise ValueError('ambiguous IPv6 approval listener')
    return rows[0][2]


def require_custody():
    """Require the loaded persistent root unit and boot ordering before Serve.

    Requires/After on tailscaled also orders shutdown: tailscaled stops before
    PID 1 releases the socket. Manual socket stop is forbidden. Ordinary manager
    stop, crash, upgrade or failed startup never stops the socket unit.
    """
    fragment = UNITS / SOCKET
    dropin = UNITS / 'tailscaled.service.d/50-wizard-owner-approval.conf'
    if not _trusted_file(fragment, SOCKET_TEXT) or not _trusted_file(dropin, TAILSCALE_TEXT):
        raise ValueError('secure approval socket lifecycle is not installed; ask the administrator for host preparation')
    s = _show(SOCKET)
    if (s.get('FragmentPath') != str(fragment) or s.get('DropInPaths') or
            s.get('ActiveState') != 'active' or s.get('SubState') not in ('listening', 'running') or
            s.get('UnitFileState') != 'enabled' or s.get('NeedDaemonReload') != 'no' or
            s.get('RefuseManualStop') != 'yes' or s.get('Accept') != 'no' or
            s.get('TriggerLimitIntervalUSec') != '0' or
            s.get('Listen') != f'127.0.0.1:{PORT} (Stream)'):
        raise ValueError('approval socket unit is not the exact active persistent custody unit')
    ts = _show('tailscaled.service')
    if (str(dropin) not in ts.get('DropInPaths', '').split() or
            SOCKET not in ts.get('Requires', '').split() or
            SOCKET not in ts.get('After', '').split() or ts.get('NeedDaemonReload') != 'no'):
        raise ValueError('tailscaled lacks persistent approval custody ordering')
    # systemd otherwise propagates service start-limit hits to the socket and
    # CLOSES it, even with RefuseManualStop. This must be disabled on the worker.
    service = _show(SERVICE)
    if service.get('StartLimitIntervalUSec') != '0' or service.get('NeedDaemonReload') != 'no':
        raise ValueError('manager start-limit policy can release approval custody')
    return listener_row()


def owned(home=None):
    try:
        require_custody()
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def inherited_listener():
    """Validate systemd provenance AND the exact FD; environment alone proves nothing."""
    try:
        inode = require_custody()
        service = _show(SERVICE)
        if (os.getppid() != 1 or service.get('MainPID') != str(os.getpid()) or
                SOCKET not in service.get('TriggeredBy', '').split() or
                os.environ.get('LISTEN_PID') != str(os.getpid()) or
                os.environ.get('LISTEN_FDS') != '1' or os.environ.get('LISTEN_FDNAMES') != NAME):
            raise ValueError('approval listener must be passed by the system manager to its exact service')
        if os.readlink('/proc/self/fd/3') != f'socket:[{inode}]':
            raise ValueError('approval activation FD is not the custody socket')
        # Duplicate only after validation; the HTTP server owns this duplicate.
        s = socket.socket(fileno=os.dup(3))
        os.close(3)  # never leak the original activation FD to manager children
        try:
            if (s.family != socket.AF_INET or s.type != socket.SOCK_STREAM or
                    s.getsockname() != ('127.0.0.1', PORT) or
                    s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1 or
                    s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) != 0):
                raise ValueError('wrong approval activation socket address or type')
            s.setblocking(True)
            s.set_inheritable(False)
            return s
        except BaseException:
            s.close()
            raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'owner approval custody refused: {exc}') from exc
    finally:
        for key in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
            os.environ.pop(key, None)


def restart_worker():
    """Manager-account self-update; no privileged systemctl operation or sudo.

    Read PID from the authenticated system manager, check kernel UID, then signal
    only that exact same-UID worker. Restart=always replaces it while PID 1 keeps
    the socket. Agent/session UIDs cannot pass this check or signal the worker.
    """
    import signal
    require_custody()
    service = _show(SERVICE)
    pid = int(service.get('MainPID', '0'))
    if pid <= 1:
        return  # the next connection activates the worker
    status = Path(f'/proc/{pid}/status').read_text()
    uid = next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1:]
    if os.getuid() == 0 or uid != [str(os.getuid())] * 4:
        raise ValueError('only the protected manager account can restart its worker')
    # pidfd pins the process across PID reuse. Supported Linux is required.
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('manager restart requires pidfd-capable Linux Python; custody retained')
    fd = os.pidfd_open(pid)
    try:
        if _show(SERVICE).get('MainPID') != str(pid):
            raise ValueError('manager worker changed during restart; retry')
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)


def http_server(handler):
    from http.server import ThreadingHTTPServer
    s = inherited_listener()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), handler, bind_and_activate=False)
    server.socket.close()
    server.socket = s
    server.server_address = s.getsockname()
    server.server_name = 'localhost'
    server.server_port = PORT
    return server
