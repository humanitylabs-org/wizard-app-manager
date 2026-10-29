#!/usr/bin/env bash
# Focused audit-repair checks; disposable containers/state, never a live install.
# Usage: focused-checks.sh EVIDENCE_DIR WAM_EXPORT WAS_EXPORT WUX_EXPORT PRIVATE_FIXTURES REVISION RUN_ID
# Run only through the authorized remote worker. Exports are hash-verified by its private wrapper.
set -euo pipefail
[[ $# == 7 ]] || { printf 'expected seven arguments\n' >&2; exit 2; }
EVIDENCE=$1 WAM=$2 WAS=$3 WUX=$4 PRIVATE=$5 REVISION=$6 RUN_ID=$7
[[ $REVISION =~ ^[0-9a-f]{40}$ && $RUN_ID =~ ^[a-z0-9-]+$ ]] || exit 2
[[ ! -e $EVIDENCE ]] || { printf 'evidence already exists\n' >&2; exit 2; }
mkdir -m 700 -p "$EVIDENCE"
WORK="$EVIDENCE/work"; mkdir -m 700 "$WORK"
fail=0
cleanup() {
  # Only containers bearing this run's exact label; never prune shared state.
  local ids
  ids=$(docker ps -aq --filter "label=wizard.privsep.security=$RUN_ID") || return 1
  if [[ -n $ids ]]; then docker rm -f $ids; fi
  [[ -z $(docker ps -aq --filter "label=wizard.privsep.security=$RUN_ID") ]]
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
step() {
  local name=$1 rc; shift
  printf '== %s\n' "$name" | tee -a "$EVIDENCE/summary.txt"
  # Capture the command's own status, not tee/printf's or the last assertion's.
  if "$@" >"$EVIDENCE/$name.log" 2>&1; then rc=0; else rc=$?; fail=1; fi
  printf '%s\t%s\n' "$name" "$rc" >>"$EVIDENCE/exits.tsv"
  printf '%s %s (exit %s)\n' "$([[ $rc == 0 ]] && printf PASS || printf FAIL)" "$name" "$rc" | tee -a "$EVIDENCE/summary.txt"
  STEP_RC=$rc
}
skip_dependency() {
  local name=$1 reason=$2
  # 77 is an explicit dependency skip, never a passing exit.
  printf '%s\t77\n' "$name" >>"$EVIDENCE/exits.tsv"
  printf '%s\t%s\n' "$name" "$reason" >>"$EVIDENCE/skipped.tsv"
  printf '%s\n' "$reason" >"$EVIDENCE/$name.log"
  printf 'SKIPPED %s: %s\n' "$name" "$reason" | tee -a "$EVIDENCE/summary.txt"
  fail=1
}
dock() {
  local src=$1; shift
  timeout --signal=TERM --kill-after=30 1800 docker run --rm \
    --label "wizard.privsep.security=$RUN_ID" --memory=6g --cpus=4 \
    -v "$src":/src -w /src \
    -e HOME=/case/home -e HERMES_HOME=/case/hermes -e WIZARD_OS_HOME=/case/wizard \
    -e TMPDIR=/case/tmp -e XDG_CONFIG_HOME=/case/config -e XDG_CACHE_HOME=/case/cache \
    -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/src \
    --tmpfs /case:rw,exec,size=2g,mode=0700 "$@"
}
# Preserve immutable source; package managers and tests write only these throwaway copies.
for name in wam was wux; do
  case $name in wam) source=$WAM;; was) source=$WAS;; wux) source=$WUX;; esac
  cp -a "$source" "$WORK/$name"; chmod -R u+w "$WORK/$name"
  # Tracked-file guards require Git. This is export bookkeeping, not candidate provenance.
  git -C "$WORK/$name" init -q
  git -C "$WORK/$name" add -f -A
  git -C "$WORK/$name" -c user.name=Acceptance -c user.email=acceptance@example.invalid commit -qm 'Synthetic export inventory'
done

