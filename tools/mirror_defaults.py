#!/usr/bin/env python3
"""Append-only curator custody, DECISIONS #83. Run only on a trusted Thales job.

No builds. Source history lives in public WAM release bundles, OCI manifests and
layers in GHCR. Existing copies survive missing origins; no force push/retag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import urllib.parse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import index, mirrors, private, wizardmd


def run(*args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True, stderr=subprocess.PIPE).strip()


def collect(root, catalog, defaults_path=None):
    """Active immutable releases + the indexed commits of today's recommendations."""
    pins, images, unpublished = set(), set(), []
    components = set()  # (repo, commit) of channel components, e.g. host-installed Troubleshooter
    for channel in ("stable", "latest"):
        version = (root / "releases" / channel).read_text().strip()
        if not version or '/' in version or '..' in version:
            raise ValueError("invalid channel pointer")
        release = json.loads((root / "releases" / f"{version}.json").read_text())
        for row in release.get("default_apps", {}).values():
            if row is None:  # a slot the release deliberately leaves empty (e.g. agent-history)
                continue
            mirrors.source_ref(row["repo"], row["commit"])
            pins.add((row["repo"], row["commit"]))
        for row in release.get("components", {}).values():
            if isinstance(row, dict) and row.get("repo") and row.get("commit"):
                components.add((row["repo"], row["commit"]))
        for row in [*release.get("images", {}).values(), (release.get("hermes") or {}).get("image")]:
            if row:
                images.add(row['ref'].split('@')[0] + '@' + row['digest'])
    defaults = json.loads((defaults_path or root / "wam" / "defaults.json").read_text())["slots"]
    for row in defaults.values():
        if not row.get("app"):
            continue
        matches = [r for r in catalog if r.get('id') == row['app'] and
                   (not row.get('repo') or row['repo'] == r.get('repo'))]
        if len(matches) > 1:
            raise ValueError(f"ambiguous default {row['app']}")
        if matches:
            rec = matches[0]
            commit = rec['install']['commit']
            mirrors.source_ref(rec['repo'], commit)
            pins.add((rec['repo'], commit))
        elif any(repo == row.get('repo') for repo, sha in components):
            # A default that is not an index app (host tooling such as the Troubleshooter) is
            # pinned by the channel releases' components; copy exactly those commits.
            for repo, sha in components:
                if repo == row['repo']:
                    mirrors.source_ref(repo, sha)
                    pins.add((repo, sha))
        elif not any(repo == row.get('repo') for repo, sha in pins):
            if row.get('install') != 'on-demand':
                raise ValueError(f"no pinned publication for default {row['app']}")
            unpublished.append(row['app'])
    return sorted(pins), images, unpublished


REPOSITORY = 'humanitylabs-org/wizard-app-manager'
RELEASE_TAG = 'curator-source-mirrors'


def api_json(url, data=None, binary=False):
    token = os.environ.get('GH_TOKEN', '')
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
               'User-Agent': 'wizard-default-curator'}
    if data is not None:
        headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
        data = data if binary else json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=180) as response:
        return json.load(response)


def source_release():
    base = f'https://api.github.com/repos/{REPOSITORY}/releases'
    try:
        release = api_json(f'{base}/tags/{RELEASE_TAG}')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        release = api_json(base, {'tag_name': RELEASE_TAG, 'target_commitish': os.environ.get('GITHUB_SHA', 'main'),
                                 'name': 'Curator source mirrors (not WAM binaries)', 'draft': False,
                                 'prerelease': True, 'make_latest': 'false',
                                 'body': 'DECISIONS #83: append-only exact Git bundles for default apps. Do not delete or replace assets.'})
    if release['draft'] or not release['prerelease']:
        raise ValueError('custody release must be public and separate from production latest')
    assets = {}
    page = 1
    while True:
        batch = api_json(f"{base}/{release['id']}/assets?per_page=100&page={page}")
        for asset in batch:
            if asset['name'] in assets:
                raise ValueError('duplicate custody asset name')
            assets[asset['name']] = asset
        if len(batch) < 100:
            break
        page += 1
    return release, assets


