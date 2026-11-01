# Contract A rerun with a guest image-integrity gate (QA only)

Branch `qa/storage-contract-a-install-integrity`, forked from `qa/storage-contract-a-manager-probe-r3`
@ `2ace18ed` (the newest QA branch; it already contains the whole contract-A chain `0ae73880 -> 0513 -> e5d7
-> 6107`, forensics `55bff/d102`, classifier refinement `fa42a49b`, health capture `f8fd700e`, manager chain
`bfff7a39/b452e604/195b1e6d/a6aa893b/ce40456e`, and the r3 eviction/load/measure guest code this gate reuses).
Product bytes stay equal to `610716410029f2f70fe4233102584c30e9b9022f`; only `qa/` and `tests/` differ.

## Why

The retained guest base disk bakes the pinned manager image id
`sha256:94cc0923...5928` (tag `privsep-wam:595523fb`) with an EMPTY stored `/app/main.mjs`.
`docker load` and install.sh's `image inspect || pull` shortcut keep that damaged copy, so every earlier
native run measured the damaged copy, not the product. Probe r3 (`heavy_a8f4ef0d`) proved that removing
exactly that id (`image rm --no-prune -f`, PINNED tag first) and a fresh `docker load -i` of the pinned
archive yields the full 6908459-byte bundle.

## Gate (`install_integrity_guest.py`)

`docker run --rm --pull=never --no-healthcheck --network none --entrypoint sh <id> -c 'wc -c < F; sha256sum F'`

* manager `/app/main.mjs` must be `6908459 / b824d438...`; the helper `/app/libexec/wizard-bind-metadata`
  (also executed from that image by production_guest.py) must be `9904 / c9590812...`
* exactly the empty value (and `allow_evict`) -> probe-r3 `remove_bound_image()` + `load_fresh()`, re-measure,
  FAIL CLOSED if still wrong (one reload, no retry)
* any other value, failed/unparsable measurement, foreign container, wrong archive hash, missing image,
  verify-only mode -> fail closed, nothing evicted
* the Service image (loaded fresh from its hash-bound archive each run, not baked) is measured only:
  `/app/server/index.mjs 17337 / b339d355...`, `/sbin/tini 23576 / 1358f1be...`; never evicted
* export `image-integrity.json` carries labels, paths taken, sizes/hashes, ids, exits only

Gates: `before-installer-start` (host runner, before the root helper), `before-install-sh` (inside the
installer's profile hook immediately before the real install.sh call), `before-service-cases`
(verify-only, after the installer started the manager).

## Not changed

Every other function of `contract_a_guest.py` is AST-identical to 610 (`guest_closure` in the runner
enforces this; `main()` gains only the `integrity` dispatch). The 105 focused pytest cases are inherited
from the 610 receipt, not re-run. This is a diagnostic, not full acceptance.
