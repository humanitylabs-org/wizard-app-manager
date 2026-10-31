# Installed synthetic continuation: Rabbit CLI and cleanup recovery

## Live lifecycle event collection (supersedes the historical collector warning)

REUSE the saved backend, existing guard policy and strict event reconciler;
EXTEND the QA runner with `manager_seal_runtime_events.py`. No product change.

Forum `docker version` metadata reports Engine **26.1.5+dfsg1**, GitCommit
**411e817**, API 1.45 (this is not the v28.5.0 SDK). Pinned upstream Moby
v26.1.5 commit `411e817ddf710ff8e08fa193da80cb78af708191` has
`eventsLimit = 256` at `daemon/events/events.go:12`, allocation at line 26,
and oldest-event eviction at lines 113–118. The v28.5.0 annotated tag resolves
to commit `cd048300a48700294339c9c91d2dcc691cb8f63b`; the same constant is
at line 13, allocation at line 27, and eviction at lines 116–121. Both keep a
daemon-wide replay window, filtering only when loading it. The live publisher
also has a finite 1024-row queue and 100ms delivery bound; a live subscription
is not permission to relax completeness. Exact upstream source hashes and
copies are retained in the continuation proof sidecars. Debian's downstream
patchset/running daemon bytes have **not** been verified; bounded upstream replay
is proven for the matching upstream version/commit, while eviction as the exact
historical cause remains source-supported rather than captured daemon internals.

The actual retained ledger has ten runtime creation records but only five
runtime create projections from the old retroactive query. It cannot recover
missing raw bodies. Regression replay fails the unchanged exact create oracle
on those actual projection fields. A separately labelled synthetic sequence
copies the recorded create/connect shapes onto both recorded identity cycles;
it passes 10/10 create and 20/20 attachment coverage through the actual bounded
reader and Unix/chunked guard protocol peer. That is **not** historical recovery
or real Docker execution.

The runner now opens `GET /events?since=<nanosecond-now>` with **no until**
before the first start POST. Header readiness alone is insufficient: Moby flushes
headers before subscribing (`system_routes.go:269/273` in v26.1.5, 328/332 in
v28.5.0). Keep the existing never-started owned positive probe until enrollment;
delete it once after opening the stream, and observe its exact destroy ID before
start. No additional container is created. Start and restart remain exactly one
POST each, with the same response-before-assertion, same-origin, DB/HTTP terminal
and effective.json gates. Failure aborts without retry and closes the reader.

The stream is bounded by 16 MiB, 32768 rows, 64 KiB per line and a 500-second
shutdown timer. The guard still enforces its 16 MiB aggregate/600-second bound,
exact-ID event filter and chunked-upstream decoding; event reads no longer use
the ordinary 15-second idle timeout. After restart terminal/runtime readback,
wait at most 20 seconds for exact ledger create/connect coverage plus a 750ms
quiet settle. Stop/join the reader, persist **all full owned event objects** to
`runtime-events.json` in `finally`, then assert guard accounting and unchanged
strict create/connect reconciliation. Partial evidence also survives stream,
transaction, cap, EOF, malformed/truncated line and coverage failures. Foreign
metadata is refused before retention (and filtered at the guard).

Accounting stays exact: 3 lifecycle requests (install/start/restart), 10 fixture
container creates, 8 network discovery/create cycles, 9 exact negative probes
plus 1 install-window typed prune refusal. Moving deletion of the existing owned
probe does not add a create, discovery or denial. No update/rollback, official
image or full-acceptance flags become true. The 73d719f3c6ce stage remains
untouched/unsubmitted and counts in retained capacity.

### Update + rollback contract audit: not added

- `app-lifecycle.controller.ts:55–59` exposes **PATCH**, not POST, `:urn/update`.
  `UpdateAppBody` (`dto/app-lifecycle.dto.ts:25–27`) accepts only performBackup;
  there is no reviewed revision/anchor selector. An update uses the store's
  current recipe. A trusted new pair could be staged through the existing
  administrator CLI (`cli/enroll-mandatory-seal.ts:7–18`), not optional config.
- `mandatory-seal.ts:134–151` supports one new staged pair with `previous` as
  the anchor. Line 144 refuses another distinct stage while previous exists;
  this does **not** prohibit the first update. `select` at 174–175 can select
  current or previous based on the whole generated catalogue.
- `update-app-command.ts:42–136,247–259` has automatic restoration only when
  the update fails (rollbackSucceeded metadata). That is not an accepted update
  followed by an independently requested successful rollback transaction.