mkdir -m 700 "$WORK/compose-proof"
step backend-typecheck-and-tests dock "$WORK/wam" \
  -v "$WORK/compose-proof":/compose-proof -e WIZARD_COMPOSE_PROOF_DIR=/compose-proof \
  -e WIZARD_COMPOSE_PROOF_RUN_ID="$RUN_ID" node:22-bookworm sh -ec '
  mkdir -m 700 -p "$HOME" "$HERMES_HOME" "$WIZARD_OS_HOME" "$TMPDIR"
  # The cross-process authority regression requires both real lock consumers.
  apt-get update
  apt-get install -y --no-install-recommends python3 util-linux
  command -v python3
  command -v flock
  python3 -c "import fcntl"
  npm install --global --no-audit --no-fund bun@1.3.0
  timeout 900 bun install --frozen-lockfile
  bun run --cwd packages/common build
  cd packages/backend
  # Resolve the workspace-local pinned compiler/test packages via normal scripts.
  bun run tsc
  bun run test --maxWorkers=2 src/modules/docker/__tests__/wizard-host-guard.test.ts \
    src/modules/docker/__tests__/compose-consumption.test.ts src/modules/docker/__tests__/host-policy-lock.test.ts \
    src/modules/apps/__tests__/leaked-env-repair.test.ts src/modules/apps/__tests__/app.helpers.test.ts \
    src/modules/docker/__tests__/docker.service.test.ts src/modules/docker/__tests__/wizard-storage.test.ts
  cd /src
  node_modules/.bin/biome check packages/backend/src/modules/docker/wizard-host-guard.ts \
    packages/backend/src/modules/apps/leaked-env-repair.ts packages/backend/src/modules/apps/app.helpers.ts \
    packages/backend/src/modules/apps/app-files-manager.ts packages/backend/src/modules/docker/docker.service.ts \
    packages/backend/src/core/filesystem/filesystem.service.ts packages/backend/src/app.service.ts'

# Host TLS/DNS works while the previous bridged build could not resolve the
# GitHub release redirect host. Limit the workaround to this one build's hosts;
# do not change daemon DNS, host networking, resolv.conf or provider settings.
step build-network-and-compose-prefetch timeout --signal=TERM --kill-after=30 240 \
  python3 "$PRIVATE/compose_runtime.py" prepare "$WORK" "$EVIDENCE"
NETWORK_RC=$STEP_RC
if [[ $NETWORK_RC == 0 && -s $WORK/compose-proof/model.json ]]; then
  step compose-dollar-runtime timeout --signal=TERM --kill-after=30 180 \
    python3 "$PRIVATE/compose_runtime.py" run "$WORK" "$EVIDENCE" "$RUN_ID"
else
  skip_dependency compose-dollar-runtime 'verified Compose binary or generated validated-stdin proof unavailable'
fi

# Build pristine candidate bytes, not the test-mutated tree, and never reuse latest.
IMAGE="wam-privsep-security:$REVISION"
if [[ $NETWORK_RC == 0 ]]; then
RELEASE_ASSET_IP=$(<"$WORK/release-assets.ip")
step backend-image-build timeout --signal=TERM --kill-after=30 3600 docker build \
  --add-host "release-assets.githubusercontent.com:$RELEASE_ASSET_IP" \
  --build-arg LOCAL=true --build-arg TIPI_VERSION=4.5.0 \
  --label "org.opencontainers.image.revision=$REVISION" \
  --iidfile "$EVIDENCE/backend-image.id" -t "$IMAGE" "$WAM"
else
  skip_dependency backend-image-build 'build-only host mapping could not be verified'
  STEP_RC=77
fi
if [[ $STEP_RC == 0 && -s $EVIDENCE/backend-image.id ]]; then
  IMAGE_ID=$(<"$EVIDENCE/backend-image.id")
  step backend-image-inspect docker image inspect "$IMAGE_ID"
  # This only reads the built bundle in a network-less container; no service or host socket.
  step backend-image-bundle timeout 60 docker run --rm --network=none \
    --label "wizard.privsep.security=$RUN_ID" --entrypoint sh "$IMAGE_ID" -ec \
    'test -s /app/main.mjs; sha256sum /app/main.mjs; grep -q wizard-host-approvals /app/main.mjs'
  step backend-image-retain bash -o pipefail -c 'docker image save "$1" | gzip -1 > "$2"' \
    _ "$IMAGE_ID" "$EVIDENCE/backend-image.tar.gz"
