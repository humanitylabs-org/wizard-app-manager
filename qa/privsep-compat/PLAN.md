# Repaired-candidate compatibility — staged only

REUSE: retained seven-boundary credential proof and exact backend image; existing
Slice 4 pinned stock-Hermes setup only. EXTEND: disposable KVM orchestration into
one full fresh install and one legacy alongside migration. NEW: no product code.

## Two cases, sequential guests

1. Full `WIZARD_MODE=fresh` through the real `get.sh`, with the accepted backend
   image, then real catalog Calendar (`wizard-calendar`) and Files (`files`).
   Exercise app-scoped stop/start, a synthetic Calendar import/export, Files HTTP,
   post-import restart and preserved synthetic payload bytes/UID/GID/mode.
   Read the actual manager container's image ID and the fresh agent boundary.
2. Main `67141228f6f73516ab68ece1c4c72bf3f67aebe0` alongside setup with the pinned
   published legacy backend and pinned stock Hermes. Only baseline setup gets the
   known Debian user-bus workaround. Install/exercise those same ordinary apps;
   move using the candidate's protected root CLI. Compare stopped synthetic
   app-data at relocation; roll back **before** credential invalidation; verify
   usable apps/data; remigrate through verified invalidation; refuse rollback;
   forward-resume and check real thin-client lifecycle, doctor, no Docker/sudo or
   migration grant, old credential rejection and new acceptance. Resume again
   idempotently. The separate seven interruption boundaries are inherited, not rerun.

## Immutable private stage contract

The parent receives `manifest.json`, `job.json` and their hashes. The runner checks
all stage files, the exact old/new source archive member closure, inherited
security manifests/proof/receipt, the backend archive and cloud-image hashes.
The complete candidate differs from production `9b2e2aca` only in this harness and
handoff docs; no production repair is smuggled into the compatibility run.

The root migration bootstrap drops `WAM_IMAGE`. In the disposable migration VM
only, tag the retained image with the default installer reference before root
preparation, then assert the live backend ID afterwards. The legacy install uses
a distinct immutable registry digest. Do not substitute a published old image
for the accepted backend. No image build is needed.

Reserve: keep the 200GB shared floor, plus **16GiB** for a full-install guest and
its migration snapshot. This is above the earlier 8GiB isolated-credential rig
minimum. There is only one guest at a time; each is stopped/reaped and its private
SSH key, seed, disk and bootstrap logs are removed before creating the next.
Recheck capacity before admission and each guest. A capacity block is not a
product test failure. Do not clean other owners' work or lower the floor.

`--preflight` validates source/evidence/tools without Docker, VM startup or a test
suite. It reports capacity separately. `--execute` is one-shot and belongs only
to the parent's native forum heavy-worker submission. Do not invoke it via a
helper, background SSH or a local terminal. No admission is claimed by staging.

## Deliberate limits

- No personal machine reinstall, private/sealed data, real credentials, live
  accounts, authenticated tailnet or installed core Hermes update. The legacy
  fixture installs one exact stock commit; migration must preserve it.
- Calendar and Files catalog commits are pinned in the stage and checked before
  install; drift refuses the case. Public transitive bootstrap/package/image
  dependencies are runtime inputs, not a newly reproducible four-repo release.
- Published stock Shell/Service packages are not builds of the current UX/Service
  PR heads. No claim of current approval-UI or Service-security acceptance.
- No real tailscaled owner identity, approval-listener lifetime/ownership,
  developer-window expiry, link resolution or genuinely used spare-box proof.
- The full historical Python/UI suites are not rerun. Production Python is
  byte-identical to the accepted credential candidate, so its 103-case evidence
  stays explicitly inherited.
- Strict bind ancestry is never weakened. If a stock recipe or its manager-created
  anchor is refused, preserve the exact failing phase/return code and safe source
  location; report the product block. Do not repair production or broaden policy.

Even a passing bounded receipt keeps `release_security_green=false`. All four
PRs remain draft / DO NOT MERGE until James explicitly confirms 100%.