- There is no explicit policy-anchor rollback route in the lifecycle controller.
  The adjacent `POST /backups/:urn/restore` takes an archive filename
  (`backups.controller.ts:42–52`), not an enrolled anchor. Its command
  (`restore-app-command.ts:23–34`) stops, restores files, and returns; it does
  **not** recreate/start the runtime. BackupManager restores app/app-data and
  optional user-config, not a trusted seal transition. It cannot meet a single
  rollback POST with running-runtime recreate proof without another mutation.

The requested two single-POST transactions are therefore not a shipped contract.
The runner ends after restart rather than inventing a rollback API, deliberately
failing an update, retrying, changing product code, or trusting hostile optional
config. A source-pinned audit is included in the new receipt/static preflight.
A separately reviewed product contract or explicit permission for a different
multi-action backup/restore workflow is needed for later lifecycle acceptance.
The trusted enrollment CLI is rebuilt separately from unchanged source during
runtime assembly, not embedded in backend main.js.map. Static preflight binds
its source through the complete original manifest; backend module audit sources
must additionally match the retained backend sourcemap. The unsubmitted
`d230066d3b97` stage failed only that overly broad CLI-in-backend-map assertion;
it remains untouched and counts in retained storage, alongside `73d719f3c6ce`.

## Install-time prune refusal accounting (QA only)

The failed `5ae46c549e29` ledger is retained verbatim as
`scripts/tests/fixtures/manager-seal-guard-ledger-5ae46c549e29.jsonl`, SHA-256
`36b65574d140d9e6e00d60eab5897a8e2ade07dc8d3c60299181e603fff491b2`.
It contains the nine deliberate pre-activation refusals plus one untyped refusal
at zero-based row 20 / sequence 21, after activation and before subnet discovery.
The original accounting's exact-nine assertion fails on those actual bytes.

The product's unchanged `ensureAppDir` calls label-selected `pruneContainers`
on install, catches refusal as an empty result, then allocates the subnet.
The retained manager log confirms that order and all three completed
install/start/restart actions; start/restart disable pruning. **The historical
row does not retain its route or filters: row 20 being that prune is strongly
source/log-correlated but remains unproven at the byte level.** A regression
replacing only this row with the new typed form is explicitly counterfactual,
not a rewrite of the actual ledger or recovery of missing request bytes.

`POST /containers/prune` is now recognized before generic object lookup and
still always returns 403 without opening an upstream connection. Only an empty
body and the exact configured synthetic URN label selector receive a typed
`deny / prune_refused / /containers/prune` row with canonical map-of-true filters.
Missing/other/unknown filters, missing configured URN and bodies retain generic
refusals without caller data in the ledger. No daemon-wide mutation is allowed.

The installed runner captures the ledger sequence bounds immediately before
and after its one install transaction. Accounting requires the same nine exact
probe rows before activation, exactly one closed typed prune refusal within
that install window and before the first network-create intent, no prune
forwards, and no other refusal anywhere in start/restart. All eight existing
network-discovery/create pairs remain exact. Regressions reject duplicate and
untyped extra denials, wrong filters, forwarding, wrong windows and changed
probe identities; local socket protocol tests prove 403 and zero forwards.

### Audit of assertions after the failed accounting call

Actual retained creation rows contain ten distinct container IDs, five names
created twice, and four networks created twice. The receipt contains three
distinct request IDs and terminal install/stopped, start/running and
restart/running transactions. No change to these assertions is demonstrated.
`runtime()` already returned twice before the old failure.

The retained safe event projections contain only five of those ten runtime
container-create IDs (plus the earlier positive sentinel). The raw `events`
return value was never written: its output write follows the failed accounting
and event reconciliations. Retrospective event loss and the later exact
create/connect reconciliation are therefore a concrete continuation risk,
not an accepted complete event proof; the historical raw shape/reason for
missing events is unproven. Do not reconstruct raw Docker events from ledger
projections or weaken the exact-ID/attachment oracle. This repair leaves that
collector unchanged and requires parent review of this risk before execution.

This runner ends at `completed-first-installed-slice`; it has no subsequent
update or rollback transactions. Their receipt flags, official-image status,
`full_acceptance` and `RUNTIPI_ADMITTED` remain false. No such later acceptance
is claimed. Cleanup is unchanged; the failed run's controller errors and parent
residuals are empty. The saved backend archive and main.js are reused, never
rebuilt. Only explicit `QA_DELTA` paths change; product bytes/modes remain equal.

