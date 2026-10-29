# Installed synthetic continuation: Rabbit CLI and cleanup recovery

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
one-use controller intent remain enforced: 1 GiB new, 3 GiB retained+peak,
200 GiB reserve, 12 GiB aggregate hard RAM, 8 GiB aggregate tmpfs, 64 MiB fixture,
zero swap; cached images only. No live activation, migration, restart, key
retirement, model/provider/core change, pulls or merges.
