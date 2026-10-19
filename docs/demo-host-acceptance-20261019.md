# Real-host alongside acceptance — 2026-10-19

Scope: authorized disposable Ubuntu 24.04 demo, customized named-user Hermes, password-required sudo, joined Tailscale. No production-host changes. This is private-pilot evidence, **not universal installer certification**.

## Outcome

WAM [#90](https://github.com/humanitylabs-org/wizard-app-manager/pull/90) fixes a reproduced cold-install failure: Files could select the pending host-network Agent Service's port before the service listened. Automatic allocation now reserves the recorded shell and service ports, including custom values. Four new assertions fail against the old code; all five pass with the fix. Python suite on the merged #88 base: 433 passed, 8 skipped; four Node tests passed. The final main commit's GitHub test run passed too.

Release 0.3.11 pins merged WAM `e3974ff6cfce92d5d689b56371c704b1d7bc750f`, official WAM image built from `904d987` with verified unchanged image inputs, official UX `9324a2e85c9fb2d03c3e0e12eaeffd7827a63e8e`, and Agent Service `03674b324f631ec3cc4ec1ae2ff0b6d5900bebbf`. Online release/provenance checking passed; canonical manifest and public WAM mirror were read back byte-identical. No old version file was rewritten.

## Matrix

| Case | Result | Evidence on authorized demo |
|---|---|---|
| Existing named-user customized Hermes; sudo requires password | PASS | `wizard-acceptance-20261019/baseline.json`, final `verify.json` |
| Real removal of Docker daemon/CLI packages; binary absent | PASS | `wizard-acceptance-0311-20261019/no-docker.json`, `remove-docker.log` |
| Unprivileged installer prints exact one-time administrator command and exits 2 | PASS | `owner-admin-gate.json`, `owner-start.log` in each run directory |
| Exact default administrator command executed by root | PASS on 0.3.10 | First run `admin-start.log`; no actual human password entry |
| Candidate 0.3.11 administrator phase | PASS | Candidate root command additionally selects `WIZARD_RELEASE=0.3.11`; it was not yet stable |
| Fresh Wizard runtime after supported unlink, no Docker, exact core images removed | PASS on 0.3.11 | `fresh-wizard.json`, `core-image-cache-removed.json`, `fresh-no-docker-install.log` |
| Port pressure from pre-existing private routes | FAIL old / PASS new | Old fresh log selects Files 8797 and service restart-loops; candidate log selects Files 8798 and bridge remains on 8797 |
| Real 0.3.10 → 0.3.11 official-image upgrade | PASS | First run `upgrade-0310-to-0311.{json,log}`; all release checks pass and original snapshot hashes match |
| Duplicate start while a detached install is running | PASS on 0.3.10 | First run `duplicate-start.log`, `recovery.json` |
| Kill actual detached runner unit, detect `stopped`, rerun | PASS on 0.3.10 | First run `killed-runner-status.log`, `interrupted-install.log`, successful following install |
| Safe ordinary repeat | PASS on 0.3.10 and default stable 0.3.11 | Candidate `stable-default-repeat.{json,log}`: no release override, all checks pass |
| All customized config, personality, memory, fixture skills, notes, original provider values | PASS | Candidate `verify.json`; exact config and all non-env fixture hashes; env differs only in Wizard-owned API/MCP link credentials after intentional reset |
| Supported unlink reverses custom config | PASS | `unlink-preservation.json`: config byte-identical to pre-Wizard original; no original env value removed/changed; pre-existing Hermes-added default names remain |
| Actual HTTPS resumable PNG upload + completed-file download | PASS | Candidate `http-acceptance.json`: 99-byte PNG, SHA-256 and bytes identical after round trip |
| Actual Chromium renders that uploaded HTTPS image | PASS | Candidate `browser-image.json`, `browser-image.png`: complete 32×32 image |
| Exact compact thread-list route without history mount | PASS (empty list) | Candidate `threads-response.json`, HTTP 200; no thread-content/inference claim |
| Real host CPU/RAM/disk route | PASS | Candidate `metrics-response.json`: six numeric fields; nonzero plausible disk capacity/use |
| Final API/MCP/bridge health after deferred restart | PASS | Candidate `final-doctor.log`: all checks pass, 18 gateway tools |
| Private-state official Tailscale daemon down + NeedsLogin + nondefault preference preservation | PASS, isolated only | First run `official-private-tailscale.log`, actual official binary 1.104.1 in `--network none` container with private tmpfs state/socket |
| Absent/logged-out/expired **host** Tailscale, real enrollment | NOT TESTED | Host Tailscale is the only access path and was never disabled/logged out; private-state tests cannot prove owner enrollment or expired-key recovery |
| Human sudo password entry | NOT TESTED | Root executes administration; no passwords received, typed or printed |
| Owner's phone/browser, provider sign-in, real chat/model invocation, chat thumbnail/overlay interactions | NOT TESTED here | Direct uploaded-image browser rendering is not the entire owner chat UI |
| Restore a runtime DB that claims apps are installed but all base/app containers are gone | FAIL unattended / restored with supported commands | Rerun alone did not recreate absent base/app containers. Restored original env backup, used Runtipi CLI from an accessible owner cwd and `wam app start-all`, then official installer; exact original fixture hashes recovered. Do not call this automatic partial-runtime recovery. |

All run-directory paths above are under `/root/` on the demo. Logs, backups, fixture hashes, session payloads and screenshots remain private on that host; no credentials or raw private evidence are committed here.

## Boundaries and incidents

- This is a clean **Wizard runtime** and no-Docker-packages rehearsal on a previously used host, not a brand-new machine. Tailscale, Docker layer/non-core caches and customized Hermes remain. The recorded three core image references were actually removed and pulled again. No global Docker prune was used.
- An initial experimental reset archived Wizard metadata without first unlinking. The installer correctly refused to overwrite an unattested existing plugin. Restoring its original receipt and rerunning succeeded. The runner now requires supported unlink before fresh reset.
- `/dev/pts` was already absent during initial host preparation (the earlier unrelated namespace incident had left it missing). Restored the standard devpts mount and exercised `openpty`. No mount-propagation changes or nested systemd/nspawn experiments were made.
- The installed browser's container could resolve, but could not connect to, this same node's Tailscale Serve endpoint. Browser verification instead used the same official Chromium image in a bounded host-network container. Its first default entrypoint unexpectedly started socat on 9222; removed it promptly, then used the browser binary as an explicit entrypoint with CDP only on 127.0.0.1:19222. No secret-bearing pages were opened. Both temporary containers/listeners were removed. Do not use that image's default entrypoint with host networking.
- A live tar snapshot is secondary preservation evidence, not a transactional Postgres backup. Stopped, renamed runtime directories were retained for rollback. Root-private backups have not been deleted.

## Repeatable tools

`scripts/demo-host-acceptance.py` is explicitly bound to root on the authorized `demo` hostname and the disposable `dana` fixture. Each destructive phase requires `--authorize-reset`, a verified backup marker, and exact owned-container inventory. It never changes Tailscale/SSH or prunes globally. Each phase emits JSON and bounded command logs. `WIZARD_ACCEPTANCE_EVIDENCE_DIR` selects a new root-private `wizard-acceptance-*` directory; do not overwrite an existing baseline.

`scripts/demo-http-acceptance.py` creates a clearly synthetic PNG over the actual HTTPS upload API, reads it back, and records metrics/thread-list results. `scripts/demo-browser-acceptance.mjs` checks the returned image through an explicitly loopback-bound CDP browser and closes its tab. Neither proves a provider-backed chat run.

Recommendation: supported, supervised private pilot for existing Hermes owners on the tested Ubuntu layout; not yet a public/unattended recovery guarantee. Missing-container recovery is tracked in [WAM #95](https://github.com/humanitylabs-org/wizard-app-manager/issues/95). Real owner enrollment/device/provider gates remain explicit follow-up work. Keep the homepage install pill and public `/os.md` disabled.