## First authenticated install: demonstrated request-contract mismatch

The retained `b70c9bbe1095` attempt (`heavy_b7899cc8868249fabbe8db2adc3d2c1a`)
passed actual installed `/app` confinement, full Nest TLS/Git/cache/assets/schema
bootstrap, registration/Set-Cookie, unauthenticated lifecycle denial, and the
three real queue consumers (each **1**). Paired causal Rabbit A/B and installed
RPC evidence remains inherited and immutable. Cleanup errors and parent residuals
are empty. The run then failed `http-install-skipRun` at the helper's 2xx check.
**Its actual HTTP status and response body were discarded and are not recovered.**
No 403, exact refusal body, successful installation, or applied transaction is
claimed for that old request.

The complete retained backend sourcemap matches the current production sources.
`AuthMiddleware` maps `runtipi-sid` to `authMethod='session'`; `AuthGuard` requires
unsafe session requests to supply either a same-origin **Origin** or **Referer**,
including scheme/host/port (forwarded values take precedence). The old helper
sent only Content-Type and Cookie, neither Origin nor Referer. Executing the
actual shipped guard's unchanged methods offline reproduces 403 for that
session-request shape and accepts the corrected loopback Origin. This is a
**confirmed harness/source-contract mismatch**, not recovery of the historical
HTTP response; attributing the original rejection to this guard remains a
source-backed diagnosis pending the new installed observation.

No DTO/fixture change is demonstrated: the controller passes body as form,
`appFormSchema` permits every submitted boolean and optional port, and the
handler preserves `skipRun` through the queued payload. Omitted port falls back
to store port 18080; the synthetic metadata is amd64, non-exposable, not
force-exposed, no minimum Tipi version, with empty form fields. Submitted proxy
flags are false and openPort is false, so domain/port uniqueness checks are not
requested. Those fields and all production code remain unchanged.

The helper now sends same-origin `Origin: http://127.0.0.1:3000` only on
cookie-authenticated unsafe requests, without weakening the guard. A real
session GET readback precedes the fresh absent-URN baseline and single install
POST. This is a new disposable namespace/DB, not a retry of the old POST.
Exactly one fetch/POST remains; timeout, malformed/oversized body and rejection
stop before app polling or another lifecycle mutation.

Before status, body-parse, cookie or transport assertions, the helper fsyncs
`installed-results/http-responses.sanitized.json`: status, bounded sanitized body,
parse/transport classification, captured-byte count and truncation flag.
Streaming body capture is capped at 8192 bytes, GET polling retains only
first/latest per fixed endpoint with attempt counts (16 keys / 256KiB total).
Response/request headers, cookies, passwords and session values are not exported;
the cookie and bare session value are in-memory redaction inputs only. Plaintext,
non-JSON, overflow and partial-read/transport failures remain diagnosable and
cannot become readiness/transaction success. Offline fixtures exercise the real
HTTP JavaScript without network and prove rejection capture occurs before the
assertion and exactly one transaction call.

This is a staged QA-only continuation, not new installed acceptance. Preserved
startup/auth/queue/backend/broker passes are source-bound inherited evidence;
`full_acceptance` and `RUNTIPI_ADMITTED` stay false. Only the parent may submit.

## Manager cwd and packaged startup audit (no backend rebuild)

The `547b5101da30` attempt retained its passing paired Rabbit canary and
installed-broker RPC/positive-policy proof, then the manager exited 1 without
OOM: `ENOENT scandir /assets/translations`. The final **runner** stage in
`Dockerfile:72-88` sets `WORKDIR /app`; the installed QA manager omitted it and
inherited `/` from the tooling image. Its actual option producer now supplies
`--workdir /app`; Docker `Config.WorkingDir` is read back before starting it.

`audit_packaged_startup` extends the old static inventory inside the existing
four QA/doc exceptions. The inventory script itself and all production files
remain unchanged. It reads actual packaged config/source and the same manager
argv producer as runtime, rather than a test-only command lookalike:

- I18n has the only two cwd-relative consumers: production `assets/translations`
  and development `src/modules/i18n/translations`. All translation JSON parses
  and English exists. The non-test backend scan found no other
  `process.cwd/chdir`, `__dirname`, or `import.meta` consumer.
- Drizzle uses absolute `/app/assets/migrations`; every journal tag must have
  its SQL file. DatabaseService uses its configured password variable, **not** a
  literal masked credential (tool displays can redact connection URLs). Both
  service credentials remain freshly randomized synthetic values.
