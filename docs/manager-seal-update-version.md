# Installed update version regression: QA publication durability

## What the failed run actually proves

Retained source `9a016bb8c3396176e5b289a56fad3aad412e4351`, job
`heavy_eb6fe92c74cd4e88b54be27178733abd`, accepted exactly one update PATCH
(request `8cceba77-f8a7-4cb4-96c1-03737e3dc57c`). Its attached log records the
first repo copy at 09:59:21, old deployment down at 09:59:22, the second repo
copy at 10:00:14, recreation, and handler success at 10:00:16.
The final authenticated GET says **installed info tipi_version=1 and repository
latestVersion=1**, as well as DB app.version=1. Five new container IDs do not
prove revision 2 ran. The seal still permits the previous reviewed revision.
The failure is real, not a pass disguised by a slow DB observer.

Exact, unmodified synthetic artifacts and the previous runner source are in
`scripts/tests/fixtures/manager-seal-update-9a016bb8c339/`, with per-file SHA256
and provenance. Original stage contents remain untouched.

## Proven QA defect; historically likely triggering race

The previous `manager_seal_installed_review.ts` publish branch wrote v2 only
into `/data/repos/migrated/apps/agent-synthetic/`. It never committed it or
published it to the Git HTTP upstream. That upstream was the read-only v1
`/app/store.git` built by `manager_seal_installed_path.py`.

`app.service.ts:64-66` schedules production repo `update_all` at `*/15 * * * *`.
`app-store.service.ts:27-34` pulls enabled stores for that event.
`repos.helpers.ts:155-184` fetches origin, force-moves the branch, and forces a
checkout. It logs the ordinary pull path at debug, while this rig uses info.
The observed update spans the 10:00 boundary. A refresh then would discard the
uncommitted v2 files before the 10:00:14 second copy.

**Proven:** dirty-clone-only QA publication is not durable against the product's
normal forced refresh; a local real Git HTTP replay reproduces v2->v1 loss,
and committed upstream publication survives repeated refresh. The actual
final HTTP body contains v1 in both installed and repository metadata.
**Not proven historically:** the repeatable queue event's actual firing and
exact overwrite time; the old run retained no debug pull line, Git-ref trace,
or raw installed/repo config hashes. The 10:00 refresh is a source-supported,
time-correlated explanation, not a recovered event. Do not call it a proven
product version-write bug.

`AppFilesManager.getAppPaths` resolves this URN to
`/data/apps/migrated/agent-synthetic`; `AppStoreFilesManager.getAppPaths` resolves
the same store to `/data/repos/migrated/apps/agent-synthetic`.
`update-app-command.ts:132,159` copies this repository twice. There is no wrong
store slug in those paths. `update-app.handler.ts:62-67` reads installed metadata,
updates the DB version, then emits success. `update-config.handler.ts:42-45`
also requires installed metadata, so a persistent null/unavailable result would
not normally produce the observed success. The final installed info is v1.

## Offline shipped-schema check

Extract the exact common-schema JS and installed-info method from the retained
backend source map, not a new backend build. `manager_seal_update_schema_probe.py`
generates the exact fixture literal plus the original v2 edits (JSON.stringify
compact bytes SHA256 `6afed8b87c3b2ce6f63daa04e0eb4175612f475218e5bbdf8fed163922e12e39`).
With the shipped lock's arktype 2.1.29, @ark/schema/util 0.56.0, arkregex 0.0.5,
it is schema-valid, available, and the exact installed-info method returns
version 2. This rules out the proposed config edits as a schema failure; it is
not evidence of the historical installed file's unknown raw bytes.

## Repair and fail-closed continuation

- Commit the reviewed config+recipe in a synthetic work repository; make an
  immutable bare v2 Git upstream appear atomically under `/data/reviewed-update`.
  The loopback Git server serves it. Fetch that same HTTP origin into the manager
  clone and verify its commit, schema, version and exact bytes before PATCH.
  No production source, schedule, lifecycle handler or seal policy changes.
- Retain upstream commit and config SHA256; require installed config hash equality
  in addition to HTTP+DB+runtime revision+sealed catalogue/effective launch checks.
- Wait for a terminal synthetic-URN handler log within the single-request offset
  window before judging running|1. Resample DB after observing that log to avoid
  mixing a pre-completion DB query with a post-completion log read. Logs omit
  requestId: correlation is explicitly one PATCH+URN+offset, not fabricated IDs.
- Keep the 180-second wait and one PATCH; no retry. At wrong terminal version,
  handler failure, unexpected state or wait failure, write bounded read-only
  diagnostics before abort: installed/repo/reviewed config versions+SHA256,
  shipped schema validity, installed recipe commands+SHA256, upstream/clone refs,
  DB status/version/updated_at, authenticated GET, owned IDs, and last 200 sanitized
  manager log lines. Individual failed reads do not erase other diagnostics.
- A future handler success with valid available installed v2 but DB running|1 is
  recorded as `product-version-write-defect` and still fails. Never patch product
  source or accept version 1 to make this test green.

Production archive remains
`e9d848ad144e5afda0f5b7c28095b08ca23fcf43bf630530f07dc7702d1cb53d`,
main.js remains `b915a5ae7e42dafa1abf7d3b7f174f029941dfc8a23dc9373bf7d4dbe5dbfc3a`.
QA exceptions are exact file paths in `manager_seal_installed_recovery.QA_DELTA`;
non-QA exported membership/content/modes must remain identical. New QA helpers
are rebuilt, not the backend. Rollback/promotion, successive updates, grants,
official image and full acceptance remain unproven. Static preflight is not a
runtime pass, and no heavy work is submitted in this preparation.
