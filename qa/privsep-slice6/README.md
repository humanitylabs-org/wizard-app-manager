# Slice 6 — existing-box separation and rollback

Status: legacy setup failures are repaired for a staged rerun, including the
Calendar host-mount fixture's app-scoped lifecycle commands. **End-to-end VM
acceptance has not passed**. Do not merge, deploy or run on James's daily driver.

Runner usage (real parser, no wrapper-only flags):

```
python3 qa/privsep-slice6/run.py EVIDENCE_DIR SOURCE_EXPORT CANDIDATE_SHA
python3 qa/privsep-slice6/run.py EVIDENCE_DIR SOURCE_EXPORT CANDIDATE_SHA --preflight
```

`EVIDENCE_DIR` must be absent and under `/srv/work/privsep/slice6/`. The export's
parent must contain `source-export.json` with `head_sha` matching the candidate
and `baseline_sha` matching the pinned main baseline in the runner. `--preflight`
executes argument/manifest validation and compiles the generated real VM function;
it creates neither a VM nor an evidence directory. The staged `job.json` uses the
first form, without `--smoke` or `--preflight`, so it runs the full pytest gate.

## Host-mount setup debug proof

A clean pinned-main debug guest confirmed `wizard-calendar` is the real Compose
service, its existing override is hermes-owned mode 0644, user config is enabled,
and the tagged network YAML survives the additive volume merge. Bare
`wam stop/start wizard-calendar` exited zero but left the Calendar container ID
and mounts unchanged: those commands address Runtipi, not the app.

The corrected helper uses `wam app stop/start`, validates the real response's
`id` and `status`, and leaves `host_canary()` unchanged. On the same disposable
clean-main fixture, scoped stop removed the running container; scoped start
created a different container with `/srv/slice6-host` mounted at `/preexisting`,
`RW: false`, and the exact canary readable. Evidence on Forum:
`/srv/work/privsep/slice6/20261028T041300Z-mount-fix-debug/candidate-legacy-resume-042626Z.log`
and `resume-receipt.json` (`passed: true`, `stopped: true`). This fixture was
resumed after correcting the helper's response-field assertion; reboot startup
also needed main's `wam network reconcile` plus app start before the measured
mount-change sequence. No migration or full rehearsal was run for this debug.

## What the durable job does

- Reuses the Slice 4 Debian 12 cloud boot, KVM, stock unmodified Hermes, registry
  mirror and `finally` VM stop. The worker is Linux/amd64 `forum`, not a live box.
- Installs **main 67141228**, not the candidate, as the legacy layout. Main cannot
  complete on a clean Debian guest without a user bus: rehearsal setup installs
  `dbus-user-session`, and stock-Hermes setup explicitly enables linger and starts
  `user@uid` before main's user phase. This workaround is not a product patch or
  an edit to stock Hermes. The legacy source SHA is read back.
- Queries the guest's actual `wam search --json` index before installing. Selects
  Calendar plus two available lightweight apps (never assumes Quest exists),
  saves those exact IDs for all later phases, and refuses fewer than three.
  Adds a local pre-existing host-mount override to Calendar and applies it through
  `wam app stop wizard-calendar` and `wam app start wizard-calendar` (main's real
  app API lifecycle, not the top-level `wam stop`/`wam start`, which control
  Runtipi itself). Checks the returned
  app/status rather than trusting exit zero, and verifies the
  read-only mount and its canary in the real container before migration and after
  migration/rollback/remigration. Writes UID-1000 data canaries and records the
  legacy token hash and groups. A real Tailscale daemon has the legacy operator,
  with no login and no published tailnet link.
- Keeps migration authority owner-run/root: a protected root-only policy checker
  queries legacy permissions without executing an elevated agent command, and
  the migration calls the fixed root grant helper directly. The strict WAM
  elevation audit is unchanged.
- Invokes the production root migration with a `relocate` checkpoint. Hashes
  **every file under the stopped app-data tree**, including modes and numeric
  owners, against the opaque pre-migration snapshot before apps write again.
- Resumes using the explicit host-preparation entry point. Checks no Docker/admin
  groups, the actual logged/expiring migration grant and revocation, post-revoke
  sudo/Docker/operator denials, thin-client lifecycle, grandfathered approval,
  rotated token, Agent Service health/UID and doctor. Canaries stay identical.
- Runs `wam privilege rollback`, checks original groups, token, user API unit,
  operator and running-app canaries, and rechecks all snapshot hashes. Migrates
  again, verifies no renewed seven-day grant and reruns doctor/lifecycle checks.
- Runs all pytest tests. The only allowed failure is exactly
  `tests.test_chats_views::test_typescript_public_index_path_has_identical_seal`.
  Missing migration/rollback steps or missing teardown can never be green.

Live authenticated app-link resolution is deliberately not faked. The production
step checks exact Serve preservation and each existing loopback proxy endpoint;
actual owner-device tailnet URL resolution needs an owner-controlled rehearsal.

See [JAMES-BOX-DRYRUN.md](JAMES-BOX-DRYRUN.md) for metadata-only live-layout findings
and explicit blockers. It contains no app payload or private-agent inspection.
The companion OS repair-kit wording is committed on the same do-not-merge branch
in `wizard-os` at `cb7da16eb34d3c21b9318d63d64af5b41f0f222a`.