- `AppService.copyAssets` consumes `/app/assets/traefik/traefik.yml` and
  `traefik/dynamic/dynamic.yml`; the logo fallback is
  `/app/assets/default-app-logo.jpg`. These source files must be nonempty.
- Production static serving uses `/app/assets/frontend`. This rig creates an
  **empty synthetic directory**, not a built frontend. The corrected audit
  labels it explicitly and retains `frontend_proven=false`.
- Writable `/data` holds generated env/state/seed/logs, repos, Traefik and TLS;
  `/app-data`, `/cache/cache.sqlite` and `/tmp` remain bounded tmpfs. Git global
  safe.directory has writable `/tmp/home`; smart HTTP serves absolute
  `/app/store.git` on loopback. Production skips development Swagger output.
- Queue setup requires the internal broker alias and matching random
  credentials, durable `app-events-queue`, `repo-queue`, `system-events-queue`,
  and their real consumers. Bootstrap catches its own errors: HTTP alone still
  cannot pass. Schema, TLS, Git, cache and actual queue-consumer gates remain.

Focused offline regressions reject omitted/wrong/duplicate cwd, shell override,
mismatched synthetic DB credential, actual Dockerfile/I18n/database/frontend/
journal mutations, and new cwd consumers. Parent preflight compares actual
backend sources against the retained sourcemap. The backend/main/archive pins,
1047-file non-QA reuse boundary, four QA exceptions, all resource/capacity bounds,
paired broker canary and installed readiness requirements stay unchanged.
This source/argv repair and static audit is **not installed acceptance**.

## File-size-limit candidate: paired canary before setup

The retained `7aea845141a5` receipt records UID/GID `100:101`, 116 successful
cookie-owner metadata calls, and 116 ping failures: exit **153**, empty stdout,
exactly `File size limit exceeded`, no timeout/output-cap termination; the
broker was running without OOM. Existing scheduler flags did not repair this.
The original receipts and backend remain immutable.

The universal 33,554,432-byte soft/hard file-size limit is now raised **only for
the Rabbit role**, to a finite **67,108,864 bytes**. All other service roles
remain at 33,554,432; build policy is unchanged. Memory/no-swap, CPU/pids/shmem,
read-only root, capabilities, tmpfs and aggregate footprint budgets do not grow.

Static audit of the exact cached image found each public CLI escript is
7,414,174 bytes, SHA256
`863b1228a2f437fe17cd2baa924a2b5bd94d943effe2c2c338e91e7e47233df0`.
The 677 ZIP entries total 10,116,184 uncompressed bytes, largest 1,004,188 bytes.
A >32MiB extracted escript/cache file is **not supported by these sizes**.
OTP 29.1.2 public source chooses a 32MiB JIT base (`beam_jit_main.cpp:216`),
doubles the first ideal block to 64MiB (`jitallocator.cpp:589–613`, maximum at
line 39), and sizes anonymous backing with `ftruncate` (`virtmem.cpp:691–695`).
This supports a file-backed JIT hypothesis; it does **not** identify the actual
failed file/FD. No exact filename or syscall trace is claimed.

Before ephemeral dependency reconstruction, the same parent-admitted queued
job acquires a one-use exclusive holder and runs two fresh brokers sequentially.
The common option producer is also used by the installed broker: same pinned
cached image, UID, internal network, env, mounts, and container policy. Actual
allowlisted inspect projections compare the pair excluding only `fsize`, and
compare the positive contract exactly with the installed broker. Cookie values
and hashes and environment values are never retained by these diagnostics.

* 32MiB negative: actual pinned startup marker, cookie-owner success, exactly
  one ping failure with the precise exit-153/file-size diagnostic. Arbitrary
  nonzero, timeout, output cap, scheduler failure or TCP is not sufficient.
* 64MiB positive: real `ping` **and** `check_running` as the same service user,
  running/non-OOM state, exec/PID1 max-file-size metadata, and fixed public
  escript sizes/hashes. No generic TCP acceptance.
* Each canary is removed and exact-ID absence verified before the next. The
  durable ownership ledger remains available to outer cleanup on all failures.
  The exclusive holder lasts through installed acceptance and closes on cleanup.
* Only a paired pass continues expensive setup and unchanged installed
  acceptance. `rabbit-limit-canary.json` retains bounded synthetic evidence.
  The precise signature raises `FatalBrokerCLI` immediately; transient
  readiness handling remains, without repeating a deterministic failure 116 times.

