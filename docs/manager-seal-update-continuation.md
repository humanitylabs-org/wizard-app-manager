# Installed synthetic single-update continuation

QA only. Product backend/archive and its complete non-QA Git export are reused,
not rebuilt or patched. This continuation is **prepared**, not installed proof.
No heavy submission is performed while preparing it.

## Transaction

After install (skipRun), authenticated start and recreate-on-restart, keep the
same bounded live event subscription open. Independently reconcile the first
slice against its 10 containers, 20 attachment pairs, 8 network creations and
exactly 9 hostile probes + one typed install-only prune refusal.

The QA `/app/review.mjs candidate SUBNET` derives revision 2 exclusively from
immutable `/app/compiled.json`, `/app/recipe.json` and the synthetic store config.
Only executable `command` changes (a Python comment); the security envelope and
entrypoint stay identical. It uses the shipped builder and `validateSeal`.
`/app/enroll.mjs stage /data agent-synthetic:migrated ... EXPECTED_SHA256` performs
the shipped CAS staging. Review/publish reads the actual staged pair back, then
publishes only those synthetic catalogue recipe/config bytes into
`/data/repos/migrated/apps/agent-synthetic`. No hostile optional config is read,
imported, normalized, or enrolled by this helper.

Send exactly one same-origin authenticated `PATCH
/api/app-lifecycle/agent-synthetic:migrated/update` with
`{"performBackup":false}`. There is no revision-selector DTO. Existing HTTP code
captures bounded sanitized status/body before any assertion; no transport,
rejection, malformed result or terminal lifecycle failure is retried.

Wait for DB `running|2`; `running|1` is recovery/failure, never accepted success.
Read the app back over HTTP and SQL, read the actual seal/current+previous pair
and installed generated recipe via the trusted helper, verify all five new
containers run the revision-2 command, and require IDs disjoint from both prior
cycles. Preserve the sanitized fixed-path Compose invocation proving use of the
shipped ephemeral `effective.json`; these bytes are deleted in product `finally`
and are **not** claimed retained. Reconcile the real guard creation ledger and
live daemon stream for 15 containers, 30 attachment pairs and 12 networks.
Refusals remain exactly 10, all before update: there is no update exemption.

Set `single_accepted_update_pass` only after final ledger+event checks, and clear
it on cleanup errors. `rollback_transaction_pass`, `full_acceptance` and
`RUNTIPI_ADMITTED` remain false. The earlier audit object is retained as historical
first-slice context; `update_continuation_contract` states the new scope.

## Source-backed limits

The machine audit emits SHA256 and exact line pins from current product source.

* `mandatory-seal.ts:129-153` explicitly lacks the trusted installed/promotion
  transition. At 144 a second distinct stage is refused while `previous` exists;
  a no-op stage at 141 deliberately preserves the anchor. `enroll` (114-127)
  refuses a different current digest. No HTTP or trusted CLI clears/promotes the
  anchor; CLI lines 7-18 exposes only enroll/stage.
* `update-app-command.ts:118-139` snapshots app files/env before prepare/pull;
  141-164 stops/down/recreates after validation; 171-195 restores files/env and
  conditionally recreates the prior runtime on failure. It never changes the
  seal anchor. This is distinct from Compose's sealed effective snapshot.
* `docker.service.ts:188-210` validates rendered configuration, writes the
  effective snapshot, executes it, and removes its directory in `finally`.
* Commit `3116f500f4d7df959a9f4d586dec4f9a9b7564b5` isolates nested remediation
  onto that effective snapshot; its mocked forced exception is not a shipped
  HTTP fault-injection contract or an anchor-promotion implementation.

Therefore do **not** stage a third revision, edit `policy.json`, counterfeit a
successful installed transition, use a hostile override as trusted input, or add
an arbitrary guard refusal to manufacture rollback. A forced-failure automatic
rollback after the accepted revision needs the missing trusted promotion contract
first. Its installed proof is not added or claimed.

App-grant revocation is shipped: `wizard-shares.controller.ts:19-23` accepts an
authenticated POST with `{to,approve:false}` and reads grants back. Share revoke
at 25-34 additionally recreates runtime. This retained synthetic fixture has no
`wizard.md`, provider or existing app grant. `wizard-storage.ts:208-212` skips it;
308-309/327-328 reject a missing consumer before grant mutation. There is no
meaningful grant on **this fixture** to revoke or preserve across update. A
no-op revoke would be false evidence; adding a reviewed manifest/provider and
its new mount/key envelope is a separate fixture expansion, not a missing product
revoke route. This gap is recorded rather than changing the accepted first-slice
fixture or reading any private app.

## Offline evidence and admission

`scripts/tests/fixtures/manager-seal-accepted-c8f08cf36f7f` contains byte-identical
read-only copies of the accepted retained ledger, receipt, raw owned event stream
and parent cleanup. `SHA256.json` records provenance, exact hashes and lengths.
Tests replay them unchanged and reject treating two cycles as three; deliberately
synthetic third-cycle inputs are labelled unit tests, never engine evidence.
Tests also cover exactly-one PATCH, CAS review/stage/publish sequencing, response
capture, terminal aborts, old DB/HTTP/seal/runtime rejection, effective-path
selection and accounting negatives.

The fresh stage is canonical `/srv/work/manager-seal-installed-<sha12>` with root
0700, explicit per-file SOURCE/SIDECARS hashes/modes and no wildcards in the QA
reuse exception list. Count non-QA baseline membership from the retained manifest
minus that explicit list; do not inherit an earlier stage's magic count when an
existing QA helper becomes an exception. A failed static stage is preserved;
corrections get a new commit and new canonical root, never in-place remote edits.
Preserve every old stage. Static preflight is Python
`-I -S -B`; capacity remains 4 GiB retained-plus-pending, 1 GiB per run, 200 GiB
free reserve. No Docker calls, runtime execution or sustained remote work belongs
to staging/static preflight. Fresh installed acceptance remains for the parent.
