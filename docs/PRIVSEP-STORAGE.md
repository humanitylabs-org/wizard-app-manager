# Protected Hermes storage: isolated first seam

Status: source implementation and staged synthetic proof, **not runtime admitted**.
No real home relocation, boot change, live service stop, image rebuild, release or
merge is authorized by this change. The unchanged bind verifier is not bypassed.

## Reuse / extend / new

- REUSE the original logical Hermes home, nonroot account, stock units and
  existing credential journal's forward-only boundary.
- EXTEND `prepare-alongside.py`, `get.sh`, `install.sh` and `wam privilege` with
  pre-mutation maintenance detection. They do not attempt an online move.
- NEW one finite primitive/entrypoint: `scripts/protected-agent-storage.py`.
  The standalone entry is also the storage-only upgrade path for an already
  separated layout. It does not run the unrelated legacy migration, re-pair,
  change account privileges, or rotate any credential.

## Why a stop receipt is insufficient

Stock lifecycle audit used upstream Hermes
`71d34bad1045610252eda02ee79549436896e784` (read-only, no core changes), plus the
current WAM preparation/migration source. Relevant witnesses:

| Producer / restart path | Source witness | Consequence |
| --- | --- | --- |
| System and user gateway units | `hermes_cli/gateway.py:3291-3353` | System units want the user's manager; `Restart=always`, unlimited start interval, user/system target activation. |
| Login / linger | `hermes_cli/gateway.py:2394-2484` | A missing bus may be recreated through linger/user-manager activation; stopping one gateway does not prevent new writers. |
| In-process cron restart | `cron/scheduler_thread.py:19-64` | A dead ticker is supervised and respawned until shutdown was requested. |
| Restart-safe / detached work | `cron/scheduler.py:3740-3839`, `cron/scheduler_detached_worker.py:1-70`, `cron/delivery_queue.py:1-8` | Worker lifetime is not the transport/gateway lifetime; process/session and DB handles can survive the foreground waiter. |
| Container supervision | `docker-compose.yml:29-61`, `hermes_cli/gateway_multiplex_s6.py:75-101`, `docker/s6-rc.d/main-hermes/run:23-26` | Docker `restart: unless-stopped` can recreate the main gateway; dynamic profile s6 slots can restart independently. The static main-hermes s6 slot itself is a no-op, not an extra gateway. |
| WAM manager/app stop list | `wam/privilege_migration.py:28-38` | This list intentionally preserves agent identity. It is not a selected-home writer fence and is now explicitly documented as such. |

Current official docs were also consulted: messaging overview and
<https://hermes-agent.nousresearch.com/docs/user-guide/features/cron> (the latter
explicitly describes restart-safe dispatch/recovery). These do not provide a
shared mandatory writer lease covering manual shells, login, cron, service and
container starts. Adding a Hermes lease would require core changes and would
still not exclude an agent opening a file directly.

## Production maintenance mechanism

The bounded first implementation requires an **explicit cold recovery boot**:
this trusted program is the only userspace process in the host PID namespace,
not a tool running alongside systemd. It never launches systemd, a login shell,
cron, Docker, a gateway, or payload code. It invokes only a checked OS `cp` for
opaque transfers and reaps it before rechecking admission. It remains PID 1 in
maintenance after success AND failure; reboot into normal operation is an
administrator action, never an automatic error fallback.

Mechanically checked on every mutation/phase and before/after `cp`:

1. real/effective root, PID 1, complete initial-user mapping, effective host
   SYS_ADMIN/SYS_BOOT capabilities;
2. `NS_GET_PARENT` refuses a nested PID namespace (the initial namespace has no
   parent); restricted/rootless namespaces cannot pass the full UID map/caps;
3. kernel command line names one explicit non-systemd recovery init on protected
   root-owned ancestry, with a regular executable endpoint;
4. no other userspace process and exactly one thread; kernel-thread distinction
   comes from the kernel's PF_KTHREAD bit, not a caller-supplied process label;
