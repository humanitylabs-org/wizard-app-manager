#!/usr/bin/env python3
"""Read-only task-stage admission probe, not the installed acceptance job.

Run as: ssh forum python3 - < scripts/manager_seal_installed_capacity.py
No container creation/inspection, content reads, image acquisition, deletion, or
remote file writes. Only lstat/statvfs of the explicitly task-owned namespace and
metadata inspection of the three already pinned public build/infra images.
"""
import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import time

RESERVE = 200 * 2**30
ALLOWANCE = 2**30  # Per-next-run additional host allocation, NOT a lifetime quota.
RETAINED_PEAK = 4 * 2**30  # James approved "Expand": this task's retained-plus-pending cap only.
ROOT = Path('/srv/work')
IMAGES = [
    'sha256:656a12855a35b53ef9bca65b3d51167a0940cc2d01b29ea5c36cb469495886dc',
    'sha256:4e32174ea7bfa3a979e37493b0b7348eb1f0c46f8201a12fd91c7db8098b0fa4',
    'sha256:908ba820e904091ddd38402404fdde212ca2f14b61b33c621dfe134d635f8ce9',
]


def inventory(root, deadline):
    seen = set()
    rows = []
    for entry in sorted(root.glob('manager-seal-*')):
        if entry.is_symlink():
            raise RuntimeError(f'refuse symlink task root: {entry}')
        row = {'path': str(entry), 'entries': 0, 'regular_file_bytes': 0, 'allocated_bytes': 0}
        def visit(path):
            if time.monotonic() > deadline:
                raise RuntimeError('task-stage metadata scan deadline exceeded')
            info = path.lstat()
            if (info.st_dev, info.st_ino) in seen:
                return
            seen.add((info.st_dev, info.st_ino))
            if len(seen) > 50000:
                raise RuntimeError('task-stage metadata scan entry cap exceeded')
            row['entries'] += 1
            row['allocated_bytes'] += info.st_blocks * 512
            if stat.S_ISREG(info.st_mode):
                row['regular_file_bytes'] += info.st_size
        visit(entry)
        if entry.is_dir():
            def onerror(error):
                raise error
            for directory, dirs, files in os.walk(entry, followlinks=False, onerror=onerror):
                for name in dirs + files:
                    visit(Path(directory)/name)
        rows.append(row)
    return rows


def admission(retained, free, *, already_allocated=0, next_peak=ALLOWANCE):
    """All values are allocated bytes. Retained is already deducted from free.

    next_peak includes stage, logs, artifacts, tool copies and overlay metadata.
    already_allocated is this invocation's footprint already present in retained.
    Never relabel old receipts or reconstruct/reset historical write totals.
    """
    assert 0 <= already_allocated <= next_peak <= ALLOWANCE
    pending = next_peak - already_allocated
    blockers = []
    if retained + pending > RETAINED_PEAK:
        blockers.append('retained allocation plus next unallocated peak exceeds 4GiB')
    if free < RESERVE + pending:
        blockers.append('free below 200GiB reserve plus not-yet-allocated footprint')
    return {'next_run_additional_peak_bytes': next_peak, 'next_run_already_allocated_bytes': already_allocated,
            'not_yet_allocated_bytes': pending, 'retained_plus_next_peak_bytes': retained+pending,
            'retained_plus_next_peak_limit_bytes': RETAINED_PEAK,
            'free_required_bytes': RESERVE+pending, 'blockers': blockers, 'admitted': not blockers,
            'accounting': 'per-run additional allocation; no lifetime quota; existing retained already deducted from free'}


def smaller_envelope_assessment(footprint, candidate=512*2**20):
    """An observation is not a bound. Do not auto-admit a smaller estimate.

    Existing controller categories alone exceed 512MiB before margin. In
    addition, Docker metadata has no task-local allocation quota and the
    controller/guard/receipt outputs lack a single aggregate pre-write fence.
    The current authority permits at most ALLOWANCE; it does not prove that
    either its estimate or a smaller candidate is a filesystem hard quota.
    """
    declared = sum(footprint.values())
    assert declared == ALLOWANCE and 0 < candidate <= ALLOWANCE
    return {'candidate_additional_peak_bytes': candidate, 'smaller_peak_proven': False,
            'declared_current_peak_bytes': declared,
            'declared_components_without_margin_bytes': declared-footprint['margin'],
            'observed_runs_are_not_upper_bounds': True,
            'unproven_aggregate_bounds': ['daemon_overlay_and_metadata_estimate',
                                        'control_and_ledger_combined_outputs',
                                        'all_future_lifecycle_receipts_and_allocation_rounding'],
            'decision': 'retain existing peak; stop before staging if admission rejects it'}


def probe(root=ROOT):
    start = time.monotonic()
    rows = inventory(root, start + 12)
    space = os.statvfs(root)
    free = space.f_bavail * space.f_frsize
    images = []
    for image_id in IMAGES:
        result = subprocess.run(['docker', 'image', 'inspect', image_id],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError(f'pinned cached image missing: {image_id}')
        info = json.loads(result.stdout)[0]
        assert info['Id'] == image_id and info['Architecture'] == 'amd64' and info['Os'] == 'linux'
        images.append({
            'id': info['Id'], 'bytes': info['Size'], 'architecture': info['Architecture'], 'os': info['Os'],
            'digests': info.get('RepoDigests', []),
            'declared_volumes': sorted((info['Config'].get('Volumes') or {}).keys()),
            'source_revision_label': (info['Config'].get('Labels') or {}).get('org.opencontainers.image.revision'),
        })
    retained = sum(row['allocated_bytes'] for row in rows)
    logical_files = sum(row['regular_file_bytes'] for row in rows)
    decision = admission(retained, free)
    return {
        'schema': 2, 'kind': 'read-only exact task namespace admission; no runtime started',
        'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'root': str(root), 'namespace': 'manager-seal-*', 'rows': rows,
        'task_stage_count': len(rows), 'unique_entries': sum(row['entries'] for row in rows),
        'retained_allocated_bytes': retained, 'retained_regular_file_bytes': logical_files,
        **decision,
        'reconciliation': 'Earlier cumulative-owner-quota interpretation was our controller error; historical receipts remain unchanged.',
        'free_bytes': free, 'reserve_bytes': RESERVE, 'free_above_reserve_bytes': free-RESERVE,
        'deduplication': '(st_dev, st_ino); st_blocks*512; includes directory allocation; no symlink traversal',
        'cumulative_writes_complete': False,
        'scope_note': 'retained allocation is a lower bound, not a cumulative-write reset; no historical deleted writes or image layers included',
        'cached_images': images, 'cached_image_sizes_counted_as_new_writes': False,
        'new_remote_host_writes': 0, 'remote_resources_created': [], 'cleanup_required': [],
        'images_acquired': 0, 'containers_inspected': 0,
        'blockers': decision['blockers'], 'admitted': decision['admitted'],
        'installed_path_pass': False, 'RUNTIPI_ADMITTED': False,
        'elapsed_seconds': round(time.monotonic()-start, 3),
    }


if __name__ == '__main__':
    result = probe()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['admitted'] else 2)
