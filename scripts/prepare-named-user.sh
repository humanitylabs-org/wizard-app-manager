#!/usr/bin/env bash
# One approved administrator elevation prepares the host; the owner never needs NOPASSWD sudo.
# Called by get.sh as root, before re-entering the ordinary installer as the selected Hermes owner.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Host preparation requires an administrator.' >&2; exit 2; }
owner="${1:?selected Hermes owner required}"
if [ "$owner" = root ] || ! id "$owner" >/dev/null 2>&1; then
  echo 'Host preparation requires an existing named owner, not root.' >&2
  exit 2
fi
home="$(getent passwd "$owner" | cut -d: -f6)"
[ -n "$home" ] && [ -d "$home" ] || { echo 'Selected owner has no home directory.' >&2; exit 2; }
uid="$(id -u "$owner")"; gid="$(id -g "$owner")"
base="$home/runtipi"
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
  wizard_fetch https://setup.runtipi.io "$work/setup.sh"
  retry_curlrc "$work"   # the setup script downloads runtipi-cli itself, with a plain curl
  (cd "$home" && CURL_HOME="$work" bash "$work/setup.sh" --env-file "$work/base.env")
  chown "$uid:$gid" "$base"
  find "$base" -mindepth 1 -maxdepth 1 ! -name data -exec chown -R "$uid:$gid" {} +
  rm -rf "$work"; trap - EXIT
fi
# Membership is activated by get.sh's fresh login. This is explicitly the rootful Docker administration
# group, not a sudo grant; no sudoers file or cached authentication timestamp is created.
echo "Administrator approval grants $owner rootful Docker group access (administrator-equivalent); it does not grant passwordless sudo."
usermod -aG docker "$owner"
loginctl enable-linger "$owner"
# Tailscale's host package/daemon needs root. Owner login is still a genuine owner action.
# The ordinary installer will reuse the running daemon without a second sudo invocation.
if [ "${WIZARD_PRIVATE_ACCESS:-tailscale}" = tailscale ]; then
  if ! command -v tailscale >/dev/null; then
    script="$(mktemp)"; trap 'rm -f "$script"' EXIT
    wizard_fetch https://tailscale.com/install.sh "$script"
    sh "$script"; rm -f "$script"; trap - EXIT
  fi
  systemctl enable --now tailscaled
  # Do not silently replace an existing designated operator. A conflicting administrator is a real gate.
  operator="$(tailscale debug prefs | python3 -c 'import json,sys; print(json.load(sys.stdin).get("OperatorUser") or "")')"
  if [ -n "$operator" ] && [ "$operator" != "$owner" ]; then
    echo "Tailscale is managed by $operator; an administrator must approve operator access for $owner before retrying." >&2
    exit 2
  fi
  tailscale set --operator="$owner"
fi
printf 'Administrator host preparation complete for %s; no passwordless sudo was granted.\n' "$owner"