5. the executing storage module has protected root-owned ancestry.

**Census and prevention are different.** Item 4 observes who exists. Prevention
comes from exclusive host-init control after a cold boot: there is no session,
service manager, user manager, scheduler, container daemon or existing agent
capable of producing another untrusted task. The trusted single-threaded PID 1
only creates/reaps its finite opaque-copy child. If an initramfs leaves a daemon
behind, admission refuses; it is not silently ignored. A normal root shell,
`systemctl stop`, SIGSTOP, root JSON receipt, root lock, or a namespace PID 1 is
not accepted. Root/admin, the kernel, initramfs, OS runtime and reviewed recovery
code remain trusted. This is not a defense against malicious host root or a
compromised kernel/OS executable, nor an online availability promise.

No mount alias, CAP_SYS_ADMIN grant to an agent, persistent `/proc/PID/fd` source,
ordinary Unix-home ownership change, or final-consumer exception is introduced.
The capability checks inspect the already-privileged cold-boot administrator;
they do not add capabilities to a running agent or container.

The operator must explicitly install the reviewed executable on protected
root-owned ancestry, provide a private root-owned
`/etc/wizard-agent-storage/request.json` containing exactly
`{"action":"relocate","agent":"hermes","logical":"/home/hermes/.hermes"}`
(or `rollback`), and select that trusted init with a writable root filesystem.
Do not run these steps on a real install before the owner's eventual release
approval. This slice intentionally does not edit boot configuration or manufacture
that authority from an installer flag. Unsupported boot/runtime prerequisites
fail closed.

## Transaction and retained data

The first source shape is a real agent-owned direct child of an ordinary
`/home/<account>` owned by that account, whose ancestor `/home` remains protected.
The physical leaf is `/var/lib/wizard-agent-homes/<numeric uid>`. Agent UID/GID,
payload ownership and ordinary home ownership are unchanged.

- Protected private per-UID journal/snapshots live below
  `/var/lib/wizard-agent-storage` (root 0700); a root-owned public mapping contains
  only UID/GID, logical/physical endpoint and canonical directory identity.
- Control and source-parent directories are opened no-follow and every ancestor
  inode stays pinned and revalidated. Publication uses descriptor-relative
  `renameat2(RENAME_NOREPLACE)`. Existing unexplained targets, symlinks or identity
  changes are never adopted.
- Snapshot/transfer/publication/source-retention/alias/metadata intents precede
  their effects and are fsynced. Same-filesystem publication renames the actual
  original directory. Cross-filesystem transfer uses a new sibling stage,
  verifies bytes/metadata, fsyncs, then publishes atomically.
- A private protected pre-change snapshot is distinct from the active writable
  canonical leaf. Interrupted partial copies are retained and restarted under a
  fresh name. A version-2 private graph records each relative component, object
  identity/type, mode/UID/GID, link count, size, mtime/ctime, access time and
  regular-file bytes/xattrs BEFORE `cp`. The complete graph is revalidated
  READ-ONLY before any timestamp write, using no-follow descriptor-relative
  traversal. Only attributable atime drift is repaired; a legitimate changed
  mtime is never overwritten. Source and target have independently validated,
  disjoint inode identities; hardlink aliases are restored once per inode.
  The graph is captured AFTER fingerprints have completed their own symlink
  atime repair (which changes ctime). Pending validation does not read symlink
  text or mutate its metadata. Legacy records and cuts during restoration itself
  are ambiguous and refuse, retaining every copy for administrator review. This
  conservative refusal is not an automatic recovery promise for every cut.
- Verification covers opaque regular-file bytes, numeric UID/GID, mode,
  nanosecond atime/mtime, hardlink relationships, symlink text and supported
  ACL/xattrs. Cross-filesystem ctime/birthtime/inode identity cannot be preserved
  and are explicitly not promised. External hardlinks, nested mounts (including
  same-device binds), special files and unsupported/remote filesystems refuse.
  The initial supported local filesystem set is ext4, XFS and Btrfs; only ext4 is
  staged for the real-kernel proof here.
