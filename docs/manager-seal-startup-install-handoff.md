# Installed manager first-path handoff — PARENT-BLOCKED

## Outcome and product diff

**The two selected staged safety changes are implemented. No installed run is claimed. No worker job was submitted, no new bundle/image was built or staged on forum, and nothing was deployed.** The unchanged cumulative write cap fails before another stage is admissible.

REUSE: existing lifecycle controller, validated form schema, handler, real queue command and trusted enrollment CLI. EXTEND: only the two product seams and their regressions. NEW: bounded read-only accounting/static-inventory helpers, not a replacement runtime or transaction system.

- `AppService.bootstrap()` no longer calls daemon-wide `pruneNetworks()`. Remove its now-unused Dockerode injection/imports as well. Migration, repository/marketplace initialization, scheduled jobs, asset copy and TLS remain on the normal path. Explicit maintenance methods are untouched; no prune/skip-bootstrap flag or fake Docker was added.
- New-install handler resolves `skipRun ?? parsedForm.skipRun ?? false` after form validation. A defined trusted boolean override wins. The resolved value is used in **both** the queue message and success status. Normal omission still launches. Controller/service/DTO/queue mapping and command implementation are unchanged.
- Scope is a **new** synthetic installation. The pre-existing `if (app)` branch updates configuration and calls `StartAppHandler`; it is deliberately not redesigned here. A future rig must prove the exact synthetic URN is absent before issuing install, fail on collision, and never retry an ambiguous install request by issuing install again.

## Verified small regressions

`docs/manager-seal-startup-install-method-evidence.json` contains actual Node output, source/test/runner hashes and pinned baseline source hashes.

- Exact candidate source-method tests: **7/7 pass**.
- Same harness against `118608e135ffa1d7f5114a2e9b7a059c50c04cf2`: **5 pass, exactly 2 expected failures**, with specific diagnostics: `bootstrap invoked daemon-wide network prune` and `validated form skipRun lost before queue` (`false` instead of `true`). Startup completion is also asserted, so catching a probe exception cannot produce a false pass.
- Full tiny Python harness regression suite: **80/80 pass**. Three Node source-method suites together: **22/22 pass**. These are not the prior installed/synthetic 22 checkpoints.
- Added actual Vitest/ArkType handler tests and a Nest testing-module bootstrap test. **Those Vitest tests have not been run**: this checkout has no installed dependencies; no dependency install or runtime build was attempted on the VPS. The runnable method tests use explicit dependency/schema facades and are not HTTP/Nest/DB/Rabbit evidence.
- Historical archive-refusal tests now reconstruct and hash-check the historical pinned source from Git, rather than silently relabeling current product source as old-bundle source. Historical manifest/archive evidence remains unchanged. The malformed-header test writes the intended raw header instead of Python 3.14's now-rejected `addfile(size>0, fileobj=None)` call; its refusal assertion is unchanged.