Source-only tests and static preflight do **not** establish real positive RPC.
Only the parent may submit this job. The exact backend archive and `main.js`
remain reused; prior privacy evidence is frozen, not rerun or reinterpreted.


This is a **QA-only candidate correction**, not installed acceptance. No production
source or build input changes. Full acceptance and `RUNTIPI_ADMITTED` remain false.
Only the parent may submit the exact staged `PARENT-SUBMIT.json`; no child job,
service, container or extra runtime helper is authorized by this handoff.

## Preserved failure and bounded diagnosis

Original source: `c44b1ac6dab07b94a75074169ffcd4a35fd6b87a`.
Original durable job: `heavy_ece3f08a10404bf8a187cd1e5b6477e0`.
Original worker log SHA256 (parent supplied):
`db8656b0dff74bc20f4f1471304e7e2cb025d5bf7d716ad84eedd4d1586b1aba`.
The original `/srv/work/manager-seal-installed-c44b1ac6dab0` remains unchanged.
Receipt SHA256: `202efaaadc2ca6cf5a791290ac3639daf66d9d725cb43d403904af8a3af58be9`.
Original cleanup SHA256: `d2459a382f5cdd0383d9c621446a8b0f26a8578352e5d8a918403be5ede57acb`.

The retained receipt says `rabbit-ready` timed out, with the last `owned-exec`
exit 153. Original code discarded stdout/stderr and reused the same label for
cookie metadata, `ping`, and `check_running`. **It cannot establish which exact
command failed, nor the cause.** The retained broker log proves RabbitMQ 4.4.0 on
Erlang 29.1.2 completed startup at 17:08:23, after binding 5672; the pre-cleanup
state was running and not OOM-killed. TCP/startup alone is not RPC/AMQP proof.

Release v4.4.0 `deps/rabbit/scripts/rabbitmq-env` SHA256:
`4593a53a99e9115feaeddb272e206b1d39c22fb9532f9c143fc682e78c9d62af`.
Its `run_escript` puts **RABBITMQ_CTL_ERL_ARGS**, not the server-additional variable,
into `ERL_FLAGS`; diagnostics and ctl wrappers use that function. The cached public
image metadata confirms version 4.4.0, HOME `/var/lib/rabbitmq`, and sbin path
`/opt/rabbitmq/sbin`. The next admitted run additionally copies the public static
`rabbitmq-env` from its already-required unstarted metadata donor and verifies
the same exact `run_escript` CTL-args-to-ERL_FLAGS route before proceeding,
recording both the actual source hash and release-source reference hash. The
**entire 7117-byte source must equal the pinned SHA256 above**, including comments.
One shared recovery validator also requires the exact newline-before-brace
`run_escript()` function and its full function body: `-boot $CLEAN_BOOT_FILE`
precedes `$RABBITMQ_CTL_ERL_ARGS`, with both distribution port bounds, the optional
`-noinput` branch, and the unchanged `ERL_FLAGS` escript callback. No whitespace
normalization, guessed prefix, or substring-only acceptance is used.

All three Erlang CLI commands now explicitly receive
`RABBITMQ_CTL_ERL_ARGS=+S 2:2 +SDcpu 1 +SDio 1 +A 4`, the service UID:GID, and a
10-second RPC timeout inside a 15-second bounded observer. This routes a bounded
thread budget through the documented source seam. **Scheduler/PID exhaustion is
only a candidate explanation; the fix and original cause are unproven.** A new
success would prove candidate readiness, not retroactively diagnose exit 153.

`rabbit-cli.sanitized.json` records each exact action, exit/termination reason,
explicit CLI env/user, captured byte counts, and first/latest sanitized failure
plus total attempts/failures. Reads are capped at 8192 bytes per output stream;
cap hits/timeouts never count as ready. Cookie/credential lines (including cookie
hashes and indented continuations) and synthetic secrets are withheld. The cookie
probe only tests nonempty ownership and emits numeric stat metadata; it never
reads a cookie value or hash. There is no environment dump. Readiness still
requires metadata **and real `ping` and `check_running`**. The later real
`list_queues name consumers` gate still requires positive consumers for all
three shipped queues; it uses the same CLI budget. No TCP-only fallback exists.

## Exact public-source preflight repair (failed continuation retained)

