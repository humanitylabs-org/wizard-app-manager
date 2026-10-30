# Installer health diagnostic: static trace; native preparation BLOCKED

REUSE: the exact 851 ordinary fresh synthetic thin baseline, source-closure
checker, pinned backend/Service archives, root-helper entry and exact-ID cleanup.
EXTEND: QA-only structured health capture before cleanup, **only after admission**.
NEW: no product implementation. No product fix is justified yet.

## Decision and evidence boundary

The completed one-refinement read-only sequence established `compose-unhealthy`,
not a failing service or root cause. Do not expand that classifier or re-open the
failed 610 guest. Its four removed containers cannot be inspected retrospectively.
This packet performs source/metadata checks only. **No collector, native stage,
job.json, VM, Docker invocation, test run or installer run is prepared/executed.**
The fresh diagnostic is blocked by capacity and incomplete historical upstream
identity. The finite design below is an acceptance contract, not working runtime
instrumentation or permission to submit an unchanged installer.

Backend source: `595523fb8f930d387d5cfb0a7aa6c7d7031c05e2`, image
`sha256:1c10570477becada8c613a55ff402f12b49d54e74fd9e16fefba678d57ead426`.
Saved archive SHA `a953c4ce3590bc1448bacb0b5b259334a99a608fb0377af425daf87c888d49c0`
is inherited; not freshly rehashed. The static preflight compares all **622**
accepted backend input rows (path/kind/mode/hash) against current Git archive
bytes, and compares all **17** saved image layers with the accepted image record.
The saved image configuration has no HEALTHCHECK; its command is
`node ./main.mjs`, entrypoint `docker-entrypoint.sh`, cwd `/app`, default user.
These are saved-image metadata, not observations of the removed containers.
Service archive/502-source-member proof and 105 passing test IDs remain inherited,
not rerun, rebuilt, or newly relabelled. Product bytes remain those of 610/0ae.

## Static call chain and two explicit health gates

At pinned product revision `610716410029f2f70fe4233102584c30e9b9022f`:

1. `scripts/prepare-alongside.py:218-220` calls the actual root
   `prepare-named-user.sh wizard-system` with the manager's clean environment.
   `prepare-named-user.sh:24-35` downloads `https://setup.runtipi.io` if `.env` is
   absent and invokes it with `--env-file`, **without a version or source hash**.
   It seeds loopback proxy ports, not backend app acceptance.
2. The root helper installs custody, then line 291 calls exactly
   `runuser -u wizard-system -- env -i ... bash .../install.sh`. The historical
   failure is a real CalledProcessError here, not the earlier source-check error.
3. `install.sh:84` calls `wam.release_pins.configure_manager`: it replaces the
   manager image and extends environment/labels; it does not replace the base
   DB/queue/proxy topology or manager health definition.
4. `install.sh:88`: real Compose `up -d --no-recreate --wait runtipi-db runtipi-queue`.
5. `install.sh:116`: real Compose `up -d --no-deps --wait runtipi`.
6. Only after that: line 118 starts the missing reverse proxy, then line 122 exits
   in separated mode. A dependency-unhealthy message alone does not distinguish
   gates 4 and 5. No exact historical line number is claimed from that message.

`WIZARD_RELEASE=none` is **not** evidence that WIZARD_RELEASE_PINS is absent.
A future diagnostic must assert that this source's exact no-pins synthetic
fixture has no release-index service; otherwise stop at scope mismatch, not widen
its four-service allowlist.

## The right upstream source, not the development Compose file

The local `docker-compose.prod.yml` is a development/build stack and is NOT
installed by the upstream CLI. It specifies DB retries 120 and manager timeout 3s;
using those timings for the real base would be wrong.

