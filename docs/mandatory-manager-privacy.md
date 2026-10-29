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

The first Docker attempt at `08068fc6` failed before any test/bootstrap step:
the bind-probe container inherited image UID 0 while the bounded volume was
owned by UID/GID 1000 with mode 0700. With all capabilities dropped, root had no
DAC override and could not open `/probe/bind-probe.txt`. This was a fixture
identity failure, not product enforcement evidence. Its failed receipt/log are
retained unchanged. The repaired bind probes use explicit worker UID:GID and
assert the actual child identity as well as reading an owner-only 0600 marker.
They refuse a root/mismatched identity, symlink root or widened directory mode.
All five Compose sleepers now use the same explicit identity, including the
client's synthetic home reader and the synthetic 0600 key reader; the installed
share-provider recipe uses that identity too. Home stays 0700 with 0600 history.
Never-started, unmounted event sentinels also specify it. The tool-donor container
is never started and reads no fixture bind; the existing worker already uses the
matching UID:GID with a 0700 tmpfs. Service `/tmp` remains the separately capped
4 MiB 1777 scratch; it is not the private fixture root. No mode widening,
supplementary DAC capability, privileged container or weaker limit is introduced.
Local harness regressions check command construction/contracts only. Actual
image bind/Compose identity and TypeScript execution remain for the next
parent-submitted bounded forum job, not locally claimed Docker acceptance.

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

### Literal-dollar failure and focused repair

The exact `8771bcab` real-Docker job reached actual containers and failed with
`$$DO_NOT_INTERPOLATE` instead of `$DO_NOT_INTERPOLATE`. This is a product
snapshot-serialization bug, not an incorrect synthetic runtime expectation.
Pinned Docker Compose `v2.40.0/cmd/compose/config.go` applies
`escapeDollarSign(content)` after rendering **both JSON and YAML** in
`runConfig`, unless `--no-interpolate`. `DockerService` consumes that rendered
JSON, not the raw in-memory resolved project. Escaping it again doubles runtime
dollars. `snapshot` must preserve rendered section values; `overlay` still must
escape the trusted raw compiler values. Do not unescape the config output or
weaken the exact runtime assertion.

The focused repair changes only that serialization and its boundary comment.
The raw fixture remains unchanged. The corrected unit models rendered input;
the new actual-Compose config/reinput test checks dollar/braced/repeated-dollar
and quoted/Unicode values with a hostile synthetic interpolation environment,
then checks a second reinput. The existing lifecycle test still asserts exactly
`$DO_NOT_INTERPOLATE` in the actual running container. A dependency-free Node
method regression exercises the exact snapshot body from candidate or prior
source; it is a bounded method check, not full-module/Compose/Docker proof.

Preserve the failed receipt `413d2601a71e9e25e873cfbab2e064212dc88283eaf5f959e36cde097c61f2cf`
and archive `69f7619097c0e6140264701354fc8939de7a27f08ffb51e8e612119ad4efe052`
unchanged. Its actual report is 16 tests, 15 pass, 1 fail, 0 skip; only the
cross-process enrollment checkpoint completed. Valid UID/bind, toolchain,
dependency/native/common-build and type evidence remains source-bound setup
evidence, not privacy proof. The earlier 389 pass / 1 pending full-suite counts
are historical: the changed snapshot/helper boundary must be re-exercised.
The next exact-source forum job runs the changed mandatory-seal file, Node and
small runner regressions, then the original manager/storage baseline negative;
it does not repeat the unchanged full stock/gateway/backend suite. Baseline red
must remain the public-network assertion, not an unrelated setup/dollar failure.
No installed-API/image gate or successive-update limitation is removed.

### Hostile optional-config rollback failure and remediation repair

The saved `72792604532056b2d988adab47ed55785f6170bc` attempt ran 17 tests:
16 passed, one failed, none skipped. Fourteen lifecycle checkpoints completed;
the final complete event reconciliation did not. The literal-dollar fix and
actual Compose roundtrip passed at that source, not at this unexecuted revision.
Preserve failed job `heavy_5e7c927414c6485fa9b3057559823102`, parent-verified
outer log SHA `6eee847cbcb13c0b94b330ea522a3be06a7ad7558e7cd69c3abc9a62ed578f99`
and artifact SHA `f5799e76f8464d2f4e17c7d610843e56a4ae39a2392cf630b0b93668c7eeea98`.

Root cause: the enclosing `up` had already produced a required-overlay-validated
snapshot, but nested `composeApp(down)` returned through the early remediation
branch without passing that snapshot. `runComposeApp` therefore rebuilt ordinary
arguments, rereading the intentionally retained optional `network_mode: host` /
`privileged: true` override. Compose rejected host network mode plus generated
networks before down completed, masking the fixture's subsequent forced error.
The saved assertion shows the truncated `service agent-synthetic declares mutu…`
error; the source path establishes the conflicting inputs. No failure is
reclassified as successful rollback or no-transient-network evidence.

