# As-found versus fresh-load manager image probe (r2)

REUSE: the retained-disk thin guest, exact source seal, worker admission, capacity
floors, source-only collector, owned-container negative control and stop/reap proof.
EXTEND: only the QA probe/collector/fixtures. Product bytes remain those of
`610716410029f2f70fe4233102584c30e9b9022f`. This is diagnostic, never full acceptance.

The first probe failed before measuring: the expected image ID was already in the
retained guest disk. A successful load cannot prove replacement of that copy.

1. `as_found`: require baseline ID
   `sha256:94cc09235bd65370eff851207b7ee52335098f078ed4f0a564d38d5aea7a5928`;
   tag it locally if needed, never pull. Measure `/app/main.mjs` and `/app/main.js`
   through networkless shell-only normal/read-only runs, stopped-container copy,
   container export, and the final layer of a save round-trip. Save the metadata
   checkpoint **before any image removal/load**.
2. `remove_and_fresh_load`: require no foreign containers; remove only exact owned
   containers, the probe tag, and that bound baseline image (`--no-prune`, no global
   cleanup). Require successful image inventory proving exact absence, then load
   the unchanged SHA-bound archive, assert/print only the loaded ID, record equality
   to baseline, and repeat all five measurements. If removing the sole tag already
   removes the image, do not issue a spurious failing second removal.

Both inspect and round-trip diff-ID lists are preserved and compared with the
original archive. Its expected final diff ID is
`sha256:4a7d6b8643f5740cfd307bab4e97cec976dcd0a6e8d6facf80fff867a630d482`.
Expected `main.mjs`: 6908459 bytes,
`b824d438aa844d0f1920c0e29a04f8f82986a81bbf812b53b5b8399b9a62ebbe`.
`main.js` should be absent from merged views. Save output can be correct despite
bad merged views; this distinguishes content-store bytes from snapshot damage.
Mixed views, metadata drift or contradictory saved bytes are `INCONSISTENT`.

Complete evidence earns one of `BASELINE_IMAGE_DAMAGED_FRESH_LOAD_OK`,
`FRESH_LOAD_ALSO_BAD`, `BASELINE_OK_FRESH_OK`, `BASELINE_OK_FRESH_BAD`,
`INCONSISTENT`. Failed commands/incomplete archives are failures, not verdicts.
No optional third phase is run. Image eviction does not claim a global cache reset.
The final guest image-ID inventory is restored; **its image content is deliberately
replaced**, not claimed unchanged. The retained/original host disks and earlier
stages must remain stat/hash bound and unchanged. The prior failed probe is now
also included in retained inventory protection.

Docker client/server/containerd versions, storage/snapshotter/backing-FS facts,
kernel, Docker filesystem and free bytes are metadata-only. No bundle source,
Config.Env, daemon identity, mount paths or raw command output is exported.
The partial checkpoint is collected even if fresh load fails.

Local `python3 -I -B tests/test_manager_probe.py` exercises synthetic archives and
command seams only. Stage `<pushed-commit>-manager-probe` remains the path rule;
a new commit, sealed manifest and key distinguish r2. Helpers may seal and run
read-only `--preflight`; only the owning parent may submit the native heavy job.
