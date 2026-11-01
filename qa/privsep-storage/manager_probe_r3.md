# Manager image materialization probe r3 — staged only

EXTEND the r2 diagnostic. Product files remain byte-identical to
`610716410029f2f70fe4233102584c30e9b9022f`. This is not full acceptance;
`review_required=true`, `full_acceptance=false`, and the r2 complete-data
verdict decision function is unchanged.

## Measurement-first execution

Measure the as-found bound image through shell, read-only shell, stopped-container
copy, export, and save. Save each completed view in guest `native.json`; save the
complete as-found phase in `probe-progress.json` before exact owned-container/tag
and bound-image eviction. Prove the bound ID absent, verify the same archive hash,
fresh-load it, prove the loaded ID equals the baseline, and repeat the five views.
Both complete phases and `measurements_complete_before_facts=true` are persisted
before engine commands or auxiliary image/mount projection. Raw inspect identity
input and shell mount output stay guest-private; only ID ownership/binding checks
run synchronously. Progress is metadata only and cannot pass final export validation.

## Parser/assertion audit

- Engine version/client/containerd/kernel tokens accept `[A-Za-z0-9._+~:-]{1,96}`,
  including `v1.7.27`, `1.7.27`, `v2.0.5` and Debian kernel suffixes. Missing or
  unsupported values become `{unparsed:true,sha256:<raw-value hash>,length:n}`.
  DriverStatus accepts only well-formed nonduplicate key/value rows; missing,
  malformed or unfamiliar individual facts are explicit hash-only unknowns.
  Fact subprocess failures, missing executables, malformed JSON and timeouts are
  nonfatal; each optional subprocess has a 15-second limit.
- Root mount absence/ambiguity (including the former `len(roots)==1` assertion)
  becomes unknown. Recognized roots export only fixed flags; mount paths never
  leave the guest. Unfamiliar root filesystems are per-field unknowns.
- Shell records are independent per fixed path. Missing/duplicate/invalid records
  become unknown, not absence. A copy failure is absence only for the exact fixed
  missing-file response; other failures or nonregular targets become unknown.
- Image RootFS/layer/graph metadata is shape-checked and projected to known fields
  or unknowns; the bound image ID remains strict. Empty/unfamiliar layer metadata
  cannot earn a causal verdict.
- Archive parsing never extracts. Missing, duplicate, oversized, nonregular,
  unsupported or malformed manifest/config/layer inputs return hash-only unknown
  fields. Flat export targets are independently unknown on duplicate/nonregular
  members; malformed tar streams cannot turn into false absence.
- Docker load text is not an identity proof. Progress output, IDs and tag messages
  are tolerated; the exact bound-ID readback and image inventory remain mandatory.
- Strict assertions retained: archive SHA and backend binding, exact baseline and
  loaded image ID, config/diff-ID agreement when parsed, command success for real
  experiment operations, bounded output/read caps, ownership/foreign-container
  guards, exact removal and successful inventory absence, and final cleanup.
  No global prune is introduced.

Unknown measurements yield `INCONSISTENT`; unknown auxiliary engine/mount facts
alone do not rewrite the r2 experiment verdict. Arbitrary raw strings, extra
fields, environment/config values and mount paths remain export-schema refusals.

## Local evidence scope

`python3 -I -B tests/test_manager_probe.py` runs the full probe suite. It includes
reconstructed realistic Docker 27/28 JSON shapes with both storage drivers, three
containerd version forms, Debian kernels, full overlay/container mountinfo and
`ls -l` lines. These fixtures are realistic-shaped, not captured native evidence.

Four tests call the actual guest `probe()` through real subprocesses and an
executable fake `docker` on PATH, with canned image/archive/file bytes. They cover
both complete five-view phases, exact eviction/load/ID/inventory proofs,
checkpoint readback before engine commands, valid facts, failed facts, malformed
shell/mount/image/export/save parsers, and an unfamiliar hash-bound original
archive. Only fixture paths and archive SHA bindings are substituted; command,
parser, archive, copy, checkpoint and final-schema code executes unchanged.
Negative controls reject wrong bound image IDs and archive hashes. Synthetic
empty bundles correctly report `INCONSISTENT`, not production correctness.

The helper may seal and run read-only `--preflight` only. The owning parent alone
may admit the returned `job.json`; no native VM/Docker/build execution or submission
is part of this handoff. Preserve r1/r2 stages and failed evidence unchanged.
