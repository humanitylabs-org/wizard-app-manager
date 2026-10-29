# First installed synthetic manager path: executable parent handoff

## Status and scope

This is an executable **new-source** candidate, not an installed-path PASS. Only the parent may submit its forum job. No child-submitted heavy job, live activation, restart/migration of an owner's app, key retirement, merge, core/provider/model change, private home/history/media read, or new image pull is authorized here. Historical privacy 19/22/60 results remain historical; do not substitute them for this job.

REUSE: the existing pinned `manager-seal-early.py` toolchain, real frozen dependency installation, bundle validator, sanitizer, mandatory-seal enrollment CLI, and actual Nest/AppService/bootstrap/lifecycle implementations. EXTEND: capacity admission. NEW: a task-only real-Docker forwarding boundary, its adversarial protocol tests, a bounded installed-path controller and synthetic Git smart-HTTP helper. No further production changes: the startup/install fixes already at `1ff1ec0b75d72376617504d3b5c1fb683d9f7ef2` are untouched.

## Accounting reconciliation (not a new owner ask)

1GiB was our implementation's estimate of **each next run's additional host allocation**, not James's all-history/lifetime quota. Historical stages, receipts and usage stay unchanged. The source records this controller correction; regression tests use retained `1433038848` and free `225618149376` without rewriting their provenance.

The current next-job envelope is:

- Additional host allocation at most **1073741824** bytes, including the new source stage, all new logs/artifacts/tool copies, daemon metadata/overlay estimate and margin.
- Total **retained allocation + not-yet-allocated next peak <= 3221225472** bytes.
- Fresh free >= **214748364800 + not-yet-allocated footprint**. Retained stages already reduce free space: never subtract them twice.
- Already allocated bytes in THIS source stage reduce the pending portion of the 1GiB next-job cap; they are not erased from retained usage.
- Source stage <=64MiB; public tool copy <=100MiB; sanitized logs <=64MiB; bundle archive <=128MiB; controls/ledgers <=32MiB; daemon metadata/overlay estimate 256MiB; explicit remaining margin 380MiB. These sum to 1GiB. Allocation is rechecked while waiting, before creation, and after owned cleanup; the 256MiB daemon estimate is never reclaimed early based on a noisy free-space delta.
- No old-data deletion, stale-bound reset or receipt relabeling. An admission pass alone is not execution readiness.

The conservative sum of simultaneous memory maxima (including controller, launcher headroom, stopped-build retained tmpfs allowance, sentinels and keeper) is **12046041088** bytes, below 12GiB. Declared tmpfs/shmem maxima sum to **7851737088** bytes, below 8GiB. Fixture volume is exactly 64MiB. Every running container has swap allowance zero (`memory-swap == memory`); the controller also asserts its cgroup's `memory.swap.max == 0`. Work tmpfs is 5GiB. A 32MiB, networkless keeper holds both named tmpfs volumes mounted until owned cleanup: otherwise their last active unmount could discard the just-built bundle/fixture.

## Real authority boundary

`manager_seal_forward_guard.py` is a forwarding boundary, **not fake Docker**. Only the trusted task controller and separately confined guard receive the raw daemon socket. The actual manager sees only the guard's Unix listener at `/var/run/docker.sock`; both Dockerode and the copied, SHA-pinned Compose binary are confined to it. Build/init/helper containers receive no daemon socket.

- Unrecognized method/path/query/body fails before opening an upstream connection. Duplicate JSON keys, duplicate query fields, traversal and ambiguous identifiers are denied.
- Exact real returned IDs establish authority; caller labels never establish it. Only task-configured fresh names may be created. Container/network creation intent is persisted BEFORE forwarding; real IDs/responses are recorded after success. An ambiguous CREATE is reconciled once using that pre-admitted exact name AND this run's label, never replayed.
- Only the exact pinned cached fixture image, fixed services/names, bounded memory/swap/PID/CPU, nonroot user, read-only root, no capabilities, no-new-privileges, no published ports, no host/privileged/device mount, no restart escalation, no Docker logging, and exact synthetic bind leaves are admitted. Both short Binds and long bind Mounts have identical source/target/read-only authority. Named/image/tmpfs mounts, propagation escalation, symlink leaves and root/host paths are denied.
- Only configured fixture networks may be created; dynamic connect requires BOTH exact network and container ownership. No public/default/unknown attachments.
- Unknown inspect/logs/exec requests are denied before upstream IO. Exec, build, pull, global prune and daemon mutations are not delegated. Lists and finite event windows expose only exact owned IDs; events are filtered in memory before persistence. `/info` is reduced to platform and swarm local-node-state; public pinned-image inspection is the only image inspection admitted.
- A metadata-only cleanup ledger belongs to the parent/controller. Creators are fenced before the guard ledger is consumed. Cleanup removes/reads back only captured IDs or reconciled task-new names; it never runs global prune or label-authorized broad deletion.

The controller has read-only `/srv/work` for narrowly scoped retained-stage metadata and the pinned public Node executable, its own stage writable, and no private home mounts. Its code enumerates only the installed capacity probe's synthetic-root namespaces. All manager state, Postgres/Rabbit data, generated TLS, SQLite cache, synthetic auth and fixture key/home are disposable tmpfs; none is an owner's real content. The generated synthetic auth values are withheld/redacted from exported logs.

