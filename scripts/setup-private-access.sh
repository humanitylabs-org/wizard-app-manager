#!/usr/bin/env bash
# Wizard OS host integration: Tailscale is the default private-access provider.
# This is NOT an ordinary store container: the daemon belongs on the host.
# Does not expose ports, enable Funnel/SSH, advertise routes, or change an existing tailnet.
#
# Every state:
#   not installed        -> the official https://tailscale.com/install.sh, then as "logged out"
#   logged out / never   -> `tailscale up` in the background (no terminal needed); its sign-in link is printed as the
#                           one owner action ("[wizard-os] owner-action: ..." for the detached status), kept current,
#                           and waited for (WIZARD_TAILSCALE_WAIT seconds: 1800 for a detached or terminal install,
#                           1 otherwise, so a plain `| bash` from an agent with a command timeout never hangs here)
#   connected            -> reused as is: never re-authenticated, no settings changed
#   stopped by the owner -> left alone
# Once connected, MagicDNS and HTTPS certificates (needed for the private https:// links) are checked; when off, the
# exact admin link is given and the install finishes box-only. No auth keys are used or printed.
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

ADMIN_DNS="https://login.tailscale.com/admin/dns"
# status_field <field>: BackendState | AuthURL | serve (MagicDNS and HTTPS certificates: on|magicdns-off|https-off)
status_field() {
  { root tailscale status --json 2>/dev/null || true; } | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except ValueError: d = {}
f = sys.argv[1]
if f == "serve":
    t = d.get("CurrentTailnet") or {}
    print("magicdns-off" if t.get("MagicDNSEnabled") is False else "https-off" if not d.get("CertDomains") else "on")
else:
    print(d.get(f) or ("Unknown" if f == "BackendState" else ""))' "$1"
}
state() { status_field BackendState; }

current="$(state)"
for _ in 1 2 3 4 5; do   # a just-started daemon reports NoState/Starting for a moment
  case "$current" in NoState|Starting) sleep 1; current="$(state)" ;; *) break ;; esac
done

wait_s="${WIZARD_TAILSCALE_WAIT:-}"
if [ -z "$wait_s" ]; then
  if [ "${WIZARD_DETACHED:-0}" = 1 ] || (: </dev/tty) 2>/dev/null; then wait_s=1800; else wait_s=1; fi
fi
case "$wait_s" in ''|*[!0-9]*) wait_s=1800 ;; esac

if [ "$current" = "NeedsLogin" ]; then
  echo "Tailscale is installed. Open the sign-in link below to connect this box to YOUR tailnet."
  echo "Use the same Tailscale account on your phone or laptop. No shared team account is used."
  out="$(mktemp)"
  deadline=$(( $(date +%s) + wait_s ))
  # Plain `tailscale up` (no flags, so no setting changes), in the background: its link is read from status/output.
  # A node logged out with earlier non-default settings (e.g. --ssh) makes a bare `up` refuse; `login` keeps them.
  start_up() {
    local left=$(( deadline - $(date +%s) > 1 ? deadline - $(date +%s) : 1 ))
    ( root tailscale up --timeout="${left}s" >"$out" 2>&1 \
        || { grep -qi "non-default" "$out" && root tailscale login --timeout="${left}s" >>"$out" 2>&1; } || true ) &
    up_pid=$!
  }
  start_up
  shown=""
  while :; do
    current="$(state)"
    [ "$current" = Running ] && break
    link="$(status_field AuthURL)"
    [ -n "$link" ] || link="$(grep -o 'https://login\.tailscale\.com/[^[:space:]]*' "$out" 2>/dev/null | tail -1 || true)"
    if [ -n "$link" ] && [ "$link" != "$shown" ]; then
      echo "Tailscale sign-in link:"
      echo "$link"
      echo "[wizard-os] owner-action: Open this link to connect your Wizard to your Tailscale: $link"
      shown="$link"
    fi
    [ "$(date +%s)" -lt "$deadline" ] || break
    if ! kill -0 "$up_pid" 2>/dev/null; then
      current="$(state)"; [ "$current" = Running ] && break
      [ "$wait_s" -le 1 ] && break
      start_up   # `up` gave up early (or its link expired): a fresh link while the owner still has time
    fi
    sleep 2
  done
  # Connected: let `up` finish on its own. Out of time: stop the CLI (the daemon keeps the pending login, so the
  # same link still works later; nothing is reset).
  [ "$current" = Running ] || kill "$up_pid" 2>/dev/null || true
  wait "$up_pid" 2>/dev/null || true
  rm -f "$out"
  [ -z "$shown" ] || echo "[wizard-os] owner-action-done"
  current="$(state)"
  if [ "$current" != "Running" ] && [ "$current" != "NeedsLogin" ]; then
    echo "Tailscale could not connect (state $current). Existing networking was not reset." >&2
    exit 1
  fi
elif [ "$current" != "Running" ]; then
  echo "Tailscale already has state $current; leaving its settings alone. Run sudo tailscale up when ready."
fi
serve=""
[ "$current" != Running ] || serve="$(status_field serve)"

home="${WIZARD_OS_HOME:-$HOME/.wizard-os}"
mkdir -p "$home"
# Store only the provider and connection state, never credentials. Atomic so readers see a complete record.
python3 - "$home/private-access.json" "$current" "$serve" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
record = {"provider": "tailscale", "runtime": "host", "state": sys.argv[2], "installed": True}
if sys.argv[3]:
    record["serve_ready"] = sys.argv[3] == "on"
fd, name = tempfile.mkstemp(prefix=".private-access-", dir=path.parent)
try:
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.replace(name, path)
finally:
    if os.path.exists(name): os.unlink(name)
PY
if [ "$current" = "Running" ] && [ "$serve" != on ]; then
  what="MagicDNS"; [ "$serve" = https-off ] && what="HTTPS certificates"
  echo "Private access: Tailscale connected, but $what is off on your tailnet, so there are no private links yet."
  echo "Turn on MagicDNS and HTTPS certificates here, then run the install command again:"
  echo "$ADMIN_DNS"
  echo "[wizard-os] note: $what is off on your tailnet. Turn on MagicDNS and HTTPS certificates at $ADMIN_DNS, then run the install command again. Until then Wizard OS is box-only."
elif [ "$current" = "Running" ]; then
  echo "Private access: Tailscale connected (existing settings preserved)."
else
  echo "Private access: Tailscale preinstalled, not connected yet. Later: sudo tailscale up"
  echo "[wizard-os] note: Tailscale is not connected yet, so Wizard OS is box-only. Connect it, then run the install command again."
fi
