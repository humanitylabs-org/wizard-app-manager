"""Mirror the public Android release onto this box. No installs, restarts, or maintain-mode decisions.

A box on For testers also mirrors the newest phone tester
build (an `android-v<version>` GitHub prerelease, wizard-os#720), with the same checks (#799)."""
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

from . import apk_sig, updates

# Read from the public App Manager release mirror, beside the box snapshots; wizard-os (canonical) is private.
POINTER = "https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases/phone/latest.json"
# The phone.json of the newest published wizard-ux release. The Phone release workflow (wizard-ux#1400) publishes
# android-v<version> from main but cannot edit the pointer above, so without this a CI release never reached a box
# (wizard-ux#1399). Prereleases and drafts are never "latest"; validate() still pins the signer and the asset URL.
RELEASE_POINTER = "https://github.com/humanitylabs-org/wizard-ux/releases/latest/download/phone.json"
# sha256 of the WizardOS phone signing certificate. The APK's own v2/v3 signing block must carry it with a valid
# signature (apk_sig.verify, #789); a release's `signer` field is only a hint.
SIGNER = "3bf74d0b597bc170761079909d3692428702ba057f753ee1ffd62da3c76ed1fd"
# A CI release may run at most this far ahead of the reviewed pointer; bigger jumps go through a pointer PR (#789),
# so one forged or mistaken huge versionCode can never freeze the feed above every real release.
MAX_CI_LEAD = 20
# Tester builds (#799): with release lanes on (wizard-ux WOS_RELEASE_LANES) every phone merge is published as a
# GitHub prerelease, which `releases/latest` never names. A box on For testers (channel `latest`, the owner's opt-in)
# also reads the newest android-v* prerelease's phone.json from this list.
RELEASES = "https://api.github.com/repos/humanitylabs-org/wizard-ux/releases?per_page=30"
TESTER_ASSET = "https://github.com/humanitylabs-org/wizard-ux/releases/download/android-v{version}/phone.json"
MAX_RELEASES = 4 * 1024 * 1024
TESTER_CANDIDATES = 3   # newest tester builds read per run
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


def _pointer(opener, url, fixture):
    with _open(opener, url, 20) as response:
        raw = response.read(MAX_POINTER + 1)
        if len(raw) > MAX_POINTER:
            raise ValueError('pointer too large')
    return validate(json.loads(raw), fixture=fixture)


def _outcome(error, url):
    if isinstance(error, urllib.error.HTTPError):
        return 'skipped:pointer_404' if error.code == 404 and error.url == url else f'skipped:http_{error.code}'
    # Offline and invalid releases never break maintenance. Do not log remote text or URLs.
    return 'skipped:unavailable_or_invalid'


_SKIP = (urllib.error.HTTPError, OSError, ValueError, TypeError, AttributeError, http.client.HTTPException)


def _signed(path):
    try:
        apk_sig.verify(path, SIGNER)
        return True
    except (OSError, ValueError):
        return False


def _current(directory):
    feed = directory / 'latest.json'
    current = json.loads(feed.read_text()) if feed.exists() else {}
    code = current.get('versionCode', 0)
    if type(code) is not int or code < 0:
        raise ValueError('unreadable existing versionCode')
    return current, code


def _publish(directory, opener, p, ceiling=None):
    feed = directory / 'latest.json'
    current, code = _current(directory)
    # A feed above the ceiling came from a release past the cap (before #789); it never blocks real releases.
    if code > p['versionCode'] and (ceiling is None or code <= ceiling):
        return 'skipped:no_downgrade'
    name = f"wizard-os-{p['versionName']}.apk"
    apk = directory / name
    if (code == p['versionCode'] and current.get('sha256', '').lower() == p['sha256'].lower() and current.get('url') == name
            and _verify(apk, p['bytes'], p['sha256']) and _signed(apk)):
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
        # The bytes match what the release says; now check who really signed them (#789).
        apk_sig.verify(tmp, SIGNER)
        os.replace(tmp, apk)
    finally:
        Path(tmp).unlink(missing_ok=True)
    _atomic_json(feed, {k: p[k] for k in ('app', 'versionName', 'versionCode', 'bytes', 'sha256', 'signer')} |
                 {'version': p['versionName'], 'url': name, 'notes': p.get('notes', '')})
    for old in directory.glob('wizard-os-*.apk'):
        if old.name != name and APK_NAME.fullmatch(old.name):
            old.unlink()
    return f"updated:versionCode={p['versionCode']}"