Continuation `5269821a0714087b8f9105c93858cc73368af433` failed before build or
resource services: its inline check guessed `run_escript() {` and a
`$RABBITMQ_CTL_ERL_ARGS $default_erl_flags` prefix, neither of which exists in the
actual retained public bytes. Independent AST execution reproduces that rejection;
the original pinned-image bytes are identical to the retained upstream file.
The shared validator now accepts those exact bytes. Four added offline tests
carry the complete compressed public source inside the existing QA test file,
execute the original rejection and corrected installed-path AST, and reject
changed/missing CTL routing, callback changes, function/boot/noinput changes,
duplicate definitions and unrelated full-file drift. Body/definition negatives
are independently exercised with a test-only repin; production never repins.
`QA_DELTA` remains exactly the same four paths, preserving the full non-QA closure.
No source/runtime success here proves broker scheduler readiness or the original
exit-153 cause. Failed receipts, saved backend and earlier evidence stay immutable.

## Exact cleanup classification and supplemental absence proof

The original parent cleanup incorrectly listed two volumes as residual because
it accepted `No such` but not Docker's actual lowercase diagnostic:
`Error response from daemon: get NAME: no such volume`.

The shared classifier now requires exit **1**, empty/`[]` stdout, and a full,
single-target, kind-specific diagnostic with the exact escaped owned identifier.
Case variation is accepted only inside these complete formats. Wrong targets,
extra stderr, permission/connectivity errors, in-use objects, success exits and
other exit statuses remain failures. Both controller and parent cleanup use it;
network/volume removal is followed by exact inspect rather than treating removal
stderr as absence. The original failed cleanup receipt is never rewritten.

A separate `SUPPLEMENTAL-ABSENCE-PROOF.json` preserves original input hashes and
actual exact-ID/owned-name metadata results. All 15 original owned object IDs
(including the controller, two named volumes and infra network) were absent;
11 original exact creation names combined with the task label yielded no
containers. No deletion, foreign object read, live logs or environment read was
used to obtain this supplemental proof. It does not turn the original failed
run into a passing receipt.

## Backend reuse and adjacent prerequisite audit

The pinned retained archive is
`/srv/work/manager-seal-installed-c44b1ac6dab0/installed-results/build-artifacts.tar.gz`,
SHA256 `e9d848ad144e5afda0f5b7c28095b08ca23fcf43bf630530f07dc7702d1cb53d`.
It contains exactly four backend dist outputs plus build metadata/sanitized logs.
It does **not** retain node_modules, common dist, review/enrollment executables,
runtime assets or the synthetic bare store; those were on removed tmpfs.

`--continue-saved` first compares the entire old/new Git-export membership,
content and modes outside the four exact QA/document paths in `QA_DELTA`. This
includes backend/common source, lock/manifests, build config, asset/migration/
translation inputs, CLI source, store helper and unchanged guard. It pins the old
manifest/receipt/archive, and mounts only the archive read-only into the existing
builder. A bounded, duplicate/traversal/link/special/sparse-rejecting reader writes
only the exact four dist files and verifies their archived sizes/hashes. Backend
`bun run bundle` is **not executed**. Main.js remains
`b915a5ae7e42dafa1abf7d3b7f174f029941dfc8a23dc9373bf7d4dbe5dbfc3a`.
The saved map/native provenance is validated against unchanged source and the
frozen dependency comparison. Production-source and current QA-source revisions
stay distinct in receipts; no old pass is relabeled fresh.

Because compiled dependencies/CLIs were not retained, the existing bounded
bootstrap/frozen install and common build must re-materialize prerequisites for
those absent executables. The two absent CLIs are compiled using unchanged
source; runtime assets are copied from identical tracked bytes and their complete
inventory must equal the original build result. Only fixture paths/owner-bound
compose recipe, compiled fixture and bare store are regenerated for the new
source-bound root. There is no claim of retained dependency-cache reuse. This is
runtime assembly with inherited backend output, not a backend rebuild.

The same job keeps the real producer protocol and pre-bootstrap guard negatives,
PostgreSQL readiness, real Nest/auth/bootstrap/DB/AMQP-consumer requirements,
proxy-only manager socket, unchanged later transaction/continuity gates and
independent owned cleanup. No separate producer-only job is needed. Local focused
offline regressions cover CLI args, fail-closed readiness, bounded exit/stderr
capture, lowercase absence vs real errors, parent cleanup integration, reuse drift
and hostile archives. The continuation runs those same offline regressions in
the cached controller's actual interpreter before runtime work; the staging
interpreter's success is not a pinned-runtime claim.

