# Manager seal — bounded installed-lifecycle spike

**Stopped on demonstrated authority/interface blockers; installed acceptance remains false.** No heavy job submitted/run and no product transition implemented.

- Parent/base head: `814c78ab24dad9cdf3ca251abd7b42af6c2ba7c9`; production remains byte-identical to `3116f500f4d7df959a9f4d586dec4f9a9b7564b5` for all17 pinned inputs.
- Branch: [fix/mandatory-manager-privacy](https://github.com/humanitylabs-org/wizard-app-manager/tree/fix/mandatory-manager-privacy). No merge/live activation.
- Main evidence and exact offline argv: [installed spike results](docs/manager-seal-installed-spike-results.md).
- Executable blocker probe: `scripts/manager_seal_installed_spike.py`; expected exit2 proved daemon-wide unfiltered bootstrap network pruning and body `skipRun:true` overwritten to queue false. Actual saved bundle/source-map pinned, no fake installed backend. Receipt SHA `8f02b0fd7a5768a0bd23fda84be81604fb58363d39a58417fc483da24121ef2b`.
- New regression `scripts/tests/test_manager_seal_installed_spike.py`:4/4 pass; source/manifest/archive drift refuse. No broad-suite rerun. Diff check passed. Any unavailable lefthook is not test evidence.
- Transaction draft awaiting controller review: [accepted revision transaction](docs/manager-seal-update-transaction-design.md); no unsafe anchor clear or extra owner approval gate. Traces command→queue→config→DB→SSE and crash/concurrency/revocation.
- Metadata: `docs/manager-seal-installed-spike-metadata.json`; cached real PostgreSQL14-alpine/RabbitMQ4 digests, sizes and observed reserve. No capacity admission/peak claim; authority blocked before runtime measurement.
- Changed old document: `docs/manager-seal-installed-acceptance-spec.md` source typo corrected and blockers linked. Added source pins, metadata, blocker receipt/results/design, probe/tests and this handoff. Product source unchanged.
- Original parent814c78ab composite and failed receipts preserved at their original bindings; no fresh composite claim after these new QA paths. `installed_backend_http_transaction_pass=false`, `successive_accepted_updates_pass=false`, `official_image_pass=false`, `RUNTIPI_ADMITTED=false`.

No executable heavy submission spec is offered: real saved bootstrap against the host Docker socket violates the synthetic-only mutation boundary. Next controller step is resolve that precise seam and review the transaction design, not admit a knowingly unsafe/incomplete job. Original full-image/provider/attestation/cutover/migration/key-retirement gates remain separate.