def tester_builds_on(home) -> bool:
    """Whether this box takes phone tester builds: it is on For testers (update channel `latest`). False on For
    everyone and whenever the channel can't be read."""
    try:
        return updates.channel(home) == "latest"
    except Exception:  # noqa: BLE001 - a channel read never breaks the public mirror
        return False


def tester_pointer_urls(opener, url, fixture, limit=TESTER_CANDIDATES):
    """[(version, phone.json URL)] of the newest published (not draft) android-v<version> prereleases that carry a
    phone.json, newest first, at most `limit`: when the newest tester APK fails its checks, the one before it is next."""
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'Accept-Encoding': 'identity',
                                               'X-GitHub-Api-Version': '2022-11-28'})
    with opener.open(req, timeout=20) as response:
        if response.status != 200:
            raise ValueError('release list must return 200')
        raw = response.read(MAX_RELEASES + 1)
    if len(raw) > MAX_RELEASES:
        raise ValueError('release list too large')
    best = []
    for r in json.loads(raw):
        if not isinstance(r, dict):
            continue
        tag = r.get('tag_name')
        if r.get('prerelease') is not True or r.get('draft') is not False or not (isinstance(tag, str) and tag.startswith('android-v')):
            continue
        version = tag.removeprefix('android-v')
        try:
            key = _version(version)
        except ValueError:
            continue
        assets = [a for a in r.get('assets') or [] if isinstance(a, dict) and a.get('name') == 'phone.json']
        if not assets:
            continue
        link = assets[0].get('browser_download_url')
        parsed = urllib.parse.urlsplit(link if isinstance(link, str) else '')
        local = fixture and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and not parsed.username
        if not (link == TESTER_ASSET.format(version=version) or local):
            continue
        best.append((key, version, link))
    best.sort(key=lambda b: b[0], reverse=True)
    return [(version, link) for _, version, link in best[:limit]]