Root remains exactly `/srv/work/manager-seal-installed-<source[:12]>`, without an
arbitrary suffix. Existing per-run/retained/reserve admission, hard limits and
one-use controller intent remain enforced: 1 GiB new, 4 GiB retained+peak
(James-approved task-only expansion recorded below),
200 GiB reserve, 12 GiB aggregate hard RAM, 8 GiB aggregate tmpfs, 64 MiB fixture,
zero swap; cached images only. No live activation, migration, restart, key
retirement, model/provider/core change, pulls or merges.

## Network-list wire repair: installed HTTP success is retained, not rerun

The actual `958b07a30219940c655de0b564f6b9a33c2ef8d8` job installed through
session-authenticated HTTP: GET 200, install POST 201 with a real requestId,
DB `stopped|1|10.128.10.0/24`, installed GET confirming `skipRun:true`, and no
fixture containers before enrollment. The trusted review pair enrolled. A single
start POST returned 201 but the manager logged `invalid filter` and the app did
not run. Guard ledger sequence 48 was a synthetic discovery 404, 49 forwarded
GET `/networks`, 50 was daemon 400; this was NOT a guard refusal. No retry occurred.
The original receipt remains failed. Internal cleanup and parent residuals were
empty. Preserve those source-bound auth/boot/install/broker passes separately
from the still-unproved start/lifecycle/grant-seal result.

### Serialization diagnosis and evidence boundary

The retained **pinned Compose protocol witness**, not the real daemon log,
contains this incoming selector at API v1.51:
`{"name":{"agent-synthetic_migrated_agent-synthetic-edge-uplink":true}}`.
The old guard preserved that map but injected
`"id":["0000000000000000000000000000000000000000000000000000000000000000"]`.
The same old rewrite is deterministic for the real first discovery (`...-seal`).
The failed real run did not retain its exact query or response body: do not label
the witness as an actual-daemon wire capture. Its source/ledger/error evidence
supports this causal path; the fresh daemon A/B gate must confirm compatibility.

Complete public Moby v28.5.0 sources are pinned by bytes/SHA256 in
`tests/fixtures/manager_seal_wire/public-sources.json`:
`client/network_list.go` calls `ToParamWithVersion`; this delegates to `ToJSON`
unless API is below 1.22. `Args.fields` is `map[string]map[string]bool`. The network
router calls `filters.FromJSON`, which tries the complete map-of-bools, then the
complete legacy `map[string][]string`. The mixed map + array fails BOTH JSON
unmarshals and returns the generic `invalid filter`. A full-ID network filter is
valid; the serialization was inconsistent. The old fake peer never decoded this
schema, which is why its protocol pass did not establish engine compatibility.

The guard now always emits canonical maps of true-valued sets, normalizing only
homogeneous, strictly validated canonical or legacy inputs. Unknown/case-shifted
keys, mixed schemas, false/non-boolean set entries, wrong value types, duplicates,
empty sets/strings, control characters and unsupported selectors refuse before
forward. Caller ID prefixes intersect actual owned full IDs; caller immutable
image references intersect pinned references instead of being overwritten. Other
caller selectors remain AND constraints. No authority always inserts a full
zero-ID constraint; nothing can become an unfiltered/global list.

Only known synthetic network names and owned/zero IDs can enter new query audit
rows. Errors retain only byte count plus the fixed `invalid_filter` or withheld
class, never arbitrary daemon strings, request bodies, credentials or headers.
The existing aggregate guard-ledger cap remains 16 MiB.

### Fresh queued canary and failure stop

After source and capacity admission, the controller runs a real-daemon canary
**before donor extraction, broker canary, tmpfs dependencies, or installed
lifecycle**. Two private guarded sockets admit only one fixed synthetic network
list request, empty authority, and no mutation/discovery/global route. The first
recreates the old mixed query with the all-zero ID and must produce 400 plus
`invalid filter`. The second emits the corrected canonical query and must produce
200 with `[]`. Both pass through the actual `Handler` and the same raw daemon;
they are not answers from the old fake peer. The <=16 KiB receipt is explicitly
not installed proof. Any mismatch aborts before the expensive prerequisites.
This code is prepared, **not executed or passed by this repair**.

The offline regression additionally includes a native Go test using the complete,
unmodified pinned Moby `parse.go` and `errors.go`, no module/network dependency.
When Go is absent it reports a skip, not an actual-SDK pass. The separate Python
schema model is labelled as such. Local Go is absent; native execution remains
unverified. The real-daemon gate remains mandatory regardless of native results.

