# Mandatory manager privacy — staged, not admitted

## Scope and reuse

REUSE: `DockerService.composeApp(up)` remains the install/start/restart/update activation seam. Keep `WizardStorage.prepare`'s existing current-grant projection and grant lock through preflight, destructive callback and launch. Reuse the existing Service compiler's `tinfoil-sealed-v1` topology, Compose 2.40 `!override`, storage volume rewriting and `x-wizard-share-mounts` metadata. No watchdog, parallel manager, general policy framework or merged overlapping branch.

EXTEND: a manager-owned enrollment fence covers both trusted enrollment/revision and daemon admission. The enrollment read occurs **inside** that fence, never before waiting for storage. One normalized argv is used for both classification and spawn. An enrolled app accepts guarded `up`/`pull`, a small explicit passive-command set, and non-activating remediation. Global prefixes, empty/unknown commands and activation shortcuts fail closed. `stop/down/kill/rm` remain available even with corrupt policy, and may be called from an already-fenced restart callback. Ordinary apps retain optional-config/storage behavior; the existing restart unit mock now observes argv rather than reparsing a command string.

NEW: a narrow trusted seal reader and reviewed executable-revision pair. No HTTP enrollment/revision/removal route. Manager-owned data is `state/mandatory-seals/<sha256(APP_URN)>/policy.json`, **not** `<app-URN>.json`. The directory is the sticky enrollment marker; missing/corrupt/old-schema content fails closed. Normal app folders, app-data, optional user-config and app backup/restore do not own it. This unreleased schema-2 candidate does not silently migrate the previous unreleased schema-1 experiment.

## Trusted policy and runtime updates

Only a trusted administrator, under the manager UID, may invoke:

```
bun packages/backend/src/cli/enroll-mandatory-seal.ts enroll DATA_DIR APP_URN REVIEWED_COMPILED_JSON REVIEWED_GENERATED_JSON
bun packages/backend/src/cli/enroll-mandatory-seal.ts stage DATA_DIR APP_URN REVIEWED_COMPILED_JSON REVIEWED_GENERATED_JSON EXPECTED_CURRENT_SHA256
```

Both inputs are reviewed artifacts: the required compiler output and corresponding generated catalogue recipe, not captured optional YAML or automatic learning. The pair must agree on service set/image/command/entrypoint. The trusted compiled input identifies existing dynamic managed-share destinations with the **existing** `x-wizard-share-mounts` array; a baked share without that trusted annotation that collides with a required static bind is refused, not guessed to be expendable. New declared share destinations may be added if they do not hide required static mounts. No keys/config/paths are printed by the CLI, only policy hashes.

`stage` is compare-and-swap bound to the current policy hash. Identical staging is a byte-preserving no-op, including the previous pair. The first distinct stage atomically retains the initial approved pair as rollback anchor. Once that anchor exists, any further distinct stage **refuses**: the latest staged candidate is not evidence of the installed revision, and even successful candidate selection does not retire the anchor. No installed-state/anchor-retirement transition is implemented in this narrow repair; repeated distinct updates remain unavailable until a separately reviewed explicit trusted transition runs under the same fence. Never remove enrollment to work around this refusal. Only image/command/entrypoint may differ in either reviewed document. Networks, security controls, mounts, home/key paths, provider-key placeholder environment, socket topology and other fields remain identical. Unsupported requested environment/mount/other catalogue changes are rejected, not silently called successful while running old values. The complete generated catalogue is compared, normalized only for existing storage volume rewrites and authoritative managed-share targets. Approved current or previous catalogue selects its matching compiled revision; an unapproved third revision fails before destructive work. Thus the existing update command can preflight/pull/up the new approved image, and its restore-files/up path can select the retained prior image. No new rollback transaction replaces `UpdateAppCommand`.

Enrollment/revision and launch use the same per-URN atomic-directory exclusion at `state/mandatory-seal-locks/<sha256(APP_URN)>`. Contention fails closed; there is no stale pre-lock null decision. A crashed owner leaves a fail-closed fence. Recovery requires a trusted administrator to stop all relevant manager/CLI processes and verify no owner remains before removing **that one stale fence**; never auto-steal or unlink a live fence. No host-side recovery/deployment is authorized here. This is not hostile-same-UID protection or power-loss/fsync durability proof.

## Current grants are not frozen policy

Within the existing storage grant lock, choose the trusted security base, strip its previous/current managed-share targets, then project precisely the current approvals using the existing storage logic. Pass this **prepared** definition to the required overlay; never overlay the older enrolled service after reconciliation. The no-installed-app path removes annotated managed mounts rather than retaining stale authorization. Approved/revoked/reapproved shares therefore remain dynamic while required private networks and static binds stay pinned. Existing socket-token file reconciliation remains unchanged. Final composed sections are compared with a required-only reference; only the validated, dollar-escaped snapshot is passed to launch.

