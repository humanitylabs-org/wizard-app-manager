#!/usr/bin/env bash
# Sourced only before a fresh Runtipi base install. Leave existing Docker alone.
# Runtipi's upstream setup concatenates IDs from /etc/*release*; Debian cloud
# images also have cloud-release (ID=genericcloud), so its Docker dispatch fails.
# Docker's documented convenience installer reads the standard os-release file.
if ! command -v docker >/dev/null 2>&1 && [ -r /etc/os-release ] &&
   [ "$(. /etc/os-release; printf '%s' "${ID:-}")" = debian ]; then
  (
    set -euo pipefail
    DOCKER_SETUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wizard-base-docker.XXXXXXXX")"
    trap 'rm -rf "$DOCKER_SETUP_DIR"' EXIT
    echo "Installing Docker for the Debian base using Docker's official installer."
    curl -fsSL https://get.docker.com -o "$DOCKER_SETUP_DIR/install.sh"
    sudo sh "$DOCKER_SETUP_DIR/install.sh"
  )
fi
