# Installed-lifecycle spike — demonstrated seam blockers

## Outcome

Stopped at the bounded, source-bound authority/interface probe, **not** a runnable installed Nest rig. This is the user-authorized blocker stop, not installed acceptance. No heavy job submitted, no Docker resources created, no existing container inspected, no migration/bootstrap invoked, no product change. `RUNTIPI_ADMITTED=false`.

The saved bundle and production closure remain unchanged. Parent's verified `composite-parent-814c78ab.json` / SHA `8873f74b0a8a09f1935b13a9ea89a706e888f764cec14d552cabb6a88d715cf8` remains evidence **at 814c78ab**, not a new composite for these added QA files. No replay of saved 19 candidate cases / 22 checkpoints / 60 adjacent cases or original negatives. Original failed receipts untouched. Missing lefthook remains tooling failure, never test pass.

## Exact blockers (source AND saved bundle)

1. **Real bootstrap exceeds synthetic-only Docker authority.** `main.ts:45-46` invokes `AppService.bootstrap()`, whose `app.service.ts:39-40` migrates then calls `await this.docker.pruneNetworks()` **without filters**. `docker.module.ts:17` constructs default `new Dockerode()`. A saved-backend launch against forum's host socket can remove unrelated unused networks. This is a mutation even if no unrelated networks happen to be eligible; neither a synthetic Compose project nor private DB limits its scope. We did not call it to prove damage. Denying the request via a fake/socket facade or bypassing bootstrap is not the requested real backend proof. It also trips bootstrap's enclosing catch before the later repo/assets/certificate work; accepting an HTTP listener alone could conceal failed initialization.
2. **The prepared install→enroll→start sequence is not expressible through unchanged HTTP install.** DTO permits body `skipRun`, but controller `app-lifecycle.controller.ts:16` forwards it only as `form:body`. `InstallAppHandler:32` reads a separate top-level `skipRun`; `:82` overwrites `parsedForm.skipRun` with that undefined top-level argument defaulted to false. Exact original JavaScript event-form expression executed by the probe observes **body true → queue false**. `InstallAppCommand:70-75` consequently does not skip up. No HTTP enrollment route exists. Calling the lifecycle service in-process with the other argument would bypass the requested HTTP boundary.

Second blocker is a **sequence seam**, not proof that trusted pre-enrollment is impossible: CLI accepts reviewed compiled/catalogue pairs without installed DB state. Pre-enroll-before-HTTP-install could be a valid revised sequence if its generated-pair/subnet contract is proven. It does not resolve unfiltered bootstrap pruning. Do not create an unenrolled sealed-looking fixture that runs briefly on ordinary networks to collect its generated files.

**Minimum next technical decision for controller:** choose an authority-safe real Docker daemon/bootstrap seam before implementation. A distinct real engine would require demonstrated cached engine availability, isolation/capability contract and measured RAM/tmpfs/host-write fit; none was established and no privileged/nested-engine authority is implied. Alternatively, a narrowly source-owned scope-safe bootstrap change needs review/new source bundle; the unchanged saved bundle cannot receive it. Do not authorize host-wide prune or silently bypass it. This is not an extra human approval gate on normal product updates.

## Executed evidence

Runnable offline diagnostic:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/manager_seal_installed_spike.py \
  --archive /home/telos/.hermes/cache/scratch/incognito/manager-seal-classifier-evidence/candidate/docker-artifacts.tar.gz \
  --output NEW_EXCLUSIVE_OUTPUT.json
```

Exit **2**, expected demonstrated-blocker classification; exit1 is invalid probe/evidence, never an installed result. Actual receipt: `docs/manager-seal-installed-spike-evidence.json`, SHA `8f02b0fd7a5768a0bd23fda84be81604fb58363d39a58417fc483da24121ef2b`. The probe checks the exact saved archive SHA, all four emitted bundle hashes, compares four embedded source-map implementations to source, verifies 17 production source hashes, and executes only the extracted pure object-expression in Node. This is **not** fake Nest/queue/DB or a mocked installed-rig pass.

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p test_manager_seal_installed_spike.py -v`: **4/4 passed**, 0.015s; manifest drift, changed production source and archive drift refuse before Node. `git diff --check` passed. No runtime/native-load/full-suite claims.

## DTO and startup findings to retain

