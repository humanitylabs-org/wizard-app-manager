# Quiet restarts (maintenance increment, PR #172)

The nightly `wam maintain run` now reports three separate Health rows: **Platform packages**, **Service restarts**, and **System reboot**. Defaults are report-only; upgrading/reinstalling never opts an owner in.

Opt-ins are independent, one setting per command:

```sh
wam maintain --quiet-restarts auto   # only active systemd services freshly listed by needrestart
wam maintain --held-platform patch  # apt only; also requires --os-security patch
wam maintain --quiet-reboot auto    # only when the distro reports reboot-required
```

Use `off` for each to return to report-only. `wam maintain setup` refreshes the fixed root-owned helper and narrow sudoers rule. A missing/older helper is reported; it never falls back to an arbitrary privileged shell. Service names must be single valid systemd service names AND in a fresh needrestart list. Additional arguments, stopped services, and unlisted units are refused/left alone. User managers and login/IPC brokers remain reported for a reboot, not restarted beneath the runner.

The existing maintenance and Hermes-update leases are reused. Activity checks reuse Agent Service readiness/work, Hermes gateway activity, turn leases, async delegations and cron executions. Each mutation requires two quiet probes and a final boundary probe. Unreadable state blocks mutation. A Wizard OS installer blocks the entire restart lane. A new probe happens before every service; an exception or failure in one independent step does not discard the others. A failed restart tries to start the service again, records whether it recovered, and still reports an error. This is **service recovery, not package/version rollback**. Reboots are deferred after a failed service/platform step to preserve diagnosis.

Held platform updates are intentionally limited to apt here: pin the pending security candidates, refuse removals and suppress package service actions with the same `policy-rc.d`/needrestart guard as security patching. RPM/OpenRC/rolling distributions remain report-only for held packages; this increment does not pretend they have the same safety contract. Platform package scripts can reload/reexec PID 1 even with service restarts suppressed, hence the separate opt-in and quiet check.

A reboot intent is fsynced before the helper schedules a fixed transient systemd reboot timer. A scheduled reboot is not success. Status/Health reconcile a changed boot ID from the persistent receipt; a reboot not observed within five minutes is an error. Another required reboot on the new boot is not automatically repeated. “Rebooted” means a changed boot ID, not proof of application readiness; existing system/application Health rows remain authoritative for that.

## Acceptance and enablement boundary

`tests/test_quiet_restart.py` covers defaults, actual turn-lease SQLite detection, busy/unknown probes, per-service recheck, independent failures, helper argument restrictions, guarded/pinned held updates, reboot intent/reconciliation/timeouts, and installer interlock.

`qa/maintain/quiet_restart_thales.py` is a durable-worker entry point restricted to `/Volumes/ThalesMedia` and the disposable Lima VM `wos-maint-qa`. Its guest script additionally asserts the exact VM hostname before doing anything. It collects real needrestart discovery, unchanged report-only package/service/boot snapshots, real active-turn deferral, idle restarts, 51 repeated restart cycles, injected failure + service recovery + later-service continuation, real held-package installation, restricted-user helper checks, and reboot-return Health/systemd evidence. It never runs package/service operations on Thales itself.

**Not permission to enable on an owner box.** Busy probes are not an atomic admission fence: a new turn can arrive after the final probe, and a queued reboot has a short shutdown window. No core Hermes admission changes are made. Attended quiet/restart/reboot acceptance and explicit owner opt-in remain mandatory; no automatic/default enablement is included. Full package-version rollback is not supplied by this lane and must not be represented as proved.