def ensure_source(repo, commit, work, env, release, assets):
    ref = mirrors.source_ref(repo, commit)
    name = mirrors.source_name(repo, commit)
    bundle = work / name
    cache = work / (name + '.git')
    if name not in assets:
        metadata = private._api(f'/repos/{repo}', os.environ.get('GH_TOKEN', ''))
        if metadata.get('private') is not False:
            raise ValueError(f"refusing to expose nonpublic source {repo}")
        cache.mkdir(parents=True, exist_ok=True)
        run('git', 'init', '-q', '--bare', str(cache))
        run('git', 'fetch', '-q', f'https://github.com/{repo}.git', commit, cwd=cache, env=env)
        head = run('git', 'rev-parse', 'FETCH_HEAD', cwd=cache)
        if head != commit:
            raise ValueError(f"source mismatch: {head} != {commit}")
        run('git', 'update-ref', ref, commit, cwd=cache)
        run('git', 'bundle', 'create', str(bundle), ref, cwd=cache)
        if bundle.stat().st_size > mirrors.BUNDLE_LIMIT_BYTES:
            raise ValueError('curator bundle exceeds size limit')
        url = f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release['id']}/assets?name={urllib.parse.quote(name)}"
        assets[name] = api_json(url, bundle.read_bytes(), binary=True)
    # Anonymous readback into a FRESH repository proves a standalone complete
    # bundle, not objects accidentally satisfied by the builder's fetch cache.
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir()
    run('git', 'init', '-q', '--bare', str(cache))
    mirrors.download_bundle(repo, commit, bundle)
    run('git', 'fetch', '-q', str(bundle), ref, cwd=cache)
    head = run('git', 'rev-parse', 'FETCH_HEAD', cwd=cache)
    if head != commit:
        raise ValueError(f"mirror source mismatch: {head} != {commit}")
    run('git', 'fsck', '--full', cwd=cache)
    manifest = run('git', 'show', f'{commit}:wizard.md', cwd=cache)
    rt = wizardmd.front_matter(manifest + '\n').get('runtime') or {}
    path = rt.get('compose')
    if not isinstance(path, str) or path.startswith('/') or '..' in path.split('/'):
        raise ValueError(f'invalid compose path for {repo}')
    compose = yaml.safe_load(run('git', 'show', f'{commit}:{path}', cwd=cache))
    record = {'repo': repo, 'commit': commit, 'url': mirrors.source_url(repo, commit),
              'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest()}
    return private.images(compose), record


def anonymous_image_digest(image):
    with tempfile.TemporaryDirectory() as config:
        env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'DOCKER_AUTH_CONFIG')}
        env['DOCKER_CONFIG'] = config
        return run('crane', 'digest', image, env=env)


def ensure_image(image):
    mirror = mirrors.image_ref(image)
    if not mirror:
        if image.startswith('ghcr.io/humanitylabs-org/'):
            # Already in curator custody. Still verify the pinned manifest exists.
            anonymous_image_digest(image)
            return image
        raise ValueError(f'not digest-pinned: {image}')
    digest = image.rsplit('@', 1)[1]
    target = mirror.split('@')[0]
    # Check immutable TAG, not just @digest (a stale conflicting tag must fail).
    try:
        found = run('crane', 'digest', target)
    except subprocess.CalledProcessError as e:
        error = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr or ''
        # A denied lookup is not absence. First package publication can return NAME_UNKNOWN.
        if not any(code in error for code in ('MANIFEST_UNKNOWN', 'NAME_UNKNOWN', '404 Not Found')):
            raise
        run('crane', 'copy', image, target)
        found = run('crane', 'digest', target)
    if found != digest:
        raise ValueError(f'image digest mismatch: {found} != {digest}')
    if anonymous_image_digest(mirror) != digest:
        raise ValueError('anonymous image readback differs from pinned digest')
    return mirror


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True, help='wizard-os policy checkout containing releases/')
    args = p.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    report = {'state': 'running', 'sources': [], 'images': [], 'unpublished': [], 'errors': []}
    def checkpoint():
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    try:
        catalog = index.all_listed(index.DEFAULT_INDEX)
        pins, images, report['unpublished'] = collect(args.root, catalog, Path(__file__).resolve().parents[1] / 'wam/defaults.json')
        release, assets = source_release()
        report['expected_sources'] = len(pins)
        checkpoint()
        for repo, commit in pins:
            try:
                refs, record = ensure_source(repo, commit, args.work, private._git_env(os.environ.get('GH_TOKEN', '')), release, assets)
                images.update(refs)
                report['sources'].append(record)
            except Exception as e:
                report['errors'].append(f'{repo}@{commit}: {type(e).__name__}: {e}')
            checkpoint()
        report['expected_images'] = len(images)
        for image in sorted(images):
            try:
                report['images'].append({'origin': image, 'mirror': ensure_image(image)})
            except Exception as e:
                report['errors'].append(f'{image}: {type(e).__name__}: {e}')
            checkpoint()
        report['state'] = 'failed' if report['errors'] else 'complete'
    except Exception as e:
        report['errors'].append(f'{type(e).__name__}: {e}')
        report['state'] = 'failed'
    finally:
        checkpoint()
    print(json.dumps(report, indent=2))
    return bool(report['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