- New synthetic auth uses `POST /api/auth/register` then fresh `POST /api/auth/login`, `{username: SYNTHETIC_USERNAME, password: GENERATED_SYNTHETIC_PASSWORD}` per `auth/dto/auth.dto.ts:4-8,71,76`. Retain server cookie only in ephemeral client memory; do not record password/header/session bytes. Both endpoints call real session handling. No real owner credentials.
- Install DTO: explicit `{port:10240,openPort:false,exposed:false,exposedLocal:false,enableAuth:false,isVisibleOnGuestDashboard:false,skipPull:true,skipRun:true}` is schema-valid, **but skipRun does not survive HTTP mapping**. Start/restart bodies are absent. Update body is exactly `{performBackup:false}`. HTTP lifecycle result is only `{requestId:UUID}`. Port is illustrative schema-valid, not reserved or tested. Actual fixture metadata/form validation still must agree.
- Source entrypoint runs generateSystemEnvFile BEFORE Nest creation. `ROOT_FOLDER_HOST` is mandatory; generated env fixes PostgreSQL host/user/db to `runtipi-db` / `tipi` / `tipi` (`env-helpers.ts:115-118`), so real task network aliases must agree. Production mode prevents Swagger source-tree writes. Synthetic .env/settings/seed/logs/state must be writable confined paths, never copied live configuration.
- `CacheService` opens `/cache/cache.sqlite` in its constructor; Node must support `node:sqlite`. Native source-map refs are `cpufeatures-e8x7s2f1.node` and `sshcrypto-a3khcfhj.node` (ELF x86_64 in saved verifier), while shipped runner additionally installs external `argon2` and `class-transformer`. **Native dynamic loads and donor runtime dependency closure are untested**, not established by saved Bun build.
- Official Dockerfile runtime assets are backend `assets/`, Drizzle directory→`assets/migrations`, i18n translations→`assets/translations`, frontend built client→`assets/frontend`; Compose v2.40.0, git/docker CLI/openssl and package.json also matter. Official image uses `main.mjs`, not raw `.js` under arbitrary package type. Stopped before a full asset/member-hash staging inventory: no shell pretending to be runnable with missing assets.
- Real bootstrap additionally initializes marketplace/repos, copies Traefik/config assets, generates TLS; later lifecycle bootstrap hooks start storage reconciliation. HTTP listening after a caught bootstrap error is not readiness. Fresh synthetic DB/store roots would isolate those data operations but do not isolate daemon-wide prune.

## Measured cached images and resource boundary

Only read-only `docker image ls` with PostgreSQL/RabbitMQ filters and image inspect selecting ID/architecture/OS/size/repo digest/User/Volumes were used. No Config.Env, container filesystem/logs or workload inventory. Exact metadata is retained in `docs/manager-seal-installed-spike-metadata.json`.

- PostgreSQL **14-alpine** cached `sha256:656a12855a35b53ef9bca65b3d51167a0940cc2d01b29ea5c36cb469495886dc`, 290643404 bytes; `postgres@sha256:862ffc2556a2db763f0439c8323490000cd01975d2798a845e4a8dd3a905862f`. Shipped `postgres:14` is **not** cached; no pull attempted. Alpine14 remains real PostgreSQL, not a claimed byte-identical shipped image.
- RabbitMQ4-alpine cached `sha256:4e32174ea7bfa3a979e37493b0b7348eb1f0c46f8201a12fd91c7db8098b0fa4`, 174989577 bytes; `rabbitmq@sha256:21577d148f4f7563a87d761862fa353f59348a094833ca8b1b1a46a8102dffce`.
- Both amd64/Linux, Config.User empty; entrypoint service-UID readiness is **not derived** by reading a running container. Declared `/var/lib/postgresql/data` and `/var/lib/rabbitmq` must be explicit capped tmpfs binds or they cause unwanted anonymous/overlay writes. DB+broker cached image sizes total465632981 bytes; these are not new host writes.
- Existing prior runtime donor `sha256:908ba820e904091ddd38402404fdde212ca2f14b61b33c621dfe134d635f8ce9` is cached amd64/Linux345216194 bytes, no RepoDigests and no declared Volumes; this does not establish Node/native/bootstrap availability.
- `df -B1 /srv/work` observed **226267193344 free bytes**. Original200GiB reserve=214748364800; reserve+1GiB=215822106624, observed shortfall0. Free above reserve11518828544 bytes. This is **not** admission: previous retained stage/write totals and runtime peaks were not remeasured because authority blocked first. Preserve original aggregate12GiB memory,8GiB tmpfs,64MiB fixture,0swap, total prior1GiB host-write budget; no new allowance/reset. No capacity blocker is claimed, so no invented larger RAM/disk needs. Exact authority blocker is independent of capacity.

Initial metadata command incorrectly supplied two positional image references (exit1); corrected to two filters. Exact shipped postgres:14 inspection failed absent; retained that distinction instead of claiming it cached. No cleanup needed: only local source/docs/evidence writes; no processes/resources were started remotely. A future real rig still needs owned-ID cleanup and aggregate cgroup/capped writable-path enforcement before admission.

## Transaction handoff

`docs/manager-seal-update-transaction-design.md` traces command→RPC→config→DB→SSE boundaries and proposes accepted/candidate/pending separation, per-URN whole-transaction ownership, durable recovery snapshot, DB commit/outbox plus trusted policy publication, idempotent crash reconciliation and current-revocation projection. It explicitly addresses V1→V2→V3 without clearing anchors on stage/SSE. **Parent/controller review required before product implementation.** No new public API or owner approval gate implemented.

Source pin typo corrected in installed acceptance spec. Its old sequence is now explicitly superseded by this blocker result, not silently marked executable. Original full image/provider/cutover/attestation/migration/key-retirement gates remain separate.