else
  for name in backend-image-inspect backend-image-bundle backend-image-retain; do
    skip_dependency "$name" 'candidate image build failed or image ID missing; no older image substituted'
  done
fi

step wam-python-focused dock "$WORK/wam" ghcr.io/astral-sh/uv:python3.12-bookworm sh -ec '
  mkdir -m 700 -p "$HOME" "$HERMES_HOME" "$WIZARD_OS_HOME" "$TMPDIR"
  python -c "import os, stat; assert stat.S_IMODE(os.stat(\"/case\").st_mode) == 0o700; assert stat.S_IMODE(os.stat(os.environ[\"TMPDIR\"]).st_mode) == 0o700"
  uv run --no-project --with pytest --with pyyaml python -m pytest -q -p no:anyio \
    tests/test_credential_rotation.py tests/test_migration_authority.py \
    tests/test_privilege_migration.py::test_inventory_rejects_root_before_authority_or_state_changes \
    tests/test_privilege_migration.py::test_inventory_is_metadata_only_and_captures_original_authority \
    tests/test_privilege_migration.py::test_inventory_policy_refusal_prevents_quiesce_or_snapshot'

if [[ -f $PRIVATE/prior-evidence.json ]]; then
  # The private wrapper hash-binds prior manifests, exact unchanged heads, passed
  # exits and complete logs. These are inherited, not fresh suite executions.
  step was-proxy-media cp "$PRIVATE/prior-was-proxy-media.log" "$EVIDENCE/inherited-was-proxy-media.log"
  step wux-privileges cp "$PRIVATE/prior-wux-privileges.log" "$EVIDENCE/inherited-wux-privileges.log"
else
step was-proxy-media dock "$WORK/was" node:22-bookworm sh -ec '
  mkdir -p "$HOME" "$HERMES_HOME" "$WIZARD_OS_HOME" "$TMPDIR"
  npm ci --no-audit --no-fund
  node_modules/.bin/vitest run --maxWorkers=2 tests/proxy-inert-media.test.ts tests/proxy-queue.test.ts tests/portable-paths.test.ts'

step wux-privileges dock "$WORK/wux" node:22-bookworm sh -ec '
  mkdir -p "$HOME" "$HERMES_HOME" "$WIZARD_OS_HOME" "$TMPDIR"
  node --test tests/privileges.test.mjs tests/portable-paths.test.mjs'
fi

# Hostile proof pages stay in the private staging area, never in the public repository.
step approval-browser timeout --signal=TERM --kill-after=30 1200 docker run --rm --add-host box.test:127.0.0.1 \
  --label "wizard.privsep.security=$RUN_ID" --memory=3g --cpus=2 --shm-size=1g \
  -v "$WAM":/src:ro -v "$PRIVATE":/private:ro -v "$EVIDENCE":/evidence \
  -w /src -e HOME=/case/home -e HERMES_HOME=/case/hermes -e WIZARD_OS_HOME=/case/wizard \
  -e TMPDIR=/case/tmp -e PYTHONDONTWRITEBYTECODE=1 --tmpfs /case:rw,exec,size=1g,mode=0700 \
  mcr.microsoft.com/playwright/python:v1.56.0-noble@sha256:a7f6cf3ae520c9d670ad956572c13747ed5abdbba5123a01526f873ed1662528 sh -ec '
    mkdir -m 700 -p "$HOME" "$HERMES_HOME" "$WIZARD_OS_HOME" "$TMPDIR"
    # Official image installs virtualenv; distro ensurepip/venv is not guaranteed.
    python -m virtualenv /case/venv
    /case/venv/bin/pip install --disable-pip-version-check playwright==1.56.0 pyyaml
    /case/venv/bin/python /private/approval_harness.py /src /evidence /private/hostile.html'

step container-cleanup cleanup
printf 'overall: %s\n' "$([[ $fail == 0 ]] && printf PASS || printf FAIL)" | tee -a "$EVIDENCE/summary.txt"
exit "$fail"