## Execution order and acceptance

1. Verify source manifest/content/modes and fresh capacity. Admit one controller cgroup, cached build image only. Assert exact cached image IDs, declared image volumes, host Docker SHA, Compose SHA and Node/toolchain pins. Missing/mismatched pins block, never pull.
2. Build common + NEW backend bundle using the genuine frozen dependencies/native rebuild. Validate new native provenance and seven embedded production source-map entries against the source pins. Bundle the real enrollment CLI and source/schema/recipe reviewer. Package actual migrations, translations, TLS inputs and frontend bootstrap directories; no bootstrap suppression.
3. Create two never-started, mount-free task-owned sentinels. Authorize only the positive ID. Before ANY real manager starts, exercise positive inspect/list and real positive create-event readiness; deny unrelated inspect/logs/exec start/create/id paths and global/host mutations. Assert every negative yields 403, guard ledger has no corresponding forwarding record, unrelated state is unchanged, and lists/events expose only owned IDs. Only then activate the fixed new-name policy.
4. Start actual PostgreSQL/RabbitMQ and the NEW Nest bundle. Run complete `AppService.onApplicationBootstrap` and background tasks. The default store is a real local Git smart-HTTP repository with only public synthetic files, not a stubbed StoreService. Require actual TLS, Git clone, SQLite cache and all three real queue consumers. No external/private provider connections.
5. Require unauthenticated lifecycle HTTP rejection, then actual registration/session cookie. Verify the synthetic URN/database row and fixture runtime are absent BEFORE the first install POST. Submit HTTP install exactly once with explicit `skipRun=true`, `skipPull=true`, `forcePull=false`; poll DB state, do not retry a POST after timeout.
6. Run the real trusted enrollment CLI against the installed recipe/compiled-input digests, with no live-continuity blessing. Require mode/owner/schema and fail-closed recipe/compiled checks.
7. Submit actual HTTP start once, verify five owned real containers, exact sealed topology, synthetic home/key bytes and resources. Submit HTTP restart once; require five REPLACEMENT IDs with no overlap, then repeat checks. Require the complete finite create/connect event window with no transient unknown/public attachment; a preexisting `running` DB row alone is not a restart proof.
8. Collect bounded sanitized service/build logs, source/native/bundle hashes and state enums. Stop manager and guard FIRST; reconcile any ambiguous exact-new-name intents and remove/read back only owned containers, networks and volumes. The independent launcher repeats owned cleanup if the controller was killed/OOM. Old stages stay untouched. Success requires BOTH the installed-path receipt and zero-residual parent cleanup receipt, with the reserve intact.

The finite event query asks the real engine for container/network events because Compose/network labels do not reliably propagate into those events. Unknown actors are discarded before forwarding/persistence, never inspected/logged/executed. Every expected container create and intended network connect across start AND restart must be present.

## Staged source pins and exact PARENT-SUBMIT

The parent receives an already-uploaded `/srv/work/manager-seal-installed-<first12-of-new-commit>` directory, `SOURCE.json`, its SHA256, archive SHA256, and `PARENT-SUBMIT.json`. The manifest identifies the full pushed named-branch commit and hashes/modes every staged regular tracked source file. No untracked bundle or private files are copied. The path is one-use: existing controller intent prevents rerun after timeout. Never substitute an old 1GiB historical receipt for this invocation.

Submit the exact JSON argument object in the staged `PARENT-SUBMIT.json` through `wizard_heavy_submit`, ONCE, after independent parent review/admission. It specifies `host=forum`, `lane=heavy`, stable key, exact cwd, `python3 scripts/manager_seal_installed_path.py --source <full-commit> --manifest-sha256 <manifest-SHA>`, and a 3000-second service deadline. No child submission, sustained SSH runner or background shell is involved. Retry the SAME stable submission key only if service receipt delivery is ambiguous; never start a second job.

Primary outputs under that exact stage:

- `installed-results/receipt.json` (phase, sanitized failure stack locations, real auth/bootstrap/queue/guard/lifecycle evidence, resource checks).
- `installed-results/build-result.json`, `build-artifacts.tar.gz`, sanitized per-service/build logs and source/native/new-bundle hashes.
- `installed-results/runtime-events.json`, guard/controller append-only ledgers and their hashes (metadata only).
- `parent-cleanup.json` (zero residual, reserve readback), plus one-use controller intent/ID and bounded controller output.

On failure, inspect only these task-owned sanitized diagnostics. Do not rerun install/start/restart or resubmit on timeout. A missing/failed receipt, guard refusal after the expected sentinel negatives, OOM, incomplete event window or cleanup failure is a genuine failure, not a pass. Successive-update design is still not an approved implementation; this first installed slice does not establish it.

## Local verification, explicitly not installed evidence

Light local Python tests exercise guard adversarial request authorization/response filtering against a counting upstream, resource/admission arithmetic, no-double-POST timeout behavior, exact-ID event reconciliation, bounded archive-copy validation, source manifest modes/content and inherited build command confinement. A separate real Git smart-HTTP clone test exercises the synthetic store helper. Source-method Node tests exercise the two existing production fixes. They do NOT claim fresh Nest/PostgreSQL/RabbitMQ/Docker installation success: that is reserved for the parent-admitted job above.