As a **public source comparator, not historical identification**, the current
public release query returned v4.11.0 (published 2026-10-21), retained Git tag
`d377b7c52567c8a852f095d5b6dd8194c66f5a62`. Its `cli` gitlink is
`c5d951ed0c92c57964b607d4e9e74db820a94a84` in `runtipi/cli`.
The CLI embeds `internal/assets/docker-compose.yml` through `assets.go` and copies
it during PrepareEnvironment. That template SHA is
`f7da61f9db29bc4a800e53b298554f72bb6a636fe9c438519e3ef50b5202ca09`.
Pinned public snapshots, URLs and file hashes are retained in the static packet.
The setup URL currently redirects to upstream master; historical 610 setup/CLI,
rendered base YAML and exact base image IDs were not sealed in exported evidence.
The latest-release response today cannot fill that hole. Do not silently choose
v4.11.0, rebuild a backend, resolve new mutable image tags or call that a replay.

| Exact allowed project/service | Public CLI image/config | Health and dependencies |
|---|---|---|
| `runtipi` / `runtipi-db` | `postgres:14`; bind `./data/postgres` | `CMD-SHELL pg_isready -d tipi -U tipi`; interval 5s, timeout 10s, retries 20, start period 5s |
| `runtipi` / `runtipi-queue` | `rabbitmq:4-alpine`; synthetic generated credentials; loopback AMQP | no explicit template healthcheck; manager requires service_started |
| `runtipi` / `runtipi` | upstream image substituted with exact accepted backend ID; `.env`, `/data/*`, `/cache`, socket and host mounts | `CMD curl -f http://localhost:3000/api/health`; interval 5s, timeout 10s, retries 20, start period 10s; DB healthy + queue started |
| `runtipi` / `runtipi-reverse-proxy` | `traefik:v3.6.14`; loopback ports from override | manager healthy; no explicit template healthcheck |

Do not assume absence of inherited image HEALTHCHECK for queue/proxy from absence
in YAML. Their exact original image configs/IDs are not in the exported record.
No `runtipi-redis` enumeration is allowed: that name occurs in CLI legacy cleanup,
not the four-service template. No owner apps/Service/release-index enumeration.

The backend's actual `/api/health` implementation delegates **only to queue
readiness**, via `QueueFactory.isReady()`; it is not a DB/cache/filesystem test.
A connection refusal can mean startup never reached HTTP; HTTP 503 can mean queue
not ready even when the manager process runs. PostgreSQL readiness alone proves
neither migration compatibility nor application startup.

No service-count/dependency-graph mismatch is shown between this public base
comparator and the accepted backend's own Compose. There IS source-version skew:
public v4.11.0 adds migrations `0032_app_migrations.sql` and
`0033_migration_recovery.sql` beyond the pinned backend. This makes upstream-first
schema/application compatibility a hypothesis, **not an established failure**.
The local Compose's TIPI_VERSION=4.5.0 and package.json=4.1.0 are not reliable
measurements of the accepted image's or historical installed base's release.

## Hypotheses and discriminating evidence (no guessed fix)

- **H1 DB never ready / exited / OOM**: gate 88, DB health exits, failing streak,
  process exit, OOM flag and restart chronology. `pg_isready` cannot reveal full
  DB errors; do not export its arbitrary output.
- **H2 manager process startup failure**: manager restarting/exited, curl connect
  exit 7 or timeout 28; bounded source-supported errno/class codes only can
  distinguish environment validation, missing cache path, permission or DB errors.
- **H3 manager queue dependency not ready**: manager running, curl HTTP failure
  exit 22 / fixed probe HTTP 503, queue lifecycle and AMQP-related code class.
  Backend queue initializer uses a 30s connection wait and reconnect attempts.
- **H4 upstream-first / older-backend schema skew**: a finite DB SQLSTATE class
  and migration-source comparison may discriminate. Newer schema alone does not
  prove a downgrade failure. Never reverse migrations or alter saved DB payloads.
- **H5 transient resource/start timing**: health schedule and exact creation/start
  chronology distinguish this from a permanent startup fault; never extend the
  window or retry until green to prove it.