def sync(home: Path, runtipi_root: Path, *, pointer_url=None, release_url=None, fixture=False, seen=None,
         tester=None, releases_url=None):
    """Return one outcome; caller logs one line. fixture permits loopback HTTP in hermetic tests only.

    Reads the mirror pointer and the newest wizard-ux release's phone.json and mirrors the highest valid
    versionCode the box admits. A test that passes only pointer_url reads only that pointer.
    `seen` (a dict, optional) gets `read` (a release source answered), `newest` (the newest valid release within the
    CI lead cap, also when this box's snapshot gate holds it back) and `rejected` (candidates whose APK failed),
    which the feed watchdog (`run`) compares with what the feeds serve.
    `tester` (default: the box is on For testers, never read when a test passes pointer_url) also reads the newest phone tester
    build (prerelease) as one more CI candidate: same validation, versionCode cap, snapshot gate, sha256/size and
    real signer; `seen['tester']` names it. Without it the release list is never fetched."""
    directory = Path(runtipi_root) / 'app-data/wizard-os/phone'
    opener = urllib.request.build_opener(_Redirect(fixture))
    urls = [pointer_url or POINTER] + ([release_url] if release_url else [] if pointer_url else [RELEASE_POINTER])
    if tester is None:
        tester = pointer_url is None and tester_builds_on(home)
    want = {}
    if tester:
        try:
            got = tester_pointer_urls(opener, releases_url or RELEASES, fixture)
        except _SKIP:
            got = []   # no list (offline, GitHub's hourly limit): the public release still syncs
        for version, link in got:
            want[len(urls)] = version
            urls.append(link)
    found, failures, reviewed = [], [], None
    for i, url in enumerate(urls):
        try:
            p = _pointer(opener, url, fixture)
            if i in want and p['versionName'] != want[i]:
                raise ValueError('tester phone.json names another version')
        except _SKIP as e:
            failures.append(_outcome(e, url))
            continue
        if i == 0:
            reviewed = p['versionCode']
        if i in want and seen is not None and 'tester' not in seen:
            seen['tester'] = p['versionName']   # the newest tester build read
        found.append((i == 0, p))
    if not found:
        return failures[0]
    if seen is not None:
        seen['read'] = True
    try:
        _, mirrored = _current(directory)
    except _SKIP:
        mirrored = 0
    # The reviewed pointer is the authority for big jumps (#789). Without it (pointer offline), the CI release may
    # still lead what this box already mirrors by the same margin; a fresh box with no pointer has no baseline.
    base = reviewed if reviewed is not None else (mirrored or None)
    ceiling = None if base is None else base + MAX_CI_LEAD
    found = [p for is_pointer, p in found if is_pointer or ceiling is None or p['versionCode'] <= ceiling]
    if not found:
        return 'skipped:ci_release_too_far_ahead'
    # Only the reviewed pointer may lift a feed that sits above the ceiling.
    ceiling = None if reviewed is None else ceiling
    if seen is not None:
        # The release phones should get. A box whose snapshot the newest release refuses (or doesn't know) still
        # counts it, so a feed held back by that gate is reported too, not left silent.
        seen['newest'] = max(found, key=lambda c: c['versionCode'])
    try:
        installed = updates.installed(home)
        admitted = [p for p in found if installed is not None and _version(installed) >= _version(p['min_box_snapshot'])]
        if not admitted:
            return 'skipped:min_box_snapshot'
        admitted.sort(key=lambda c: c['versionCode'], reverse=True)
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.parent.chmod(0o755)  # same public projection modes as cli.share_registry
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
        with (directory / '.sync.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'skipped:busy'
            # Newest first; if its APK cannot be fetched or verified, a valid older source still gets mirrored.
            for i, p in enumerate(admitted):
                try:
                    return _publish(directory, opener, p, ceiling)
                except _SKIP as e:
                    if seen is not None:   # the watchdog names why the newest release is not on this box
                        seen.setdefault('rejected', []).append(f"{p['versionName']} ({_outcome(e, None).split(':', 1)[1]})")
                    if i == len(admitted) - 1:
                        raise
    except _SKIP as e:
        return _outcome(e, None)


# ---- The owner feed and the feed watchdog (wizard-ux#1768) ----
#
# Some boxes (James's) serve /reports/wizard-os/ from a private owner feed in the Hermes home instead of the
# mirror above. Nothing on the box ever moved that folder: only tools/publish_owner_phone.py by hand, so it sat on
# 0.10.58 while 0.10.59 and 0.10.60 shipped, and every skip here exits 0 with one line nobody reads. Now the hourly
# run also brings such an owner feed up to the verified mirror, and remembers what each feed serves against the
# newest release, so a stuck feed tells the owner (Notifications) and fails `wam doctor`.

OWNER_FEED = Path('data/apps/private-reports/wizard-os')   # under a Hermes home
# The first phone build that saves the Wizard's address on the phone (ServerSettings, 0.10.18). The shared build
# has no address baked in, so a phone older than this would open Connect after the update: an owner feed still on
# such a build is never moved here (publish_owner_phone.py with an owner build is the way), only reported.
OWNER_MIN_CODE = 82
STATE = 'phone-feed.json'                                   # in the Wizard home
BEHIND_HOURS = 3        # a feed behind the newest release this long tells the owner
BEHIND_RELEASES = 2     # ... or this many releases behind, once it has stayed behind for an hour
UNREAD_HOURS = 24       # no release source answered for this long
STALE_RUN_HOURS = 3     # `wam doctor`: the hourly mirror has not run for this long


def owner_feed_dirs(home: Path) -> list[Path]:
    """Private owner feeds this box serves (a Hermes home's private-reports/wizard-os with a latest.json)."""
    homes = [os.environ.get('HERMES_HOME'), Path.home() / '.hermes']
    try:
        from . import alongside
        homes.append(((alongside.load(Path(home)) or {}).get('agent') or {}).get('hermes_home'))
    except Exception:  # noqa: BLE001 - discovery never breaks the mirror
        pass
    out, keys = [], set()
    for h in homes:
        if not h:
            continue
        d = Path(h) / OWNER_FEED
        try:
            if d.is_symlink() or not d.is_dir() or (d / 'latest.json').is_symlink() or not (d / 'latest.json').is_file():
                continue
            key = d.resolve()
        except OSError:
            continue
        if key not in keys:
            keys.add(key)
            out.append(d)
    return out


def _read_feed(directory: Path) -> dict:
    try:
        feed = json.loads((Path(directory) / 'latest.json').read_text())
    except (OSError, ValueError):
        return {}
    return feed if isinstance(feed, dict) and type(feed.get('versionCode')) is int else {}


def _copy_apk(src: Path, apk: Path, new: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.phone-apk-', dir=apk.parent)
    try:
        with os.fdopen(fd, 'wb') as out, src.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                out.write(chunk)
            out.flush(); os.fsync(out.fileno()); os.fchmod(out.fileno(), 0o644)
        if not _verify(Path(tmp), new['bytes'], new['sha256']):
            raise ValueError('owner APK copy mismatch')
        os.replace(tmp, apk)
    finally:
        Path(tmp).unlink(missing_ok=True)


def promote_owner(owner_dir: Path, mirror_dir: Path) -> str:
    """Bring a private owner feed up to the mirror's verified release: same bytes (size, sha256, real signer),
    the old latest.json kept as latest.json.bak-<code>, never a downgrade, latest.json written last."""
    owner_dir, mirror_dir = Path(owner_dir), Path(mirror_dir)
    new = _read_feed(mirror_dir)
    name = new.get('url')
    if not new or not isinstance(name, str) or not APK_NAME.fullmatch(name) or name != f"wizard-os-{new.get('versionName')}.apk":
        return 'skipped:no_mirror'
    src = mirror_dir / name
    if not (_verify(src, new.get('bytes'), str(new.get('sha256', ''))) and _signed(src)):
        return 'skipped:mirror_unverified'
    with (owner_dir / '.owner-publish.lock').open('a') as lock:   # the lock publish_owner_phone.py takes too
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'skipped:busy'
        target = owner_dir / 'latest.json'
        previous = target.read_bytes()
        current = json.loads(previous)
        code = current.get('versionCode') if isinstance(current, dict) else None
        if type(code) is not int:
            raise ValueError('unreadable owner feed')
        apk = owner_dir / name
        if code > new['versionCode']:
            return 'unchanged'
        if code == new['versionCode']:
            # The same release: unchanged only once its APK is really there and right; the owner's own build of
            # this version (other bytes or name) is theirs and stays.
            if current.get('url') != name or str(current.get('sha256', '')).lower() != new['sha256'].lower():
                return 'unchanged'
            if _verify(apk, new['bytes'], new['sha256']) and _signed(apk):
                return 'unchanged'
            _copy_apk(src, apk, new)
            return f"repaired:versionCode={code}"
        if code < OWNER_MIN_CODE:
            return 'skipped:pre_preference'
        if apk.exists() and not _verify(apk, new['bytes'], new['sha256']):
            raise ValueError('conflicting immutable APK URL')
        if not apk.exists():
            _copy_apk(src, apk, new)
        fd, tmp = tempfile.mkstemp(prefix='.phone-feed-', dir=owner_dir)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(previous); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(), 0o644)
            os.replace(tmp, owner_dir / f'latest.json.bak-{code}')
        finally:
            Path(tmp).unlink(missing_ok=True)
        _atomic_json(target, {k: new[k] for k in ('app', 'version', 'versionName', 'versionCode', 'url', 'bytes',
                                                  'sha256', 'signer', 'notes') if k in new})
        if _read_feed(owner_dir).get('versionCode') != new['versionCode']:
            raise ValueError('owner feed readback failed')
    return f"updated:versionCode={new['versionCode']}"


def _load_state(home: Path) -> dict:
    try:
        st = json.loads((Path(home) / STATE).read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _hours(since, now) -> float:
    return (now - since) / 3600 if isinstance(since, (int, float)) else 0.0


def _tell(home: Path, title: str, message: str) -> bool:
    from . import app_updates
    from types import SimpleNamespace
    return app_updates._notify(SimpleNamespace(home=Path(home)), title, message, priority='high', required=True)


def watch(home: Path, feeds: dict, seen: dict, outcomes: dict, *, now=None, notify=None) -> list[str]:
    """Compare what each feed serves with the newest release; tell the owner once per stuck release.

    feeds: {label: directory}. Returns the labels that are behind (for the CLI line). State: <home>/phone-feed.json."""
    now = time.time() if now is None else now
    notify = notify or (lambda title, message: _tell(home, title, message))
    st = _load_state(home)
    old = st.get('feeds') if isinstance(st.get('feeds'), dict) else {}
    told = st.get('told') if isinstance(st.get('told'), dict) else {}
    newest = seen.get('newest')
    if seen.get('read'):
        st['read_at'] = now
        st.pop('told_unread', None)
    st.setdefault('first_run_at', now)
    if newest:
        prev = st.get('newest') or {}
        st['newest'] = {'versionName': newest['versionName'], 'versionCode': newest['versionCode'],
                        'seen_at': prev.get('seen_at', now) if prev.get('versionCode') == newest['versionCode'] else now}
    rows, behind = {}, []
    for label, directory in feeds.items():
        feed = _read_feed(directory)
        code, name = feed.get('versionCode', 0), feed.get('versionName') or 'none'
        row = {'versionName': name, 'versionCode': code, 'outcome': outcomes.get(label, '')}
        want = (st.get('newest') or {}).get('versionCode')
        if want and code < want:
            was = old.get(label) or {}
            row['behind_since'] = was['behind_since'] if isinstance(was.get('behind_since'), (int, float)) else now
            hours = _hours(row['behind_since'], now)
            if hours >= BEHIND_HOURS or (want - code >= BEHIND_RELEASES and hours >= 1):
                behind.append(label)
                if told.get(label) != want:
                    where = '' if label == 'mirror' else " from this Wizard's private update feed"
                    msg = (f"Your phones are offered WizardOS {name}{where}, but {st['newest']['versionName']} is out "
                           f"({int(hours)} h behind). The hourly phone update check said: {row['outcome'] or 'nothing'}. "
                           "Ask your Wizard to look at it (wam doctor).")
                    if notify('Phone app updates are stuck', msg):
                        told[label] = want
        else:
            told.pop(label, None)
        rows[label] = row
    read_at = st.get('read_at', st['first_run_at'])
    if not seen.get('read') and _hours(read_at, now) >= UNREAD_HOURS and not st.get('told_unread'):
        if notify('Phone app updates are not being checked',
                  f"This Wizard has not been able to read the phone releases from GitHub for {int(_hours(read_at, now))} h, "
                  "so your phones get no app updates. Ask your Wizard to look at it (wam doctor)."):
            st['told_unread'] = now
    st.update(checked_at=now, feeds=rows, told=told)
    try:
        if Path(home).is_dir():
            _atomic_json(Path(home) / STATE, st)
    except OSError:
        pass
    return behind


def run(home: Path, runtipi_root: Path, *, owners=None, now=None, notify=None, **kw) -> str:
    """The hourly job: mirror, bring owner feeds along, then the watchdog. Returns the one CLI line."""
    seen = {}
    outcome = sync(home, runtipi_root, seen=seen, **kw)
    mirror = Path(runtipi_root) / 'app-data/wizard-os/phone'
    why = outcome + (f"; newest not mirrored: {', '.join(seen['rejected'])}" if seen.get('rejected') else '')
    feeds, outcomes, parts = {'mirror': mirror}, {'mirror': why}, [outcome]
    if seen.get('tester'):   # this box took part in tester builds (#799): name the one it read
        parts.append(f"tester-newest:{seen['tester']}")
    for i, d in enumerate(owner_feed_dirs(home) if owners is None else owners):
        label = 'owner' if i == 0 else f'owner{i + 1}'
        try:
            res = promote_owner(d, mirror)
        except _SKIP:
            res = 'failed:unavailable_or_invalid'
        feeds[label], outcomes[label] = d, f'owner {res}; mirror {why}'
        parts.append(f'owner:{res}')
    behind = watch(home, feeds, seen, outcomes, now=now, notify=notify)
    if behind:
        parts.append('behind:' + ','.join(behind))
    return ' '.join(parts)


def doctor_row(home: Path, *, now=None, unit_dir=None):
    """`wam doctor`: is the phone update feed current, and does the hourly mirror run? (wizard-ux#1768)"""
    label = 'Phone update feed is current'
    now = time.time() if now is None else now
    st = _load_state(home)
    if not isinstance(st.get('checked_at'), (int, float)):
        timer = Path(unit_dir or Path.home() / '.config/systemd/user') / f'{UNIT}.timer'
        try:
            age = (now - timer.stat().st_mtime) / 3600
        except OSError:
            age = None
        if age is not None and age >= STALE_RUN_HOURS:
            return (label, False, f'the hourly phone mirror has never finished a run: check '
                                  f'`systemctl --user status {UNIT}.timer`, run `wam phone-sync`')
        if age is None and owner_feed_dirs(home):
            return (label, False, 'this box serves a private phone update feed, but the hourly phone mirror is not '
                                  'installed, so the feed never moves: run `wam phone-sync --ensure-timer`')
        return None   # a fresh box before its first tick, or no phone feed on this box
    if _hours(st['checked_at'], now) >= STALE_RUN_HOURS:
        return (label, False, f"the hourly phone mirror last ran {int(_hours(st['checked_at'], now))} h ago: "
                              f"check `systemctl --user status {UNIT}.timer`, run `wam phone-sync`")
    newest = st.get('newest') or {}
    late = []
    for name, row in (st.get('feeds') or {}).items():
        if newest.get('versionCode') and row.get('versionCode', 0) < newest['versionCode'] and _hours(row.get('behind_since'), now) >= 1:
            late.append(f"{name} feed offers {row.get('versionName')} ({int(_hours(row.get('behind_since'), now))} h): {row.get('outcome')}")
    if late:
        return (label, False, f"newest phone release is {newest.get('versionName')}; " + '; '.join(late))
    if _hours(st.get('read_at', st.get('first_run_at')), now) >= UNREAD_HOURS:
        return (label, False, f"could not read the phone releases from GitHub for {int(_hours(st.get('read_at', st.get('first_run_at')), now))} h")
    names = sorted({(r or {}).get('versionName') or 'none' for r in (st.get('feeds') or {}).values()})
    return (label, True, f"phones are offered {', '.join(names)}" + (f"; newest release {newest['versionName']}" if newest else ''))


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
