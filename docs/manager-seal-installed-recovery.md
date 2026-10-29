# Installed synthetic continuation: Rabbit CLI and cleanup recovery

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
recording both the actual source hash and release-source reference hash (packaging
comments need not be byte-identical). No new helper is introduced.

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
