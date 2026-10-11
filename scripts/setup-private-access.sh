#!/usr/bin/env bash
# Wizard OS host integration: Tailscale is the default private-access provider.
# This is NOT an ordinary store container: the daemon belongs on the host.
# Does not expose ports, enable Funnel/SSH, advertise routes, or change an existing tailnet.
set -euo pipefail

provider="${WIZARD_PRIVATE_ACCESS:-tailscale}"
case "$provider" in
  cloudflare|none)
    echo "Private access: $provider selected; leaving Tailscale unchanged."
    exit 0
    ;;
  tailscale) ;;
  *) echo "Unknown WIZARD_PRIVATE_ACCESS=$provider (use tailscale, cloudflare or none)" >&2; exit 1 ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  root() { "$@"; }
else
  root() { sudo "$@"; }
fi

if ! command -v tailscale >/dev/null; then
  echo "Installing Tailscale (private access)"
  script="$(mktemp)"
  trap 'rm -f "$script"' EXIT
  curl -fsSL https://tailscale.com/install.sh -o "$script"
  root sh "$script"
  rm -f "$script"
  trap - EXIT
fi
root systemctl enable --now tailscaled

state() {
  root tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState", "Unknown"))'
}
current="$(state)"
if [ "$current" = "NeedsLogin" ]; then
  echo "Tailscale is installed. Open the sign-in link below to connect this box to YOUR tailnet."
  echo "Use the same Tailscale account on your phone or laptop. No shared team account is used."
  # A login link is printed even without a terminal; unattended VM builds remain unpaired.
  # A bounded timeout leaves the installer usable when the owner chooses to sign in later.
  wait_for_login=1s
  if (: </dev/tty) 2>/dev/null; then wait_for_login=120s; fi
  rc=0
  root tailscale up --timeout="$wait_for_login" || rc=$?
  current="$(state)"
  if [ "$current" != "Running" ] && [ "$current" != "NeedsLogin" ]; then
    echo "Tailscale could not connect (state $current, exit $rc). Existing networking was not reset." >&2
    exit 1
  fi
elif [ "$current" != "Running" ]; then
  echo "Tailscale already has state $current; leaving its settings alone. Run sudo tailscale up when ready."
fi

home="${WIZARD_OS_HOME:-$HOME/.wizard-os}"
mkdir -p "$home"
# Store only the provider and connection state, never credentials. Atomic so readers see a complete record.
python3 - "$home/private-access.json" "$current" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
record = {"provider": "tailscale", "runtime": "host", "state": sys.argv[2], "installed": True}
fd, name = tempfile.mkstemp(prefix=".private-access-", dir=path.parent)
try:
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.replace(name, path)
finally:
    if os.path.exists(name): os.unlink(name)
PY
if [ "$current" = "Running" ]; then
  echo "Private access: Tailscale connected (existing settings preserved)."
else
  echo "Private access: Tailscale preinstalled, not connected yet. Later: sudo tailscale up"
fi