Unknown output remains unknown. No raw `.env`, generated passwords, owner URLs,
headers, tokens, log lines or Health.Log.Output may reach host/context.

## One-run capture contract — UNIMPLEMENTED, NOT ADMISSIBLE

Before implementation/admission: restore enough capacity without deleting any
retained/unowned artifacts or lowering reserve/pending; establish a source-bound
historical base/config/image identity by an explicitly authorized route. The
finished 610 read-only classifier campaign stays closed. If identity cannot be
established within scope, report that exact wall rather than substituting latest.

Then the parent may review a NEW immutable QA source export, exact job.json and
source manifest for **one** fresh thin pair over original 851. No 610 ordinary
boot, replay/storage migration, installer retries, backend/Service rebuild or
continuation into app/Service acceptance. The ordinary root helper runs only
through real install.sh / Compose up. Capturing a healthy result ends the same
single diagnostic as NOT_REPRODUCED, never FIXED.

### Identity, incident, projection

- Extend only the existing profile's exact checked `run` call to line 291 with
  known `runuser`, identity, clean-env and `bash <protected-checkout>/install.sh`
  argv. Do not hook arbitrary subprocesses, monkeypatch production run(), replace
  success, or turn failed commands green. On exceptional return retain the
  original install exception/return code FIRST; collector/cleanup errors append
  separately and cannot replace it. On healthy return stop the QA invocation
  before post-line-291 helper actions, without claiming installation acceptance.
- Capture engine ID and guest boot ID plus socket dev/inode before observation.
  Enumerate only four exact project+service filtered Docker API lists (`all=1`),
  one per allowlisted service, with finite response byte/count limits. Reject
  foreign labels, duplicates/replicas, name-only matches and engine/boot/socket
  changes. Bind each observation to an observed full 64-hex container ID and
  image ID; do not use short/friendly names as authority. Refuse a new container
  or image instance after binding rather than silently rebind.
- Match Config.Healthcheck in guest against the sealed actual merged config/image
  command allowlist; never export it. Bind observed Created, StartedAt, FinishedAt,
  restart count and monotonic sample time. Reject lifecycle regression; a changed
  restart count is recorded but cannot erase the first incident. Bound every read
  and the number of samples. Capture at the first `unhealthy` state, freeze that
  incident, and retain a final failure-bound snapshot before any owned cleanup.
  A snapshot taken only after cleanup does not count. If a first transient is
  missed by sampling, say so; do not claim historical completeness.
- Export schema-validated metadata only: service enum, full ID/image ID, lifecycle
  timestamps, restart count, State.Status enum, running/dead/paused/restarting,
  exit code, OOMKilled, signal derived ONLY from known 128+signal exit mapping
  (inferred, not proof), health enum, failing streak, and at most 5 health records
  with Start/End/ExitCode. Docker State.Error and Health.Log.Output stay private.
  Validate bounded types/enums/numbers; do not copy arbitrary object keys.
- Do NOT execute a health command received from container metadata. Only if
  needed, one predeclared source-bound fixed localhost health probe may return
  curl exit code and HTTP status, no body/headers. Avoid any extra mutation/probe
  when the existing recorded health exit/status is sufficient.
- If metadata is insufficient, exactly ONE Docker API logs read for the identified
  owned failing service/full ID: fixed since=bound creation time, tail=64,
  stdout/stderr streams permitted only inside guest memory, aggregate max 65536
  bytes and fixed deadline. Revalidate identity before/after. No follow, arbitrary
  path, journald, another service or unrelated file. Strict finite parser outputs
  only pre-reviewed source/library-backed code enums (e.g. EACCES/ECONNREFUSED,
  SQLSTATE classes), counts and unknown count; NOT substrings or sanitized raw
  lines. It must reject mixed-secret/control/forged-JSON attempts. Unknown codes
  stop as insufficient; broader access requires a new scoped decision.

### Finite timing (must bind the ACTUAL base first)