- Cross-filesystem original input is retained as a unique sibling of the logical
  path; the agent can later move that sibling because its ordinary home remains
  agent-owned. The authoritative rollback image is the separate root-protected
  snapshot, not that sibling. Incomplete copies and all snapshots remain until
  an administrator explicitly approves cleanup; no product recursive deletion
  exists.
- Rollback checks the credential journal BEFORE creating directories, locks,
  journals, aliases, or restoring anything. `db_intent` and every later/unknown
  phase make storage recovery forward-only. Earlier rollback restores the
  snapshot to the original logical directory, retaining the current canonical
  tree—including any later writes—under a protected quarantine name. Original
  credential journal semantics are unchanged; no credential mutation is part of
  this seam.
- Rollback first atomically publishes `state: rollback-unavailable`, then
  journals its rollback direction/original phase before changing logical
  selection. Even a cut between those two writes blocks forward resume. Finite
  rollback phases end in a durable mapping withdrawal, including early rollback
  that retains the original directory. Public checks require `state: ready`,
  the canonical identity/private layout and an exact absolute logical alias.
  Absent, relative, stale or non-symlink aliases refuse without adopting their
  targets. Alias readback is a current consistency observation, NOT a durable
  pin. Only the unchanged final canonical bind helper authorizes actual Docker
  consumption. Public checks inspect filesystem/ACL metadata, not payload bytes.

### Deliberate compatibility limit: private leaf required

The selected leaf itself must have **no group/world permission bits and no
POSIX access/default ACL**. An ordinary `.hermes` mode 0700 is supported. A 0755
or 0750 leaf, or a leaf with a named-user ACL, is refused BEFORE any transaction
directory, journal, snapshot, stage or publication is created, even if its old
account home is 0700. No automatic chmod/chown is performed. This prevents
relocation from silently removing the only effective privacy barrier.

Relevant existing copy-parent directories must have no default ACL: this covers
the public anchor, private controls and ordinary home used for restore siblings.
`cp` uses a private umask, a private source root and a default-ACL-free destination
parent, so the copied root never relies on a later permission repair for privacy.
Resume, copy, publication and rollback revalidate the finite known endpoints.
ACLs/xattrs INSIDE an accepted private leaf remain preserved. No new agent-owned
protected parent or parent-ACL architecture is introduced. Unsupported layouts
remain an explicit first-slice refusal, not a permanent assertion about all homes.

Root reads selected bytes only for opaque transport and comparison, and numeric
filesystem metadata for verification. It never parses a payload database,
config, profile, plugin, transcript, secret format or executable. This reviewed
root transport deliberately extends the former "root never opens home" promise;
normal manager runtime still never reads payloads.

## Admission proof staged, not executed

`qa/privsep-storage/run.py` is parent-native-heavy-only on forum. Its read-only
preflight checks the immutable whole-source archive/member inventory, genuine
Git bundle and shallow boundary, exact cloud-base digest, native prerequisites,
reserve **217179869184 bytes plus 21474836480 pending bytes**. That pending budget
is for one small synthetic two-disk guest, not a lowered version of the former
backend-build floor. There is no backend build or old-suite rerun.

The queued guest starts in ordinary systemd with a real distinct user-owned
`/home/hermes/.hermes`, a writer holding a file handle and a real automatically
restarting systemd unit that actually appends inside the selected payload. It
requires pre-move refusal with frozen payload sentinels, directory identity and
pairing unchanged and no transaction-control creation. The writer's own output
must vary, so it does not dishonestly claim whole-tree immutability while that
writer runs. After stopping the writer, the stopped-only refusal also compares
the entire tree. Root-receipt and nested-PID-1 refusals are separate. Removing
only the admission function must start the otherwise-forbidden root transaction
online and be detected, even if a later source-drift check also refuses; this
negative control is not relabelled as a demonstrated data-corruption incident.