A terminal start failure is `stopped` in the actual shipped handler, not a
fictional `failed` enum. The first poll observing an unexpected stable state or
a new synthetic `App ... <action> failed:` log marker saves a read-only DB state,
GET app response/status, requestId and only actual guard-owned fixture IDs, then
aborts. Restart preflight errors that preserve `running` are covered by the new
log marker. There is no second POST, install/start retry, or 180-second wait after
terminal failure. Successful skipRun install still accepts terminal `stopped`.

### Capacity decision: blocked before fresh staging

The exact read-only, inode-deduplicated namespace scan retained **2,251,644,928
allocated bytes**. A new 1,073,741,824-byte peak totals **3,325,386,752**, exceeding
the unchanged **3,221,225,472** maximum by **104,161,280 bytes**. Remaining peak
headroom is **969,580,544 bytes**, before any fresh stage. Free space was
299,713,110,016 bytes, so the 214,748,364,800-byte reserve is not the blocker.
Historical roots are not deleted, reset or relabelled.

A smaller peak is allowed in principle: 1 GiB is a maximum, not a minimum. But
512 MiB is **not proved** by this source: its declared non-margin categories total
**644 MiB (675,282,944 bytes)**, already larger than that candidate. An intermediate
896 MiB estimate would fit arithmetic headroom but is also not a proved bound:
Docker overlay/metadata has no task-local allocation quota; controller output,
guard ledger and all future lifecycle/receipt/rounding writes lack a combined
pre-write allocation fence. Approximately 104 MB observed in previous runs is
not an upper bound. Do not merely shave margin and assert safety.

`smaller_envelope_assessment` records those missing bounds and does not lower the
admission/controller/checkpoint/staging peak. Therefore no new remote stage,
files, canary, heavy submission or runtime was created in this repair. The
capacity-blocked continuation specification is local and explicitly cannot be
submitted. A later proven smaller route must consistently enforce its source,
archive/tool/log/metadata/overlay aggregate at staging and every runtime boundary;
otherwise the exact present capacity refusal stands without changing owner caps.

The current `QA_DELTA` adds only the exact guard, capacity, focused test and pinned
public-source fixture paths listed in `manager_seal_installed_recovery.py`, with
no wildcard. The earlier four-path/unchanged-guard text describes the earlier
continuation, not this repair. Backend/common/product/lock/build/runtime assets
must still match the original complete export byte-for-byte and mode-for-mode.
The original backend archive and `main.js` hashes above remain mandatory; no
backend rebuild or broader privacy rerun is introduced.

## James-approved task-only storage expansion ("Expand")

James explicitly approved **only this manager-seal installed test's total
retained-plus-not-yet-allocated next-peak cap** increasing from **3 GiB
(3,221,225,472 bytes)** to **4 GiB (4,294,967,296 bytes)** by replying **"Expand"**.
This supersedes the historical capacity refusal above; it does not rewrite the
old scan, stage, receipt or blocked decision. The historical retained
2,251,644,928 plus 1,073,741,824 next peak is still **3,325,386,752 bytes**:
refused under the old 3 GiB cap, admitted under 4 GiB if the fresh reserve check
also passes. Fresh stage allocations remain in retained usage and reduce only
this invocation's unallocated peak. No old stage deletion, accounting reset,
receipt relabeling or double counting is permitted.

Unchanged: **1,073,741,824 bytes per-run additional maximum** (stage, logs,
artifacts, tool copies and daemon metadata/overlay included); fresh free space
must cover **214,748,364,800 bytes plus the not-yet-allocated footprint**. Also
unchanged: 12 GiB aggregate RAM, 8 GiB aggregate tmpfs, 64 MiB fixture, zero swap,
cached images only/no pulls, mandatory real-daemon old-400/new-200-empty canary
before donor/broker/dependency setup, one POST per action and immediate terminal
failure evidence/abort. Backend archive and main.js pins above remain unchanged;
the backend is restored, never rebuilt. No live activation/restart/migration,
private data access, merge, or core/provider/model/key change is authorized.

`QA_DELTA` additionally lists the exact capacity regression and four mirrored
QA-envelope documents, with no wildcard. All product source/build/runtime inputs
still require exact original byte/mode equality. A new canonical stage must pass
isolated source/sidecar inventory and static preflight with the fresh 4 GiB gate;
only the parent may submit. Capacity/static admission is not installed acceptance.