For the pinned PUBLIC CLI comparator, an upper health-schedule budget using
`start_period + retries * (interval + timeout)` is DB **305s**, manager **310s**.
Sequential two-gate budget plus fixed 60s collection/scheduling allowance is
**675s**. This is a proposed bound, not a historical runtime measurement. It
must be recomputed or refused if the actual sealed timings differ. Do not use
local development YAML's 120 DB retries. At the exact checked install subprocess
entry, record monotonic start and absolute monotonic deadline=start+675s, both
numeric in the guest-private report. All API/probe calls share that deadline;
no nested operation resets it. A fixed 1s sampling ceiling is at most 676 ticks,
four service observations per tick. Stop on first incident/real command exit or
deadline, not on eventual green. Earlier first incident stays frozen even if a
later snapshot improves. If setup time consumes the budget or no instance appears,
record INSUFFICIENT/DEADLINE, quiesce the exact installer process group, then clean
up. A wider prerequisites timeout is separate and cannot extend health time.

### Cleanup, retained proofs, finite collector-only cases

Re-use the exact-ID same-engine cleanup with an explicitly created unrelated
synthetic negative control. Assert it survives owned cleanup, remove only that
known QA control by full ID, and structurally read back absence. Do not enumerate
host Docker or other host projects. Exact guest-instance cleanup may see the
negative control only under its already-reviewed cleanup contract, never as a
health-observation source. Quiesce installer descendants before cleanup. Export
original failure and cleanup failures separately. Preserve input disk hashes/stat
before/after, whole retained inventories, and stop/reap every created VM in finally.
No historical 474-container-absence claim.

New collector-only fixtures, if later implemented/admitted: exact four-service
selection; foreign/duplicate/short-ID refusal; engine/boot/socket/image/container
swap; lifecycle/restart regression; db and manager unhealthy; queue HTTP 503 vs
connect error; freeze first incident; original exception survives collector and
cleanup failure; healthy NOT_REPRODUCED; hard deadline and no second attempt;
5-record/API/log byte caps; no arbitrary health command; one-tail-only identity
recheck; secret/control/nested JSON rejection; unknown output stops; unowned
negative survives and exact cleanup absence. No existing 105 tests are rerun.

## Capacity refusal: complete once-only ledger

Original remaining after 851: 66220945408. Previously credited allocations once:
474 2949689344, signed 207671296, 0ae 16367616, 0513 53530624,
e5 508805120, 610 2473054208; total **6209118208**.
Thus canonical pending remains **60011827200**, not reset or credited again.
Retained bytes are not free capacity. No additional credit is taken for 851,
archives, prior stages or this source-only local packet.

Native forum statvfs observed **277823594496** available bytes:

- Reserve 217179869184 + unchanged pending 60011827200 = **277191696384**.
- Headroom over this floor = **631898112**.
- Prior RO wrapper floor 277258805248 leaves **564789248**, not a fresh-install budget.
- Even prior actual 610 allocation + 64MiB telemetry would require **279731859456**
  available, a **1908264960** shortfall. Prior actual is NOT a guaranteed new peak.
- Conservative all-in new-stage allocation reservation, reusing the existing thin
  runner's declared stage ceiling: **8589934592** bytes, including
  source/COW/logs/instrumentation (do not add those again). Reserve+pending+new
  reservation requires **285781630976** available; shortfall **7958036480**.
  This is not a measured or proven new peak: any later collector/runner must
  demonstrate enforcement before native admission, rather than rely on the old
  runner's between-command checks or claim the run will necessarily use 8GiB.

The same original 60GB contains future campaign budget; it cannot simultaneously
be treated as still protected pending work AND spent by this additional diagnostic.
Do not lower it, double-credit 610, or charge the old actual allocation again as a
new retained stage. Until the full remaining campaign and additional diagnostic
peak fit together, **stop native preparation**. No native stage or runnable job is
provided. Source-only static audit success is not native admission or acceptance.
