# Quiet restart increment (PR #172)

Scope: report needrestart system services, held platform security packages and reboot-required; three separate opt-ins default off. Reuse the existing nightly lease and busy probes (Agent Service, gateway, turn leases, delegations and cron). Check quiet twice and again immediately before each destructive helper call. Unknown/unreadable state fails closed. No guarantee of atomic admission fencing: new work can arrive after a probe; unattended enablement remains an attended acceptance gate.

Reuse:
- activity: REUSE `maintain.busy_reasons` → `hermes_update.busy`.
- privilege boundary: EXTEND root-owned `os_patch.py` helper with validated fixed-purpose actions; never a shell/service-name executor.
- Health: EXTEND `maintain.health` JSON rows consumed by existing Resources Health renderer; no frontend changes.
- package install: EXTEND Apt guarded transaction for held security packages; other distributions report only in this increment.
- NEW: none. Visual: none (backend projection only).

Independent steps retain their own failures. A failed service does not prevent later services or the held/reboot steps. Do not reboot after a failed restart/platform transaction (preserve evidence and avoid hiding failure). Persist reboot intent before scheduling it, then reconcile boot ID and system health on readback. Pending reboot is not success, and a reboot that never happens becomes a failure. No live owner machine changes, no merges.

Acceptance: focused unit fault injection; isolated Thales wos-maint-qa report-only unchanged; real active turn lease defers; real needrestart service replaced while idle; service failure and continuation; held-package guarded install; reboot-required → changed boot ID → healthy Health row; 50+ isolated service restart cycles. Reuse existing QA rig; never act on Thales host services.

## Disposable rig contract

The worker creates a fresh `wos-maint-qa` from the digest-pinned, mount-free Lima template; it refuses an existing disk or mixed evidence directory. Archive evidence and delete the previous disposable VM before admitting another run. Native apt timers **and their services** are masked/stopped during provisioning and rechecked before the observation interval. Otherwise unattended-upgrades can consume held candidates and restart stale test services between the scan and helper boundary (the first run's cycle-27 failure).

Both full Python suites run as an unprivileged fixture owner with a clean environment and a SHA-256-verified Node 22 runtime, matching CI. Running installer fixtures as root enters genuine provisioning paths; omitting Node breaks the TypeScript seal tests. Either suite's failure is a hard pre-reboot failure, not merely a sidecar log. A kernel-only guarded setup transaction seeds a real reboot condition while preserving held candidates. Full product restart/held/reboot gates remain unchanged. The host collects evidence and stops its own disposable VM in `finally`, including collection-failure paths; post-reboot acceptance reads back both suite statuses and all 51 recorded cycles.
