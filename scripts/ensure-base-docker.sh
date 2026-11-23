#!/usr/bin/env bash
# Sourced only before a fresh Runtipi base install. Leave existing Docker alone.
# Runtipi's upstream setup concatenates IDs from /etc/*release*; Debian cloud
# images also have cloud-release (ID=genericcloud), so its Docker dispatch fails.
# Docker's documented convenience installer reads the standard os-release file.
# wizard_fetch <url> <file>: download to a file, and try again when the connection drops (wizard-app-manager#868:
# one TLS reset, curl exit 35, failed a fresh install outright). Always into a file, never `curl | sh`: a retry
# rewrites the file from the start, while a retried pipe would feed the script twice. Same text in every installer.
wizard_fetch() {
  local n="${WIZARD_FETCH_TRIES:-5}" i=1 rc
  while :; do
    rc=0; curl -fsSL "$1" -o "$2" || rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$i" -ge "$n" ] && return "$rc"
    echo "Download of $1 failed (curl exit $rc, try $i of $n); trying again in $((i * 3))s." >&2
    sleep "$((i * 3))"; i=$((i + 1))
  done
}
# retry_curlrc <dir>: a curl config in <dir> (run a third-party installer with CURL_HOME=<dir>) so that its own
# downloads also try again after a dropped connection. retry-all-errors only where this curl knows it (7.71+).
retry_curlrc() {
  { printf 'retry = 5\nretry-delay = 3\nretry-connrefused\n'
    case "$(curl --help all 2>/dev/null || true)" in *--retry-all-errors*) echo retry-all-errors ;; esac
  } > "$1/.curlrc"
}
if ! command -v docker >/dev/null 2>&1 && [ -r /etc/os-release ] &&
   [ "$(. /etc/os-release; printf '%s' "${ID:-}")" = debian ]; then
  (
    set -euo pipefail
    DOCKER_SETUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wizard-base-docker.XXXXXXXX")"
    trap 'rm -rf "$DOCKER_SETUP_DIR"' EXIT
    echo "Installing Docker for the Debian base using Docker's official installer."
    wizard_fetch https://get.docker.com "$DOCKER_SETUP_DIR/install.sh"
    retry_curlrc "$DOCKER_SETUP_DIR"
    sudo env CURL_HOME="$DOCKER_SETUP_DIR" sh "$DOCKER_SETUP_DIR/install.sh"
  )
fi
