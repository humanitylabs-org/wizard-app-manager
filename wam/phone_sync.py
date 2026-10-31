"""Mirror the public Android release onto this box. No installs, restarts, or maintain-mode decisions."""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import updates

# Read from the public App Manager release mirror, beside the box snapshots; wizard-os (canonical) is private.
POINTER = "https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases/phone/latest.json"
SIGNER = "3bf74d0b597bc170761079909d3692428702ba057f753ee1ffd62da3c76ed1fd"
UNIT = "wizard-phone-sync"
MAX_POINTER = 64 * 1024
MAX_APK = 200 * 1024 * 1024
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
HEX = re.compile(r"[0-9a-fA-F]{64}")
APK_NAME = re.compile(r"wizard-os-[0-9A-Za-z.+-]+\.apk")


def _version(value):
    if not isinstance(value, str) or len(value) > 80 or not (match := SEMVER.fullmatch(value)):
        raise ValueError("invalid semantic version")
    pre = match[4]
    if pre and any(not part or (part.isdigit() and len(part) > 1 and part[0] == '0') for part in pre.split('.')):
        raise ValueError("invalid prerelease")
    # SemVer: a stable release sorts after every prerelease of that release.
    return (*map(int, match.group(1, 2, 3)), (1,) if not pre else (0, *[(0, int(p)) if p.isdigit() else (1, p) for p in pre.split('.')]))