Reproduce the tiny source checks (output must be a fresh path):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/manager_seal_startup_install_check.py --output "$TMPDIR/manager-seal-safety-$(date +%s).json"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_manager_seal_*.py'
node --test scripts/tests/test_manager_seal_remediation.mjs scripts/tests/test_manager_seal_dollars.mjs scripts/tests/test_manager_seal_startup_install.mjs
```

## Exact demonstrated admission blocker

`docs/manager-seal-installed-capacity-evidence.json` is the actual bounded forum read-only probe result, **exit 2**, observed `2026-10-29T15:20:52.520049+00:00`:

| Boundary | Bytes |
|---|---:|
| Unchanged cumulative task host-write allowance | 1,073,741,824 |
| Retained task-stage allocation, unique inode/device, including directories | **1,433,038,848** |
| Regular file logical bytes alone, unique inode/device | 1,341,622,113 |
| Retained allocated bytes above allowance | **359,297,024** |
| Filesystem available to worker | 225,785,044,992 |
| Required reserve | 214,748,364,800 |
| Free above reserve | 11,036,680,192 |

All **34** exact `/srv/work/manager-seal-*` roots and per-root bytes are in the receipt; **31,692** unique entries were scanned. No private home, container filesystem, history, content, Messenger, recordings, or media were read. Names alone do not grant deletion authority. Symlinks are not traversed, hardlinks are deduplicated, unreadable entries/deadline exceedance refuse instead of yielding a partial green total. Four tiny regressions cover those accounting rules. Scan plus three cached-image metadata reads took 0.296 seconds.

This is **not filesystem exhaustion** and not a claim of complete historical cumulative writes. Retained task allocation alone is a sufficient lower bound to fail the stated allowance. It excludes deleted historical writes and image-layer changes. A fresh revision, cleanup, or an in-memory future build does not erase prior cumulative writes. Parent must reconcile the original allowance boundary or obtain an explicit revised cumulative budget; do not invent a reset or delete old stages to disguise one. No task-owned remote resources were created; task cleanup ledger is empty. No image acquisitions, network pulls, new stage/archive, containers, volumes, networks, mounts, processes or cgroups were created by this handoff.

Read-only recheck, with no remote file staging:

```sh
ssh forum python3 - < scripts/manager_seal_installed_capacity.py
```

**Do not submit an installed job from this handoff.** There is deliberately no placeholder `PARENT-SUBMIT` job pretending to be executable acceptance. This is the user-authorized exact-blocker outcome. Reserve/memory/tempfs/native-runtime admission must still be checked at the real job boundary if the cumulative accounting is resolved.

## Cached-image pins, unchanged and re-observed

The receipt has full IDs, digest, architecture, OS, sizes and declared volume targets. All are cached Linux/amd64. Image sizes were **not counted as newly written bytes**.

- PostgreSQL 14 alpine: `sha256:656a12855a35b53ef9bca65b3d51167a0940cc2d01b29ea5c36cb469495886dc`, 290,643,404 bytes; digest `postgres@sha256:862ffc2556a2db763f0439c8323490000cd01975d2798a845e4a8dd3a905862f`; image declares `/var/lib/postgresql/data`.
- RabbitMQ 4 alpine: `sha256:4e32174ea7bfa3a979e37493b0b7348eb1f0c46f8201a12fd91c7db8098b0fa4`, 174,989,577 bytes; digest `rabbitmq@sha256:21577d148f4f7563a87d761862fa353f59348a094833ca8b1b1a46a8102dffce`; image declares `/var/lib/rabbitmq`.
- Prior runtime donor: `sha256:908ba820e904091ddd38402404fdde212ca2f14b61b33c621dfe134d635f8ce9`, 345,216,194 bytes; no RepoDigest or declared volumes, source revision label `fead192e9fb2d560fd3b875d092819b32e7189b6`. This label is **not** a new source/bundle/runtime proof.

Initial hand-written image-format selection failed on nil upstream labels; the committed probe parses metadata and handles absent labels explicitly. Its complete rerun succeeded and is the retained receipt. No failed metadata read is called runtime verification.

## Bootstrap assets, native closure and source-DI audit

`docs/manager-seal-installed-source-audit.json` pins all audited build/entrypoint/config/module/handler/queue/storage/enrollment sources and **every existing source asset** by SHA-256/bytes. It is generated by `scripts/manager_seal_installed_source_audit.py`, not a container inspection or dependency closure proof.

| Runtime location | Audited source | Existing files / bytes |
|---|---|---:|
| `/app/assets` | `packages/backend/assets` | 4 / 81,039 |
| `/app/assets/migrations` | `packages/backend/src/core/database/drizzle` | 67 / 342,971 |
| `/app/assets/translations` | `packages/backend/src/modules/i18n/translations` | 31 / 1,489,276 |
| `/app/assets/frontend` | `packages/frontend/dist/client` | **absent**, not fabricated |

Runtime build closure and ordering:

1. `main.ts` first generates system env, then creates the **real root `AppModule`**, then runs `AppService.bootstrap()`, then installs HTTP prefix/pipes/cookie middleware and listens. Production mode avoids the Swagger source-tree write. `ROOT_FOLDER_HOST` is mandatory; system generation fixes DB host/user/db to `runtipi-db`/`tipi`/`tipi`. Task network aliases and real DB credentials must agree.
2. Before Nest construction, synthetic `/data/state`, logs, `.env`, settings/seed, `/cache`, and `/app-data` must exist in bounded writable paths. Cache opens `/cache/cache.sqlite` in its constructor. Do not bind any live owner path. Asset copy creates synthetic media **directories**, never reads owner media. `/app/assets/traefik/traefik.yml`, `traefik/dynamic/dynamic.yml`, migrations and translations must be packaged at the real absolute paths. TLS invokes **real `openssl`**, using only synthetic `/data/traefik/tls`.
3. Source module boundaries audited: configuration and database are global and export their providers; configuration imports `EnvModule`; `DockerModule` imports Apps/AppStore and exports DockerService + `DOCKERODE`; `AppLifecycleModule` imports Queue/Apps/Env/Docker/Marketplace/BackupManager/SSE and registers its handlers; Network imports Apps/Docker. Install handler dependencies remain provided/exported through these modules. AppService no longer needs `DOCKERODE`. This is a **static audit**, not proof of emitted decorator metadata or successful full-root DI.
4. Real QueueFactory construction connects to RabbitMQ before queue providers resolve (30-second connection readiness); real command consumer runs through `ModuleRef` and the per-URN mutex. Build and run the actual bundle, not a stand-alone manually wired handler. Outer job timeout must bound the source's long DB connect timeout (300 seconds), Rabbit reconnects and bootstrap's catch-and-log behavior; a listening socket alone is not success.
5. Exact historical bundle inventory remains in `manager_seal_bundle.py`: `main.js`, map, `cpufeatures-e8x7s2f1.node` and `sshcrypto-a3khcfhj.node`. The saved native references are ELF x86_64 and source/lock-bound in **old** evidence. The newly changed source must produce and validate **new** JS/map/native hashes; the old `SAVED_BUNDLE` hashes are not valid pins for a new build.
6. Build externalizes `argon2`, `class-transformer` and optional Nest integrations (exact list in pinned `build.ts`). Dockerfile separately installs the two required runtime packages. Auth calls actual `argon2.hash/verify`; Node must support `node:sqlite`. Donor Node, module dependency closure, addon dynamic loading/ABI, libc/libstdc++/OpenSSL and shipped Docker/Compose/git executables are **not verified** by this read-only image metadata audit. They must be tested from the pinned cached donor/build environment under the admitted job, with no new image acquisition. Do not replace them with stubs, alternate password logic, fake DB/queue, fake openssl, or silent optional-addon fallback. Cached image metadata alone is insufficient closure.
7. Trusted CLI source exists but is not a second entrypoint in `backend/build.ts`. A future source-pinned job must bundle the **unchanged CLI** (or run it with the already pinned actual toolchain), retain its hashes, and use actual `MandatorySeal.enroll()` under the same synthetic data mount/UID. No HTTP pre-enrollment start. Never repurpose lifecycle success/SSE/Compose-up/status as authority to clear `previous`.
8. Source safety constraint for the real rig: background `AppStatusSyncService` lists all daemon containers labeled `runtipi.managed=true` and may inspect clean exited ones, even before intersecting with the isolated DB's app rows. Raw access to a shared daemon would therefore risk prohibited unrelated-container inspection. The future rig needs a real, fail-closed test authority boundary that only forwards allowed synthetic/owned Docker IDs and constrains mutation, while actual DB/Rabbit/Compose/Docker still execute. Do not invoke an installed daemon against the unrestricted shared socket, bypass background/bootstrap paths, or change production code just to hide this prerequisite. No such proxy/engine/runtime closure is claimed or staged here.

## First installed path acceptance, still outstanding

If and only if parent resolves admission, one source-pinned forum worker job should build the updated backend and trusted CLI and carry this single path to completion:

- Verify exact pushed source/archive/lock/assets/toolchain pins; collect new bundle/CLI/native provenance. No network pulls or image acquisitions. Run the new real DTO/Nest unit tests there before the installed attempt.
- Own only fresh synthetic IDs and isolated DB/broker/data/cache state. Refuse every name/path/label collision. Confine HTTP binding to loopback; no production restart, activation, migrations, key retirement, provider/model or core Hermes change. Migrations inside the disposable real synthetic DB are part of this test, not production migration authority.
- Keep aggregate process/container/cgroup memory ≤12 GiB, aggregate tmpfs ≤8 GiB, fixture ≤64 MiB, swap 0, unchanged 200 GiB reserve and cumulative host-write cap, including retained stages and writable layers/logs. Pre-bind image-declared DB and broker volume destinations so Docker creates **no anonymous disk volume**. Include `/var/log`, Rabbit mnesia, PostgreSQL WAL/temp, `/cache`, `/data`, `/app-data`, build scratch, Docker daemon-side bind path identity and every retained output in accounting. Read back actual mounts/limits and stop on uncertainty rather than claiming requested flags enforced aggregate bounds.
- Start real cached DB/Rabbit and normal full-bootstrap manager under the confined authority. Suggested outer readiness bounds: DB 45s, Rabbit 60s, Nest/auth 60s; bounded install/start/restart 60s each; overall job ≤15 minutes. Check process exit at every poll. On failure retain only ≤64 KiB per owned phase of sanitized synthetic logs, exact phase/exit and DB/Rabbit readiness diagnostics; no private container/log introspection or endless retries.
- Real synthetic register/login and cookie-authenticated **HTTP** install with validated `skipRun:true`, `skipPull:true`, `openPort:false`; prove queue completion and exact DB record `stopped`, generated Compose/installed files exist, no activating Docker event **from the pre-install event barrier**, and no fixture container was ever created/started before trusted enrollment. Do not infer this from final `docker ps` alone. HTTP UUID is correlation, not completion; no duplicate install on timeout.
- Trusted enrollment of reviewed synthetic generated/compiler output under same data identity, then **HTTP start**, then **HTTP restart**, through real Rabbit and DB. Assert exact fixture/project/container/network identity and full lifecycle event intervals; all relevant reads/mutations must stay within owned/synthetic IDs. Keep a task-owned unconnected unrelated-network sentinel alive across normal bootstrap to prove no automatic global prune. Do not add a new prune flag.
- Exact owned-ID cleanup only, even on timeout/signals; reconcile the complete event stream and ownership ledger, remove only created containers/networks/volumes/task mounts, prove fixture/DB/broker/manager stopped and no anonymous volumes or temp roots leaked. Cleanup may not turn a failed acceptance into a pass. No daemon-wide prune or broad label deletion.

This section is the acceptance boundary, **not** an executed job. Runtime/native closure, full root DI, bounded resource enforcement, auth→HTTP install→trusted enroll→HTTP start/restart and owned cleanup remain unproved. `RUNTIPI_ADMITTED=false`.

## Evidence inheritance, not relabeling

Prior parent composite remains valid **at `814c78ab`**, with original candidate 19 / checkpoint 22 / adjacent 60 and bypass negatives: `8873f74b0a8a09f1935b13a9ea89a706e888f764cec14d552cabb6a88d715cf8`. Historical installed-spike receipt remains SHA `8f02b0fd7a5768a0bd23fda84be81604fb58363d39a58417fc483da24121ef2b`, expected exit 2. Those files are untouched and not fresh results for these product changes. No old bundle is called the new bundle; no new installed/image/composite acceptance is claimed. Broad accepted/candidate/pending transaction redesign remains deferred and unchanged.
