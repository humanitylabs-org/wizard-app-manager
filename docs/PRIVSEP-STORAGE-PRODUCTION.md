# Production storage entry and Service integration (isolated candidate)

REUSE: the accepted `Transaction`, namespace fence, opaque copy, graph/metadata,
rollback and credential `db_intent` guards. EXTEND: the existing PID1 `main` only
(strict finite request values and success-only retirement), protected Service
Compose generator and lifecycle preflight. NEW: a two-file, root-reviewed cold
boot bootstrap/package installer. No installer framework, privileged agent API,
root interpretation of agent config, Docker grant or host compatibility links.

## Administrator-controlled maintenance, not an online migration

This supported entry targets the same audited Linux initial PID namespace and
LP64 x86_64/aarch64 contract as the primitive; arm64 remains unexecuted. Stage
reviewed `install-storage-maintenance.py`, `storage-maintenance-init.py`, and
`protected-agent-storage.py` together under a new root-owned, nonwritable-by-other
ancestry, e.g. `/run/wizard-storage-package`, with regular root-owned files. Run:

```
/usr/bin/python3 -I /run/wizard-storage-package/install-storage-maintenance.py
```

The installer refuses agent/user-owned source ancestry. It installs only
`/usr/local/lib/wizard-storage/maintenance-init` (0755) and the neighbouring
`protected-agent-storage.py` (0644); root owns every ancestor. It never downloads,
reads an agent home, creates a request, changes a bootloader or starts/stops any
service. Review/install the two matching files as one administrator operation;
never replace them while maintenance runs.

With explicit authorization for the selected existing private home, prepare
`/etc/wizard-agent-storage` root:root 0700 and its regular, singly linked,
root:root 0600 `request.json` containing exactly:

```
{"action":"relocate","agent":"hermes","logical":"/home/hermes/.hermes"}
```

Only `relocate`/`rollback` are allowed. There is no nested-namespace override,
request command, environment, import, bootarg authorization switch or auto-root
grant. A permissive selected leaf (0755/0750 or leaf ACL) is unsupported and
refused, NOT chowned/chmodded to force compatibility. First inventory and copy
remain under the accepted maintenance primitive. A stock gateway that already
runs for that logical home requires authorized cold maintenance.

Select the fixed init **for one boot only**, with root mounted `rw`:

```
init=/usr/local/lib/wizard-storage/maintenance-init
```

Use the machine's existing trusted one-time boot selection, not an installed
persistent GRUB/kernel change. This patch intentionally supplies no universal
bootloader mutator. Synthetic queued guests use QEMU's one-process `-append`;
no real machine's boot configuration is modified.

The production bootstrap only mounts fixed `proc` at an empty root-controlled
`/proc` if absent (nosuid,nodev,noexec). It never covers an existing nonempty
mountpoint, mounts sysfs, remounts root, bind-mounts a host namespace, or starts a
service. It proves the unchanged runtime initial namespace/UID/capabilities/
sole-user-task fence BEFORE checking root writability and calling the actual
packaged PID1 request reader. A read-only root is refused after the fence.
The minimal proc mount is the trusted kernel-boot prerequisite, not mutation
permission; it cannot authorize a nested PID1. Normal invocation exits 2 without
mounting anything. The fixed refusal never echoes requests or exception paths.

Success atomically moves the request to protected `completed.json` and fsyncs
its parent, after transaction completion and a final fence. It never overwrites
an earlier completion. An interrupted success-before-retirement safely resumes
the same transaction. Failure leaves the original request and journal intact.
Both success and refusal remain in maintenance forever; NO systemd exec or
service/writer start is hidden in any outcome.

## Bounded owner-controlled return to normal boot

1. Owner checks the serial outcome, completion and mapping in the maintenance
   evidence; on refusal, do NOT select normal boot or enable writers. Preserve
   request/journal, correct only administrator-owned inputs, then explicitly
   reselect maintenance to resume. No automatic fallback/reboot on refusal.
2. Only after owner-authorized success, reboot/reset the machine through the
   existing trusted console/hypervisor. The one-time init selection expires;
   boot the unchanged normal OS without an `init=` override. The request has
   been retired, so accidental reselection cannot endlessly reapply it.
3. Check the public mapping, unchanged gateway User/logical HERMES_HOME/unit
   environment, stock gateway restart + `/health`, and synthetic persistence
   before applying Service wiring. Deliberately disabled writers are reenabled
   only by this administrator step, not the maintenance program. Archive the
   root-only completion record before deliberately authorizing a new request.

## Single final configuration and binding contract

`wam.privsep.storage_source` obtains the passwd identity for the root-selected
agent UID, checks its GID, then calls the same `require_mapping`. It accepts
only the finite public mapping schema (no private payload/config read). Alias
drift or absent/stale/unavailable mapping is a maintenance-needed refusal;
never follow/adopt a new agent-controlled symlink target.

Generated Service Compose binds ONLY `/var/lib/wizard-agent-homes/<uid>` to both
`/hermes` (old container references) and the original logical HERMES_HOME (old
absolute artifact paths). It keeps the agent UID/GID, cap-drop and no-new-privs,
loopback upstream and protected launcher. Logical HERMES_HOME is unchanged for
the stock host gateway; no logical host symlink is handed to Docker. Only the
already-approved main-home alias is checked by the primitive. Private agent
homes gain no host compatibility link and are never opened by the manager.

Preflight occurs before Service install dependencies, update/network mutations,
restart API calls and override pin reconciliation/save. Stop remains available.
The unchanged compiled backend's mandatory final bind verifier is independent
and must accept the physical endpoint and reject the old logical source.

For a genuinely new empty account/home, canonical protected preparation can be
performed BEFORE its first stock gateway start; this change does not add a
second privileged setup configuration or pretend a running existing home is
empty. Fully automated empty-home provisioning/fresh install is a later explicit
acceptance gate; the queued vertical slice is an existing synthetic supported
private home, relocated through this one primitive.

## Evidence boundary

No runtime acceptance is claimed by this source document. The parent-owned
finite job must boot the real packaged entry directly with a real protected
request, exercise negative request/fence cases, return normally, restart stock
Hermes, then use the real installed compiled manager and source-bound Service
image. Unit mocks establish ordering/schema only, not F1. All requested tests
are staged, never run on the editing VPS. Full fresh/alongside install, legacy
credential migration, all-app Deny/revoke HTTP, authenticated tailnet identity,
owner UI/reboot and native ARM64 remain separate gates. Files/Transcriber nested
`data/db`/`data/huggingface` mounts still need their direct-child data contract
repair in that later scope; this slice does not silently approve them.