The same disposable disk then cold-boots a reviewed PID1 driver with no NIC.
It exercises sole-init admission, surviving writer refusal, real samefs rename,
real distinct-filesystem transfer, exact metadata, rollback/db_intent boundary,
symlink and parent-swap attacks, nested mount/special-file/external-hardlink
refusals, useful nonroot leaf writes and denied canonical-parent rename. For
both samefs/crossfs it emits fsynced barriers before/after publication, alias and
metadata commit. The host actually SIGKILLs QEMU and reboots the same disk to
exercise persisted recovery, not an exception-only unit mock. All actual case
identities are checkpointed, deduplicated and checked against the full expected
set. Missing/failed/skipped cases cannot produce green. First errors, diagnostics,
source binding and all owned-VM cleanup are retained.

The extended inventory adds real fixed-`/usr/bin/cp` interruptions with durable
pending-copy records, an observed open source FD, an incomplete target and an
attested stopped copy child before host power-off. Bounded normal boots between
maintenance boots substitute a synthetic descendant with an external-sentinel
symlink, make a legitimate edit, install an ambiguous record or change leaf
privacy; unchanged resume is a separate positive. Source/external metadata and
all partial identities are retained in private evidence. Real UID 65534 actors
with cleared supplementary groups must be denied access to interrupted snapshots
and crossfs stages, and to committed canonical leaves. Both filesystems include
restrictive-home/permissive-leaf/ACL negatives before any copy or control creation.
New rollback power cuts exercise public unavailability, private direction intent,
pending restore copy, alias removal, restored logical selection, canonical
retention and final withdrawal, not just the original forward cuts. The host boot
budget is derived from that exact finite cut inventory plus a completion boot;
intervention boots have their own bounded phases and exported artifacts.

These are QUEUED assertions, not executed passes. The original failed attempt
stopped at focused Python fixtures before any kernel storage cases. Exact
extended case IDs/counts and Python IDs are sealed in the revision manifest;
JUnit must match the full four-module inventory with zero skips/failures/errors.

## Explicit remaining integration gates

This first slice **does not make installation work end-to-end**. In particular,
the existing Service generator still uses the old logical bind source. A storage
mapping is necessary but not sufficient for install/release admission. Parent
must next wire the physical source to both existing `/hermes` and original
logical in-container destinations, preserve artifact roots and finite override
recognition, and prove the installed stock gateway still usefully restarts with
its original logical HERMES_HOME/account/unit identity. Legacy runtime migration
requires the full committed storage mapping; runtime rollback refuses a mapped
home and requires the maintenance storage rollback first, rather than modifying
the active canonical home under the old manager-only stop list.

Files DB and Transcriber cache have separate known nested-source recipe fixes;
private/profile/extra-agent adoption remains forbidden. Installed backend/HTTP,
normal installer/upgrade and final unchanged verifier consumption must be proved
after those finite follow-ons. The retained backend binary/image may be reused
only with explicit finite build-input dependency equality, never relabelled as a
new whole-source build. Nothing here supersedes prior image/security evidence or
the owner's no-main/no-live/no-deploy/no-release gate.

**F1 is still separate:** the QA cold-init wrapper mounts proc/sysfs, remounts
root writable and calls the imported primitive as PID 1. It does NOT exercise
production `main()` or its private request/error loop. Direct production entry
requires those boot mounts/writable-root prerequisites to be established by a
reviewed trusted boot path; product `main()` currently checks the fence before
provisioning `/proc`. A real positive and negative boot through the exact product
entry/request remain mandatory before installer/integration/release claims. A QA
wrapper pass cannot discharge that gate. Fresh and alongside-config installation
acceptance also remain unexecuted, behind the owner's explicit final confirmation.
