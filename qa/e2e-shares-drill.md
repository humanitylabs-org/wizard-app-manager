# Disposable #79/#80 share drill

`qa/e2e-shares-drill.ts` uses the production compose builder, `WizardStorage`
(propose/change/prepare/migrate/remove), `BackupManager`, `ArchiveService`,
`FilesystemService` and `AppFilesManager.deleteAppDataDir`. It does not import a
live configuration or call the app lifecycle worker. The only filesystem adapter
changes the appliance's fixed allowed roots and temp-directory location to the
throwaway tree; its copy/delete methods remain production methods.

## Prepare and run

Stage **tracked files from the pushed commit**, not an uncommitted worktree.
Install with `PATH=/opt/homebrew/bin:$PATH bun install --frozen-lockfile`, then
run `(cd packages/common && PATH=/opt/homebrew/bin:$PATH bun run build)` (a small
TypeScript-only workspace build: fresh installs have no exported `dist/schemas`).
Use an explicit Colima `DOCKER_HOST` (do not change the user's Docker context).
All host artifacts go in an explicitly selected dedicated directory whose last
component is `e2e-shares`, supplied below as `$WIZARD_DRILL_ROOT`.

```sh
bun --tsconfig-override ./qa/tsconfig.e2e-shares.json \
  qa/e2e-shares-drill.ts --dry-run --root "$WIZARD_DRILL_ROOT"

# Only the authorized parent/heavy worker runs this, after staging:
WIZARD_E2E_SHARES=1 WIZARD_DRILL_REVISION="$PUSHED_SHA" \
  DOCKER_HOST="$COLIMA_SOCKET" \
  bun --tsconfig-override ./qa/tsconfig.e2e-shares.json \
  qa/e2e-shares-drill.ts --root "$WIZARD_DRILL_ROOT"
```

Expected duration: **2–5 minutes** with a warm image cache, up to **10 minutes**
with the first pull. The dry run performs production compilation and grant file
operations only, in a disposable directory. `PlanningStorage` deliberately skips
migration; it neither starts Docker nor fabricates Docker results. It prints the
plan, actual compiled composes and their safe runtime projections. The actual
compiled composes are for inspection only: **do not start them directly**.

Local, Docker-free checks:

```sh
bun test --tsconfig-override ./qa/tsconfig.e2e-shares.json ./qa/e2e-shares-drill.test.ts
bun node_modules/typescript/bin/tsc -p qa/tsconfig.e2e-shares.json --noEmit
(cd packages/backend && bun run vitest run \
  src/modules/docker/__tests__/wizard-storage.test.ts \
  src/modules/backups/__tests__/backup.manager.security.test.ts --maxWorkers=1)
```

## Safety and evidence boundaries

- Companion recipes are exact committed snapshots with SHA-256 provenance in
  `qa/fixtures/e2e-shares/provenance.json`; every run verifies the bytes.
- Every run has a random `wz-shares-*` prefix. Helpers and projected services have
  names under that prefix, `--network none`/`network_mode: none`, no published
  ports, and the production digest-pinned Alpine image. No named app volumes
  remain in the generated composes; compiled mount arrays are retained verbatim.
- `WizardStorage` accepts an optional **third constructor argument** selecting
  the operational legacy volume name. The default is unchanged. The recipe
  compatibility exception still recognizes only the literal
  `wizard-ai-accounts-claude`. The drill always injects a disposable name, so it
  never reads, mounts or writes the real fixed-name login, whether present or
  not. Both modern migration and legacy install-autoapproval use this selector.
  The legacy fixture still uses the real fixed-name recipe syntax, exercising
  the production compatibility shim and rewrite, not a reimplementation.
- The seeded login is explicitly fake, including `.credentials.json`,
  `.claude.json`, a nested file and `.oauth_refresh.lock`. No application images,
  real accounts, refresh requests or owner files are used.
- **macOS virtiofs remaps ownership.** The app-data migration and host visibility
  use the real host run directory. Numeric uid/gid/mode preservation is proved
  separately by a second invocation of production `migrate()` into a disposable
  Docker scratch volume's Linux mountpoint. No arbitrary Linux host directory
  is created or removed. The numeric check requires `1000:1000:600` for the fake
  credential and `1000:1000:700` for the nested directory.
- Runtime services keep the compiled numeric user (Claude `1000:1000`; Hermes
  defaults to root as in its recipe). Cross-container file/lock probes explicitly
  use `docker exec --user 0:0` and `DAC_OVERRIDE` to avoid mistaking virtiofs uid
  mapping for storage failure. This proves **mount identity, bidirectional writes
  and atomic lock visibility**, not non-root application access or a real
  Claude/Hermes token-refresh upgrade. The receipt records that boundary. The
  owner-native Linux-box non-root login rehearsal remains separate.
- Writers stop before production backups. Provider archive bytes must equal the
  original source hash; the consumer archive must omit the credential path.
  Revoke is followed by force-recreation and inspection of the running container,
  not merely a source-YAML assertion or Docker restart.
- Uninstall uses production grant removal and the production consumer data-removal
  method, not a whole live lifecycle worker. Provider bytes must remain intact.
- `finally` tears down only exact prefixed objects, removes fake data and
  archives, and verifies zero prefixed containers/volumes remain. It retains
  safe projected composes plus `receipt.json` with check booleans, hashes and
  error/cleanup status. It never treats incomplete cleanup as success. A killed
  process/SIGKILL or failed daemon can defeat `finally`; inspect the recorded
  prefix and remove only its labelled objects when recovering that run.

No full Docker rehearsal is claimed by writing or dry-running this script. Only
an actual full-run receipt with `passed: true` constitutes Docker evidence.