def validate(pointer, *, fixture=False):
    if not isinstance(pointer, dict) or pointer.get('app') != 'ai.wizards.os':
        raise ValueError("wrong app")
    if type(pointer.get('versionCode')) is not int or not 0 < pointer['versionCode'] <= 2147483647:
        raise ValueError("invalid versionCode")
    version = pointer.get('versionName')
    _version(version)
    _version(pointer.get('min_box_snapshot'))
    if type(pointer.get('bytes')) is not int or not 0 < pointer['bytes'] <= MAX_APK:
        raise ValueError("invalid APK size")
    if not isinstance(pointer.get('sha256'), str) or not HEX.fullmatch(pointer['sha256']):
        raise ValueError("invalid sha256")
    if pointer.get('signer') != SIGNER:
        raise ValueError("unexpected signer")
    if not isinstance(pointer.get('source_commit'), str) or not re.fullmatch(r'[0-9a-fA-F]{40}', pointer['source_commit']):
        raise ValueError("invalid source_commit")
    if not isinstance(pointer.get('notes', ''), str):
        raise ValueError("invalid notes")
    url = pointer.get('asset_url')
    if not isinstance(url, str):
        raise ValueError("missing asset URL")
    parsed = urllib.parse.urlsplit(url)
    allowed = (parsed.scheme == 'https' and parsed.netloc == 'github.com'
               and parsed.path == f'/humanitylabs-org/wizard-ux/releases/download/android-v{version}/wizard-os-{version}.apk'
               and not parsed.query and not parsed.fragment)
    local = fixture and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and not parsed.username
    if not (allowed or local):
        raise ValueError("untrusted asset URL")
    return pointer


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, fixture):
        self.fixture = fixture

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' and not (self.fixture and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'):
            raise ValueError("unsafe redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(opener, url, timeout):
    response = opener.open(urllib.request.Request(url, headers={'Accept': 'application/json' if url.endswith('.json') else 'application/octet-stream',
                                                             'Accept-Encoding': 'identity'}), timeout=timeout)
    if response.status != 200:
        response.close()
        raise ValueError("download must return 200")
    return response


def _verify(path, size, digest):
    try:
        if path.is_symlink() or path.stat().st_size != size:
            return False
        h = hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest() == digest.lower()
    except OSError:
        return False


def _atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix='.phone-feed-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f); f.write('\n'); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def sync(home: Path, runtipi_root: Path, *, pointer_url=POINTER, fixture=False):
    """Return one outcome; caller logs one line. fixture permits loopback HTTP in hermetic tests only."""
    directory = Path(runtipi_root) / 'app-data/wizard-os/phone'
    opener = urllib.request.build_opener(_Redirect(fixture))
    try:
        with _open(opener, pointer_url, 20) as response:
            raw = response.read(MAX_POINTER + 1)
            if len(raw) > MAX_POINTER:
                raise ValueError('pointer too large')
        p = validate(json.loads(raw), fixture=fixture)
        installed = updates.installed(home)
        if installed is None or _version(installed) < _version(p['min_box_snapshot']):
            return 'skipped:min_box_snapshot'
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.parent.chmod(0o755)  # same public projection modes as cli.share_registry
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
        with (directory / '.sync.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'skipped:busy'
            feed = directory / 'latest.json'
            current = json.loads(feed.read_text()) if feed.exists() else {}
            code = current.get('versionCode', 0)
            if type(code) is not int or code < 0:
                raise ValueError('unreadable existing versionCode')
            if code > p['versionCode']:
                return 'skipped:no_downgrade'
            name = f"wizard-os-{p['versionName']}.apk"
            apk = directory / name
            if code == p['versionCode'] and current.get('sha256', '').lower() == p['sha256'].lower() and current.get('url') == name and _verify(apk, p['bytes'], p['sha256']):
                return 'unchanged'
            # A versioned URL is immutable. Never replace bytes a still-published old feed references.
            if current.get('url') == name and current.get('sha256', '').lower() != p['sha256'].lower():
                raise ValueError('conflicting immutable APK URL')
            fd, tmp = tempfile.mkstemp(prefix='.phone-apk-', dir=directory)
            try:
                total, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 300
                with os.fdopen(fd, 'wb') as out, _open(opener, p['asset_url'], 30) as response:
                    declared = response.headers.get('Content-Length')
                    if declared is not None and int(declared) != p['bytes']:
                        raise ValueError('APK Content-Length mismatch')
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > min(MAX_APK, p['bytes']) or time.monotonic() > deadline:
                            raise ValueError('APK limit exceeded')
                        digest.update(chunk); out.write(chunk)
                    if total != p['bytes'] or digest.hexdigest() != p['sha256'].lower():
                        raise ValueError('APK bytes/sha256 mismatch')
                    out.flush(); os.fsync(out.fileno()); os.fchmod(out.fileno(), 0o644)
                os.replace(tmp, apk)
            finally:
                Path(tmp).unlink(missing_ok=True)
            _atomic_json(feed, {k: p[k] for k in ('app', 'versionName', 'versionCode', 'bytes', 'sha256', 'signer')} |
                         {'version': p['versionName'], 'url': name, 'notes': p.get('notes', '')})
            for old in directory.glob('wizard-os-*.apk'):
                if old.name != name and APK_NAME.fullmatch(old.name):
                    old.unlink()
            return f"updated:versionCode={p['versionCode']}"
    except urllib.error.HTTPError as e:
        return 'skipped:pointer_404' if e.code == 404 and e.url == pointer_url else f'skipped:http_{e.code}'
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        # Offline and invalid releases never break maintenance. Do not log remote text or URLs.
        return 'skipped:unavailable_or_invalid'


def ensure_timer(home: Path, runtipi_root: Path, *, unit_dir=None, runner=subprocess.run):
    """Always enabled, independent of OS update/maintain mode. Called on every installer rerun."""
    units = Path(unit_dir or Path.home() / '.config/systemd/user')
    units.mkdir(parents=True, exist_ok=True)
    # systemd quoting, not shell quoting; preserve spaces and literal percent specifiers.
    quote = lambda v: '"' + str(v).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    (units / f'{UNIT}.service').write_text(
        '[Unit]\nDescription=Mirror the Wizard phone release (no box changes)\nAfter=network-online.target\n\n'
        '[Service]\nType=oneshot\nEnvironment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin\n'
        f'Environment={quote("WIZARD_OS_HOME=" + str(home))}\nEnvironment={quote("RUNTIPI_ROOT=" + str(runtipi_root))}\n'
        f'ExecStart={quote(Path.home() / ".local/bin/wam")} phone-sync\nTimeoutStartSec=6min\n')
    (units / f'{UNIT}.timer').write_text(
        '[Unit]\nDescription=Hourly Wizard phone release mirror\n\n'
        '[Timer]\nOnBootSec=2min\nOnCalendar=hourly\nRandomizedDelaySec=5min\nPersistent=true\n\n'
        '[Install]\nWantedBy=timers.target\n')
    for args in (['daemon-reload'], ['enable', '--now', f'{UNIT}.timer']):
        result = runner(['systemctl', '--user', *args], capture_output=True, timeout=60)
        if result.returncode:
            raise OSError('could not enable phone sync timer')
    return f'timer:{UNIT}.timer'