## Verification contract — execution still pending

`scripts/manager-seal-build.py --source HEAD --phase focused` is the parent-admitted **forum-only** diagnostic/test stage: exact source manifest/baseline/toolchain checks; frozen dependencies; common build; backend types **before** backend units; then direct-seam real-Docker regression. No manager image build runs in this phase. The unchanged tested baseline receives only the helper/fixture; baseline red must specifically be the original public-network attachment, never setup/import/whitespace failure. The installed fixture includes real synthetic consumer/provider rows and share declarations, approval/revocation/reapproval, literal-dollar and synthetic home/key continuity, guarded whitespace/prefix commands, a new manager object, an approved transition between two distinct pinned Python image IDs, and forced down/failure/prior-policy recovery.

The existing fixture starts a 4 MiB/210-second-bounded Docker create/connect metadata stream **before launch**, retaining only fixture-owned identities. Network-event label filters alone would miss attachments to unlabeled public networks: Docker filters the network actor's labels, not its container's labels. The fixture therefore identifies owned container creates by the exact synthetic Compose project **or** the fixture label (sentinels), buffers early connect metadata until create ownership is known, and retains all network connects for those IDs regardless of network label. Other container contents/config/env/logs are never read; unrelated metadata is not written into the retained trace. Owned sentinel creates establish readiness and drain boundaries. The final missing-policy negative probe completes before the last drain; stream shutdown is awaited before final exact create/connect reconciliation and trace serialization. Unknown/extra project services fail even if they disappeared before inspection. Every inspected lifecycle container ID must correspond exactly to a captured project create, and every such ID must have exactly its expected network connects. Missing early creates/connects, unexpected creates or public attaches fail. Full synthetic trace is retained outside the repository in the fresh stage `manager-seal-results` directory (the tracked source `qa/` is never reused). This is not a retrospective finite-event-buffer query and is not yet an executed no-transient proof.

`--phase image` is separately admitted **only after** a same-source successful focused receipt; it packages and hashes the image. Neither phase is submitted by the builder. Capacity admission preserves **200 GiB reserve plus conservative incremental footprint**: 16 GiB focused, 40 GiB image; refresh before material allocations and allow another 10 GiB before archive export. No pruning/deletion, reserve reduction or sibling-resource modification is authorized. A capacity refusal is a blocker, not permission to continue.

### Bounded early phase (separate receipt)

`scripts/manager-seal-early.py --source HEAD --manifest-sha256 SHA` is a separately staged, parent-submitted diagnostic. It uses the retained exact public Python 3.12 image ID (never pulls), existing pinned Node, and checksum-pinned public Bun 1.3.0 zip. It copies the candidate source only into an **8 GiB tmpfs** and redirects home, package caches, temp, install scripts, common output and type/unit artifacts there. A read-only rootfs and the two read-only source/Node binds prohibit host package writes; there is no Docker socket, private home/cache, Compose binary, baseline extraction, fixture-image pull or image build. Docker daemon logging is disabled; exported host evidence is capped at 8 MiB plus one small receipt. Only the newly owned synthetic container/tmpfs is removed afterward, not any existing file/resource.

The container has **12 GiB memory including tmpfs, no swap**, 256 pids, four CPUs, no capabilities, no-new-privileges and no published ports. Before downloads the worker checks actual cgroup memory/swap/pid limits, read-only root and tmpfs filesystem size, refusing missing/weakened enforcement. Host admission preserves **200 GiB plus 1 GiB** for bounded evidence and control-plane metadata and requires 16 GiB available RAM. Eight GiB is an enforced scratch ceiling, **not a measured claim that dependencies fit**: ENOSPC/OOM/dependency/network/type/unit failures remain failed diagnostics. Other host writers are not fenced by this script; the parent retains resource/concurrency admission authority.

Frozen install -> common build -> backend types -> normal backend unit suite (`RUN_MANAGER_SEAL_DOCKER=0`, two workers) is the only product execution. Runner regressions run before install. The `early-receipt.json` always has `focused_pass=false`, no image/API acceptance and `RUNTIPI_ADMITTED=false`; it cannot unlock the image phase. Its ephemeral dependency closure is not advertised as reusable disk state. Full focused remains at 16 GiB and image at 40 GiB; source-only early staging is not executed evidence.

### Bounded direct-Docker follow-up (staged, execution pending)

`scripts/manager-seal-docker.py` reuses the successful early bootstrap: exact
Node/Bun, real compiler/link/load, isolated-store bins, the fixed bunx tmp alias
and sanitized dependency transport. It validates the inherited receipt and
unchanged source closure. It reruns the changed mandatory-seal file plus backend
types and small harness regressions, not the passed full backend unit suite.
The old disk-backed focused script is not invoked.