REUSE/EXTEND: carry the existing validated arguments through an async-local,
same-URN, callback-lifetime remediation context. Nested down/stop/kill/rm do not
relock the enclosing fence, reread optional files or alter snapshot bytes. The
context expires in `finally`, including failure, and does not grant activation.
Separate/concurrent calls and other URNs cannot inherit it. Standalone enrolled
remediation (update down, backup/restore stop, explicit stop/reset/uninstall)
uses existing generated Compose plus generated env only; optional env/Compose
are not read. Sticky enrollment presence is checked without decoding policy, so
missing/corrupt policy still permits non-activating cleanup. This fallback still
requires a usable generated project; it is not recovery from arbitrary corrupt
catalogue files. Ordinary apps keep their existing optional-input behavior.
No new manager, persistent launch snapshot or alternate update transaction.

Guarded pull, update preflight and recovery up remain unchanged and fail closed
on invalid/unapproved runtime/policy. `prepareApp` does not export a cached launch
capability; the actual recovery up revalidates current grants and retained prior
runtime. The trusted first rollback anchor and third-distinct-revision refusal
remain intact. Successive accepted updates still require the separately reviewed
installed-state/anchor-retirement transition described above.

Regressions retain the hostile optional file byte-for-byte, require the original
forced exception, assert all project containers are actually gone before
recovery, then inspect prior image/home/key and revoked grant after recovery.
Additional real-Docker groups cover registered restart with hostile input and
standalone stop/recovery. The expected trace inventory now includes these groups;
the full create/connect reconciliation is not weakened. Small dependency-free
method checks execute exact product methods with dependency facades, including
corrupt-policy cleanup, ordinary apps, URN/concurrency/lifetime isolation, guarded
pull and refusals. Their immediate-prior source negative must fail specifically
because optional input reached nested remediation, not an import/setup error.
They are not full-module or Compose execution.

The next parent-owned bounded forum job reruns types, those method checks,
the exact-prior rollback negative, existing app-lifecycle and restart-incident
tests, the complete mandatory-seal file with real Docker and all checkpoints,
then the original manager+storage disabled-config public-network negative. It
keeps the same pinned Compose, frozen dependency/bootstrap closure and 200 GiB
reserve plus 1 GiB bounded host allowance, 8 GiB tmpfs, 12 GiB memory/no swap and
64 MiB synthetic fixture. Installed API/image gates stay separate. No heavy job
is submitted by the child and no full unchanged backend/stock suite is repeated.

### QA-only saved-bundle continuation

The `3116f500f4d7df959a9f4d586dec4f9a9b7564b5` source-handler attempt
(`heavy_5fba1cbd6d8d44029e02f9bb6e5282da`) genuinely passed the complete
19/19 candidate file with zero skips and 22/22 unique checkpoints, including
hostile optional configuration, forced down failure/prior rollback, revoked
grants, missing-policy remediation and complete live create/connect reconciliation.
Adjacent lifecycle tests passed 60/60; type checking, 7/7 dollar checks, 8/8
remediation checks and the exact-prior specific negative also passed. These are
source-handler fixture results, not installed API proof. The backend bundle
command exited zero. The job then failed only at the harness's two-file `dist`
inventory assertion, before node syntax validation and the original baseline.

Preserve its original failed receipt SHA
`76bee4c997f2c176838c0e90a348b9fb2b1250005dca045e0db97d9639c3e6cf`,
artifact SHA `168a63a43777a34a1bf3b5a3fa6b5daef6f52339828f9a761a3a36024754de44`,
wire-log SHA `d3a0ac71040441040291e2bf3dc6f6c5c3b2e5bdd3f38422c39c465ec58e9cbd`
and independently retained outer-log SHA
`0ec5131f927f1f007a329c2895f9e9adf27c01640537b26f766c31d9f0ab2f6c`.
No file or pass flag in that receipt is rewritten.

`scripts/manager-seal-docker.py --continue-saved` is a **separate** bounded,
parent-admitted continuation. Its full previous/new source inventories may
differ only in the explicit QA scripts/tests and this document. Any product,
candidate-fixture, lock, baseline or dependency drift refuses inheritance; a
different product needs fresh affected proof. It verifies the prior manifest,
receipt, archive, wire log, machine reports, phase log hashes, all 22 checkpoints
and cleanup. The original candidate Docker suite, adjacent tests, types, dollar,
remediation and rollback negatives are not run again. Historical 389/390 early
counts remain historical, never a fresh full-suite claim.

The saved output is exactly `main.js` (6,855,279 bytes), `main.js.map`
(20,939,273), `cpufeatures-e8x7s2f1.node` (60,552) and
`sshcrypto-a3khcfhj.node` (83,264). `manager_seal_bundle.py` rejects any missing,
extra, linked, special, oversize or changed saved file. It hashes bounded regular
single-link files, requires exactly the two corresponding local JS references,
checks ELF64/x86-64/shared-object headers and connects each emitted filename to
the saved sourcemap's exact original require, Bun build configuration, lock
version/integrity and frozen-installed package source (`cpu-features@0.0.10`,
`ssh2@1.17.0`). No `*.node` exemption is introduced. A tiny SHA-pinned Bun 1.3.0
build with only the two saved native bytes must independently reproduce their
exact emitted filenames and hashes; it is not a backend rebuild.

