# Manager import/materialization probe (QA only)

Reuse: EXTEND `manager_start_run.py`'s sealed-source/admission/transport,
capacity monitor, exact-cleanup negative control and retained-inventory proofs.
No product files change from `610716410029f2f70fe4233102584c30e9b9022f`.

One fresh thin guest from the stopped 851 disk pair. Before any app or Compose:

1. Hash-bind the original backend archive, inspect its manifest/config diff IDs
   and the final layer's `app/main.mjs` and `app/main.js` (never extract a tar).
2. Record only selected Docker version/driver/snapshotter/backing-FS/kernel/free
   facts. Unknown status values become `other`; no daemon ID, hostname, env,
   paths, source text, raw tool output or bundle bytes are exported.
3. Load that archive, tag the exact loaded locator, record selected image
   inspect fields and all diff IDs. No pull/build/install/Node/healthcheck.
4. Read/hash both fixed file paths with a networkless shell-only `docker run`,
   a separate read-only run, `docker create` + `docker cp`, and `docker export`.
   Reduce the shell container's root mountinfo to filesystem/option booleans.
5. `docker save` the loaded image and stream its last layer through the same
   parser as the original archive. Compare original/loaded/saved diff ID lists
   and last-layer file size/hash. All lists/hashes remain in the export.
6. Remove only the exact newly loaded full image ID (after its containers are
   absent); require the original image inventory. Reload the SAME SHA-bound
   archive once and repeat. Never run a global/image/system prune.
7. Validate the complete two-cycle export in guest and again on host. The four
   classes are IMPORT_LOSES_CONTENT (round-trip and every view are empty),
   MOUNT_LOSES_CONTENT (round-trip correct, every merged view empty),
   NOT_REPRODUCED (all correct), or INCONSISTENT (disagreement/missing file,
   unexpected main.js, cross-cycle drift). Diff-ID changes remain explicit
   evidence rather than being normalized away. An incomplete export fails.
8. Reuse exact foreign-container negative-control cleanup, exact-ID removals
   and successful inventory readback, baseline image preservation, one poweroff,
   reap/absence, and original disk hashes + retained stat inventories unchanged.

Run only synthetic local fixtures with `python3 -I -B tests/test_manager_probe.py`.
They do not run Docker or native work. Native execution is parent-admitted on
forum/heavy only. The run is diagnostic (`review_required=true`,
`full_acceptance=false`), one-shot and bounded by the existing
285781630976-byte start floor, 277191696384-byte run floor and 8589934592-byte
stage allocation ceiling. `--preflight` is read-only; `--seal` writes only the
new stage's metadata. Never restage over an existing path or key.