The follow-up runs actual DockerService/storage/seal code and actual Compose and
Docker. AppLifecycleCommandFactory selects the real RestartAppCommand and the
fixture executes that method with real manager down/up and a real
invalid-runtime refusal. Generation/DB/filesystem facades remain synthetic and
the source-handler flag is deliberately named `source_handler_real_docker_pass`.
This is **not** instantiated Nest dependency registration, queue/Postgres, real
generation, authenticated HTTP or installed-manager security acceptance.
`packaged_api_pass`, `full_docker_acceptance`, `image_built` and
`RUNTIPI_ADMITTED` remain false. Before baseline substitution, the exact candidate
backend bundle is built, node-syntax-checked, hashed and exported; frontend,
production external runtime dependencies and official image assembly remain.

A separate **64 MiB tmpfs Docker volume** is mounted inside the worker at its
canonical host-daemon mountpoint; fixture bind paths therefore mean the same
files to the worker and daemon. Never use an inside-only /work path as a host
bind. Linux AF_UNIX 107/108-byte boundary probes, fixture-path bind, Docker socket
group access and real bind/read probes in both cached fixture images precede
package installation. App socket grant enforcement is still an API-stage gap.
Docker socket access grants full daemon authority, not a hostile-client sandbox.

Fixture v1 is exact cached Python 3.12 slim; v2 is the different immutable early
Node 22 bookworm image's actual Python 3.11.2. Both execute the fixture's python3
sleeper and are version-checked. This is **not** Python 3.13 compatibility proof.
No image pulls/builds or disk-backed dependency cache are permitted. Fixture
roots are read-only, logs disabled and memory/pids/tmpfs allocations capped.

The candidate includes the exact disabled-config bypass; real cross-process
enrollment/launch exclusion; prefix/shortcut refusals; sticky revoked share
through update/recovery; no-op bytes, stale CAS and envelope-revision negatives;
and refusal of a third distinct revision with the first rollback anchor intact.
The product still does not support arbitrary successive trusted revisions.
Live events start before the first enrollment/launch race. Readiness and final
negative/drain barriers, exact container create coverage and exact inspected
network identities/internal flags detect transient unexpected/public attaches.
Cleanup uses the exact owner label **and** deterministic synthetic Compose
project. Fallback cleanup cannot turn a resource leak into a pass.

One frozen dependency closure serves both probes. Baseline restores **both**
original DockerService and storage bytes from the verified archive; all other
preexisting baseline files must match candidate hashes except the inherited
incident test. Only the policy helper/fixture are injected. Baseline must fail
specifically at the original optional-config-disabled public-network assertion,
not setup/import/race. The candidate product files are restored afterward.

The **200 GiB reserve is unchanged**. The successful early run measured
1,390,694,400 bytes in the same capped 8 GiB scratch / 12 GiB no-swap cgroup.
This follow-up keeps dependencies and source there, caps baseline extraction at
64 MiB, bundle output at 512 MiB, compressed artifact at 128 MiB, host wire at
384 MiB, separate dependency evidence at 64 MiB and copied Compose at 100 MiB.
Fixture data is separately capped at 64 MiB in RAM. A 1 GiB **host-write** budget
covers exports/control metadata rather than pretending the old 16 GiB disk
working set is smaller. Admission and the running watchdog preserve the reserve;
insufficient capacity/RAM or changed closure fails before package installation.
No deletion is authorized. This source-only bounded phase is not the old full
focused/image admission and does not unlock that image phase.

## Remaining acceptance gates — unchanged

Direct `DockerService` with synthetic DB/file facades is not authenticated Nest API/queue/Postgres acceptance. A new manager object is not process-level daemon restart. Manual forced callback failure/prior generated-file recovery is not the actual update transaction. The next exact-image disposable API stage must prove install/start/restart/update, process restart, optional-config disabled/removed/changed, required-policy failure before destructive callbacks, real version update plus forced rollback retaining synthetic home/identity/bindings, socket grants/revocation and Incognito. Preserve full acceptance; do not reduce it to metadata-only updates or grant refusal.

Real provider startup/recovery, attested inference/search/image, combined exact-current migration, backup credential retirement and the round11f proxy failures remain separate unresolved gates. No unchanged stock/gateway rerun is requested. No real Private home/container/history/recordings/Messenger data or prompts are permitted. Scope stays on tested daemon base `fead192e9fb2d560fd3b875d092819b32e7189b6`; do not merge PR319/282 or unrelated owner work, and do not touch Service `pnpm-lock.yaml`. No live activation/restart/migration, provider/model/core change, key retirement, merge or deployment. **RUNTIPI_ADMITTED remains false.**
