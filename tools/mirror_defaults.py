#!/usr/bin/env python3
"""Append-only curator custody, DECISIONS #83. Run only on a trusted Thales job.

No builds. Source history lives in public WAM curator/* tags, OCI manifests and
layers in GHCR. Existing copies survive missing origins; no force push/retag.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import index, mirrors, private, wizardmd


def run(*args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True, stderr=subprocess.PIPE).strip()


def collect(root, catalog, defaults_path=None):
    """Active immutable releases + the indexed commits of today's recommendations."""
    pins, images, unpublished = set(), set(), []
    for channel in ("stable", "latest"):
        version = (root / "releases" / channel).read_text().strip()
        if not version or '/' in version or '..' in version:
            raise ValueError("invalid channel pointer")
        release = json.loads((root / "releases" / f"{version}.json").read_text())
        for row in release.get("default_apps", {}).values():
            mirrors.source_ref(row["repo"], row["commit"])
            pins.add((row["repo"], row["commit"]))
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
        elif not any(repo == row.get('repo') for repo, sha in pins):
            if row.get('install') != 'on-demand':
                raise ValueError(f"no pinned publication for default {row['app']}")
            unpublished.append(row['app'])
    return sorted(pins), images, unpublished


def ensure_source(repo, commit, work, env):
    ref = mirrors.source_ref(repo, commit)
    remote = run('git', 'ls-remote', mirrors.SOURCE_MIRROR, ref, env=env)
    if remote and remote.split()[0] != commit:
        raise ValueError(f"immutable mirror ref mismatch: {ref}")
    cache = work / repo.replace('/', '--')
    cache.mkdir(parents=True, exist_ok=True)
    run('git', 'init', '-q', '--bare', str(cache))
    if remote:
        # The curator remains usable after the builder account disappears.
        run('git', 'fetch', '-q', mirrors.SOURCE_MIRROR, f'{ref}:{ref}', cwd=cache, env=env)
    else:
        metadata = private._api(f'/repos/{repo}', os.environ.get('GH_TOKEN', ''))
        if metadata.get('private') is not False:
            raise ValueError(f"refusing to expose nonpublic source {repo}")
        run('git', 'fetch', '-q', f'https://github.com/{repo}.git', commit, cwd=cache, env=env)
        head = run('git', 'rev-parse', 'FETCH_HEAD', cwd=cache)
        if head != commit:
            raise ValueError(f"source mismatch: {head} != {commit}")
        run('git', 'update-ref', ref, commit, cwd=cache)
        run('git', 'push', os.environ.get('SOURCE_MIRROR_PUSH_URL', mirrors.SOURCE_MIRROR), f'{ref}:{ref}', cwd=cache, env=env)
        readback = run('git', 'ls-remote', mirrors.SOURCE_MIRROR, ref, env=env)
        if readback.split()[0] != commit:
            raise ValueError('source mirror readback failed')
    # Read blobs directly; no checkout hooks, submodules, build, or app execution.
    manifest = run('git', 'show', f'{commit}:wizard.md', cwd=cache)
    rt = wizardmd.front_matter(manifest).get('runtime') or {}
    path = rt.get('compose')
    if not isinstance(path, str) or path.startswith('/') or '..' in path.split('/'):
        raise ValueError(f'invalid compose path for {repo}')
    compose = yaml.safe_load(run('git', 'show', f'{commit}:{path}', cwd=cache))
    return private.images(compose)


def ensure_image(image):
    mirror = mirrors.image_ref(image)
    if not mirror:
        if image.startswith('ghcr.io/humanitylabs-org/'):
            # Already in curator custody. Still verify the pinned manifest exists.
            run('crane', 'manifest', image)
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
    # Anonymous registry reads prove a fresh box can retrieve each copied manifest.
    return mirror


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True, help='wizard-os policy checkout containing releases/')
    args = p.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    report = {'sources': [], 'images': [], 'unpublished': [], 'errors': []}
    try:
        catalog = index.all_listed(index.DEFAULT_INDEX)
        pins, images, report['unpublished'] = collect(args.root, catalog, Path(__file__).resolve().parents[1] / 'wam/defaults.json')
        for repo, commit in pins:
            try:
                images.update(ensure_source(repo, commit, args.work, private._git_env(os.environ.get('GH_TOKEN', ''))))
                report['sources'].append({'repo': repo, 'commit': commit, 'ref': mirrors.source_ref(repo, commit)})
            except Exception as e:
                report['errors'].append(f'{repo}@{commit}: {type(e).__name__}: {e}')
        for image in sorted(images):
            try:
                report['images'].append({'origin': image, 'mirror': ensure_image(image)})
            except Exception as e:
                report['errors'].append(f'{image}: {type(e).__name__}: {e}')
    finally:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return bool(report['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
