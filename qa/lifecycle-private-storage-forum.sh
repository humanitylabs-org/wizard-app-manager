#!/usr/bin/env bash
# Run only via the forum durable worker, in an isolated staged source tree.
set -euo pipefail
: "${WIZARD_LIFECYCLE_SYNTHETIC:?explicit opt-in required}"
: "${WIZARD_QA_DOCKER_CONTEXT:?exact daemon context required}"
: "${TMPDIR:?task-local forum scratch required}"
[[ "$WIZARD_LIFECYCLE_SYNTHETIC" = 1 && "$TMPDIR" = /srv/work/* ]]
[[ "$(uname -m)" = x86_64 ]]
node -e 'if (+process.versions.node.split(".")[0] < 22) process.exit(1)'
command -v bun
mkdir -p "$TMPDIR" qa-results
node --version > qa-results/tool-versions.log
bun --version >> qa-results/tool-versions.log
bun install --frozen-lockfile > qa-results/install.log 2>&1
(cd packages/common && bun run build) > qa-results/common-build.log 2>&1
status=0
step() {
  local name="$1"; shift
  if "$@" > "qa-results/$name.log" 2>&1; then
    printf '%s\t0\n' "$name" | tee -a qa-results/steps.tsv
  else
    local code=$?; printf '%s\t%s\n' "$name" "$code" | tee -a qa-results/steps.tsv
    status=1
  fi
}
: > qa-results/steps.tsv
step lifecycle-source node --experimental-strip-types --test tests/lifecycle-private-storage.test.mjs
step existing-node node --test tests/runtipi-open.test.mjs tests/connections-map.test.mjs
step backend-types bash -c 'cd packages/backend && bun run tsc'
step backend-contracts bash -c 'cd packages/backend && bun x --no-install vitest run --maxWorkers=1 src/modules/apps/__tests__/app-files-manager.private.test.ts src/modules/apps/__tests__/app-files-manager.security.test.ts src/modules/app-lifecycle/commands/install-app-command.test.ts src/modules/app-lifecycle/commands/start-app-command.test.ts src/modules/app-lifecycle/commands/update-app-command.test.ts src/modules/docker/__tests__/wizard-storage.test.ts'
step synthetic-docker node --experimental-strip-types qa/lifecycle-private-storage-synthetic.mjs
exit "$status"
