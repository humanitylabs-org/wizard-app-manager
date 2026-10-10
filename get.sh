#!/usr/bin/env bash
# One-line Wizard OS install on a fresh Linux box:
#   curl -fsSL https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/get.sh | bash
# Clones (or updates) the Wizard App Manager into ~/wizard-app-manager and runs install.sh, which sets up Runtipi
# with the Wizard image, the `wam` command, and the default app in every parking spot (agent, shell, files, ...).
set -euo pipefail
DIR="${WAM_DIR:-$HOME/wizard-app-manager}"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only -q; else git clone -q https://github.com/humanitylabs-org/wizard-app-manager "$DIR"; fi
exec bash "$DIR/install.sh"