The continuation syntax-checks the actual saved JS with pinned Node, reconstructs
only the destroyed RAM-only dependency/bootstrap prerequisites, runs focused QA
oracle regressions and the required common build, then runs **only the unfinished
original baseline negative**, swapping both original DockerService/storage
files from the pinned baseline. The specific disabled-config public-network
failure, exact active/filter counts, restoration and cleanup are required. Only
a successful continuation can write a new composite source-handler pass; the
original failed receipt stays false. Installed lifecycle/update transaction,
process restart, successive accepted update transition, provider and image
acceptance remain unfinished. No child submits or runs sustained tests.

Failures record a trusted phase, fixed reason, exception class and bounded
runner filename/function/line frames even for a message-less assertion. No
exception payload/source/local variables enter those fields. Worker and cleanup
errors are separate so cleanup cannot erase the primary failure receipt. The
200 GiB floor plus 1 GiB bounded host allowance, 8 GiB scratch, 12 GiB memory,
zero swap, 64 MiB fixture, no pulls/builds of container images and synthetic-only
data restrictions remain unchanged.

### Pinned Python 3.11.2 baseline extraction repair

The `00675f85839665c1b853b30b0cf3380fad003f10` continuation
(`heavy_f455858068214db984727a1c1c1159f3`) passed saved bundle/native provenance,
pinned Node syntax, pinned Bun native-filename reproduction, 33 focused harness
tests, frozen prerequisites and common build. Its original baseline test never
started: Python **3.11.2**, not 3.11.8, rejects `extractall(filter='data')`.
Preserve that failed receipt and artifact archive SHA
`d0cdd9eba71cd253da471c07c7c2f1ba117bed618c0bafe0b037ab7d677531d7`;
do not rewrite it as a pass or invalidate the saved candidate/backend evidence.

Both continuation and full runner now reuse `extract_baseline`. It prevalidates
a nonempty inventory of at most 9,999 members and 64 MiB total/per-file bytes;
rejects absolute, traversal, ambiguous, duplicate, file-ancestor, linked,
sparse, special and malformed-size members; and extracts only exact regular
payload bytes. The destination must be an empty owner-owned 0700 directory.
Descriptor-relative traversal uses `O_DIRECTORY|O_NOFOLLOW` from `/` through
every ancestor, and exclusive no-follow file creation. Ownership, timestamps,
setuid/setgid/sticky and group/other bits are never restored: directories and
executables become 0700, other files 0600. Neither extraction convenience API
nor the newer tar filter interface is used. This is not an unfiltered fallback.

New focused synthetic regressions cover those refusals, mid-extraction symlink
insertion, truncation, bytes/modes and both runner call sites. The parent-only
continuation explicitly requires the actual SHA-pinned Python 3.11.2 interpreter
before running the focused harness, including these regressions. Newer local
Python results are bounded unit checks only, not pinned-runtime acceptance.
Trusted failure diagnostics now recognize builtin `TypeError` as `invalid_type`
without exception messages, custom class names or arbitrary payload export.

Remaining baseline-path audit: `Path.rglob/relative_to/is_file/read_text`,
hashlib reads, `shutil.copy2`, environment updates, bounded `subprocess.run`,
JSON report/count validation, source restoration, `statvfs`, tar archive creation
and base64 result export use no post-3.11.2 API arguments. The new helper uses
Linux directory-FD APIs already available in that runtime; `extractfile`,
`TarInfo.sparse/pax_headers` and `fdopen` need no extraction-filter backport.
Python 3.11 grammar parsing and focused units are local checks; actual execution
of the remaining baseline and pinned-interpreter regressions still belongs to
the parent-admitted worker. No candidate/adjacent/full suite replay is added.

## Remaining acceptance gates — unchanged

Direct `DockerService` with synthetic DB/file facades is not authenticated Nest API/queue/Postgres acceptance. A new manager object is not process-level daemon restart. Manual forced callback failure/prior generated-file recovery is not the actual update transaction. The next exact-image disposable API stage must prove install/start/restart/update, process restart, optional-config disabled/removed/changed, required-policy failure before destructive callbacks, real version update plus forced rollback retaining synthetic home/identity/bindings, socket grants/revocation and Incognito. Preserve full acceptance; do not reduce it to metadata-only updates or grant refusal.

Real provider startup/recovery, attested inference/search/image, combined exact-current migration, backup credential retirement and the round11f proxy failures remain separate unresolved gates. No unchanged stock/gateway rerun is requested. No real Private home/container/history/recordings/Messenger data or prompts are permitted. Scope stays on tested daemon base `fead192e9fb2d560fd3b875d092819b32e7189b6`; do not merge PR319/282 or unrelated owner work, and do not touch Service `pnpm-lock.yaml`. No live activation/restart/migration, provider/model/core change, key retirement, merge or deployment. **RUNTIPI_ADMITTED remains false.**
