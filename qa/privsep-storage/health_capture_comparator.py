#!/usr/bin/env python3
"""Staging-only: build sealed public comparator snapshots + migration allowlist.

Read-only public fetches (GitHub raw/API) plus local git objects. Writes only
under the given output directory. Never touches a VM, Docker or forum.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

REPO = '/home/telos/workspace/wam-privsep'
B = '595523fb8f930d387d5cfb0a7aa6c7d7031c05e2'
U_TAG = 'd377b7c52567c8a852f095d5b6dd8194c66f5a62'
U_COMMIT = '5e4d7a5bb1d537782191fc1fbcb7e30c0423d566'
CLI_GITLINK = 'c5d951ed0c92c57964b607d4e9e74db820a94a84'
TEMPLATE_SHA = 'f7da61f9db29bc4a800e53b298554f72bb6a636fe9c438519e3ef50b5202ca09'
DRIZZLE = 'packages/backend/src/core/database/drizzle'
RAW = 'https://raw.githubusercontent.com/runtipi/runtipi/' + U_COMMIT + '/' + DRIZZLE + '/'
CLI_ASSET = 'runtipi-cli-linux-x86_64.tar.gz'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fetch(url, cap=8388608):
    req = urllib.request.Request(url, headers={'User-Agent': 'privsep-health-capture-staging'})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read(cap + 1)
    assert len(raw) <= cap, url
    return raw


def git(*args):
    return subprocess.run(['git', '-C', REPO, *args], check=True, capture_output=True, timeout=60).stdout


def journal(rev, raw_journal=None, fetch_sql=False):
    raw = raw_journal if raw_journal is not None else git('show', rev + ':' + DRIZZLE + '/meta/_journal.json')
    doc = json.loads(raw); rows = []
    for e in doc['entries']:
        tag = e['tag']; when = int(e['when'])
        local = git('show', rev + ':' + DRIZZLE + '/' + tag + '.sql')
        if fetch_sql:
            remote = fetch(RAW + tag + '.sql')
            assert remote == local, 'public SQL differs from local tag object: ' + tag
        # drizzle-orm readMigrationFiles: hash = sha256(file text) (utf8 round-trip verified)
        assert local.decode('utf-8').encode('utf-8') == local
        rows.append({'tag': tag, 'sha256': sha(local), 'when': when})
    return raw, rows


def main(out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    assert git('rev-parse', U_TAG + '^{commit}').decode().strip() == U_COMMIT
    ref = json.loads(fetch('https://api.github.com/repos/runtipi/runtipi/git/refs/tags/v4.11.0'))
    assert ref['object']['sha'] == U_TAG and ref['object']['type'] == 'tag'
    setup = fetch('https://setup.runtipi.io', 1048576)
    latest = json.loads(fetch('https://api.github.com/repos/runtipi/runtipi/releases/latest'))
    asset = next(a for a in latest['assets'] if a['name'] == CLI_ASSET)
    cli = fetch(asset['browser_download_url'], 16777216)
    assert latest['tag_name'] == 'v4.11.0', 'latest release moved: ' + latest['tag_name']
    assert asset.get('digest') == 'sha256:' + sha(cli) and asset['size'] == len(cli)
    template = fetch('https://raw.githubusercontent.com/runtipi/cli/' + CLI_GITLINK + '/internal/assets/docker-compose.yml')
    assert sha(template) == TEMPLATE_SHA
    up_raw = fetch(RAW + 'meta/_journal.json')
    assert up_raw == git('show', U_TAG + ':' + DRIZZLE + '/meta/_journal.json')
    _, upstream = journal(U_TAG, up_raw, fetch_sql=True)
    _, pinned = journal(B)
    for row in pinned:          # names shared with upstream must agree, else both are kept distinct
        u = next((r for r in upstream if r['tag'] == row['tag']), None)
        row['same_as_upstream'] = bool(u and u['sha256'] == row['sha256'] and u['when'] == row['when'])
    import io
    with tarfile.open(fileobj=io.BytesIO(cli)) as t:
        members = t.getmembers(); assert len(members) == 1 and members[0].isfile()
        binary = t.extractfile(members[0]).read()
    files = {'comparator-setup.sh': setup, 'comparator-cli.tar.gz': cli, 'comparator-cli-template.yml': template,
             'comparator-latest-release.json': (json.dumps({'tag_name': latest['tag_name'], 'published_at': latest['published_at'],
                 'target_commitish': latest.get('target_commitish'), 'tag_ref_object': ref['object']['sha'],
                 'assets': [{'name': a['name'], 'size': a['size'], 'digest': a.get('digest')} for a in latest['assets']]},
                 sort_keys=True, indent=2) + '\n').encode(),
             'comparator-upstream-journal.json': up_raw}
    for n, raw in files.items():
        (out / n).write_bytes(raw); (out / n).chmod(0o600)
    allow = {'names': sorted({r['tag'] for r in pinned} | {r['tag'] for r in upstream}),
             'pinned': {r['tag']: [r['sha256'], r['when']] for r in pinned},
             'upstream_v4_11_0': {r['tag']: [r['sha256'], r['when']] for r in upstream}}
    allow_sha = sha(json.dumps(allow, sort_keys=True, separators=(',', ':')).encode())
    doc = {'schema': 'privsep-health-capture-comparator-v1', 'label': 'current-upstream-sealed-at-run',
           'note': 'public comparator snapshots taken at staging; NOT historical 610 identity (unrecoverable)',
           'setup_url': 'https://setup.runtipi.io', 'setup_redirect_target': 'runtipi/runtipi master scripts/install.sh',
           'setup_sha256': sha(setup), 'latest_release_tag': latest['tag_name'],
           'v4_11_0_tag_object': U_TAG, 'v4_11_0_commit': U_COMMIT, 'cli_gitlink': CLI_GITLINK,
           'cli_v4_11_0_tarball_sha256': sha(cli), 'cli_v4_11_0_binary_sha256': sha(binary),
           'cli_v4_11_0_binary_bytes': len(binary), 'cli_template_sha256': TEMPLATE_SHA,
           'pinned_backend_revision': B, 'pinned_migrations': len(pinned), 'upstream_migrations': len(upstream),
           'pinned_only': sorted({r['tag'] for r in pinned} - {r['tag'] for r in upstream}),
           'upstream_only': sorted({r['tag'] for r in upstream} - {r['tag'] for r in pinned}),
           'shared_differing': sorted(r['tag'] for r in pinned if not r['same_as_upstream'] and
                                      any(u['tag'] == r['tag'] for u in upstream)),
           'migration_allowlist': allow, 'migration_allowlist_sha256': allow_sha,
           'migration_hash_rule': 'drizzle-orm readMigrationFiles: sha256(sql file text); created_at=journal when',
           'files': {n: {'sha256': sha(raw), 'bytes': len(raw)} for n, raw in files.items()}}
    (out / 'comparator.json').write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n'); (out / 'comparator.json').chmod(0o600)
    print(json.dumps({k: v for k, v in doc.items() if k not in ('migration_allowlist', 'files')}, sort_keys=True, indent=1))


if __name__ == '__main__':
    main(sys.argv[1])
