#!/usr/bin/env bash
# One approved administrator elevation prepares the host; the owner never needs NOPASSWD sudo.
# Called by get.sh as root, before re-entering the ordinary installer as the selected Hermes owner.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Host preparation requires an administrator.' >&2; exit 2; }
owner="${1:?selected Hermes owner required}"
reassign_from=""
if [ "${2:-}" = --reassign-from ]; then
  [ "$#" -eq 3 ] && [ "$owner" = wizard-system ] && id "$3" >/dev/null 2>&1 || { echo 'Explicit operator reassignment requires wizard-system and the prior named user.' >&2; exit 2; }
  reassign_from="$3"
elif [ "$#" -ne 1 ]; then
  echo 'Unknown host preparation arguments.' >&2; exit 2
fi
if [ "$owner" = root ] || ! id "$owner" >/dev/null 2>&1; then
  echo 'Host preparation requires an existing named owner, not root.' >&2
  exit 2
fi
home="$(getent passwd "$owner" | cut -d: -f6)"
[ -n "$home" ] && [ -d "$home" ] || { echo 'Selected owner has no home directory.' >&2; exit 2; }
uid="$(id -u "$owner")"; gid="$(id -g "$owner")"
base="$home/runtipi"
# The privileged base setup is the same upstream installer used by install.sh. Do it ONCE as administrator,
# rather than relying on the selected account being able to run sudo from an unattended process.
if [ ! -f "$base/.env" ]; then
  install -d -o "$uid" -g "$gid" "$base/user-config"
  if [ ! -f "$base/user-config/tipi-compose.yml" ]; then
    printf '%s\n' 'services:' '  runtipi-reverse-proxy:' '    ports: !override' \
      '      - 127.0.0.1:8480:80' '      - 127.0.0.1:8443:443' > "$base/user-config/tipi-compose.yml"
  fi
  work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
  printf 'NGINX_PORT=8480\nNGINX_PORT_SSL=8443\nINTERNAL_IP=127.0.0.1\n' > "$work/base.env"
  curl -fsSL https://setup.runtipi.io -o "$work/setup.sh"
  (cd "$home" && bash "$work/setup.sh" --env-file "$work/base.env")
  chown "$uid:$gid" "$base"
  find "$base" -mindepth 1 -maxdepth 1 ! -name data -exec chown -R "$uid:$gid" {} +
  rm -rf "$work"; trap - EXIT
fi
# Membership is activated by get.sh's fresh login. This is explicitly the rootful Docker administration
# group, not a sudo grant; no sudoers file or cached authentication timestamp is created.
echo "Administrator approval grants $owner rootful Docker group access (administrator-equivalent); it does not grant passwordless sudo."
usermod -aG docker "$owner"
loginctl enable-linger "$owner"
# One security contract in both private-access modes: custody needs the official
# package and a responsive daemon for authoritative persisted Serve inspection.
# `none` disables private APP access, not this security prerequisite. Installing
# or starting the daemon does not enroll, log in, publish, or grant an operator.
# Reuse the existing official dependency installer, never select the tailscale
# access provider merely to obtain its package (that path can invoke `up`).
if ! command -v tailscale >/dev/null; then
  script="$(mktemp)"; trap 'rm -f "$script"' EXIT
  curl -fsSL https://tailscale.com/install.sh -o "$script"
  sh "$script"; rm -f "$script"; trap - EXIT
fi
systemctl enable --now tailscaled
# Only an explicitly selected private-access provider grants an operator.
# The custody installer still independently refuses failed/unknown inspection.
if [ "${WIZARD_PRIVATE_ACCESS:-tailscale}" = tailscale ]; then
  # Do not silently replace an existing designated operator. A conflicting administrator is a real gate.
  operator="$(tailscale debug prefs | python3 -c 'import json,sys; print(json.load(sys.stdin).get("OperatorUser") or "")')"
  if [ -n "$operator" ] && [ "$operator" != "$owner" ] && [ "$operator" != "$reassign_from" ]; then
    echo "Tailscale is managed by $operator; an administrator must approve operator access for $owner before retrying." >&2
    exit 2
  fi
  # Changing only OperatorUser preserves Serve, DNS, routes and node identity.
  before_serve="$(tailscale serve status --json)"
  tailscale set --operator="$owner"
  actual_operator="$(tailscale debug prefs | python3 -c 'import json,sys; print(json.load(sys.stdin).get("OperatorUser") or "")')"
  [ "$actual_operator" = "$owner" ] || { echo 'Tailscale operator handoff did not take effect.' >&2; exit 2; }
  [ "$(tailscale serve status --json)" = "$before_serve" ] || { echo 'Published links changed during operator reassignment; stop and restore the saved configuration.' >&2; exit 2; }
fi
printf 'Administrator host preparation complete for %s; no passwordless sudo was granted.\n' "$owner"
