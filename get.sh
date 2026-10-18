#!/usr/bin/env bash
# Wizard OS, one command on a fresh Ubuntu/Debian server (run as root, or as a user with sudo):
#
#   curl -fsSL aiwizards.com/os.sh | bash
#
# Already running Hermes on this box? The same command finds it, whichever Linux user owns it (root or any named
# user), and installs Wizard OS *alongside* it as that user (the App Manager, the apps, and the Wizard shell pointed at
# your Hermes; your agent is linked to the apps with `wizard link`, reversible with `wizard unlink`). Nothing in your
# Hermes is migrated or moved. Several Hermes installs: WIZARD_HERMES_USER=<user> or HERMES_HOME=<path> picks one.
#
# Look first, change nothing:  curl -fsSL aiwizards.com/os.sh | WIZARD_CHECK=1 bash   (or: bash get.sh --check)
#
# Safe to run from a Hermes agent (e.g. asked in a Telegram chat): the Hermes restart the link needs is scheduled
# detached, ~20s after the install has printed its summary (WIZARD_RESTART_DELAY), never in the middle of the run.
#
# What it does:
#   1. As root: installs Docker, git, gh and python3-yaml, and creates the `wizard` user (docker group,
#      passwordless sudo) that owns everything. Skipped when run by an existing sudo user.
#   2. GitHub login only when asked for with WIZARD_GITHUB=1 (for private, team-only apps). All default apps are
#      public today, so normally nothing is asked.
#   3. As the wizard user: clones the Wizard App Manager and runs install.sh (Runtipi with the Wizard image,
#      box-only, the `wam` command, then the default app in every parking spot).
#
# Options (environment): WIZARD_MODE=auto|alongside|fresh (default auto: on a first install, alongside when an
# existing Hermes is found; a re-run keeps the mode the box already has), WIZARD_USER (default wizard), WAM_DIR, WAM_IMAGE, WAM_REF (branch to install), WIZARD_GITHUB=1, GITHUB_TOKEN,
# WIZARD_CHECK=1 (read-only preflight), WIZARD_HERMES_USER / HERMES_HOME (which Hermes), WIZARD_AGENT_SAFE=1|0.
#
# Release pins (wizard-os#27): the install follows one Wizard OS release file, wizard-os/releases/<version>.json,
# which names the App Manager commit and the image digests. WIZARD_RELEASE=<version> picks one; without it the
# release named in wizard-os/releases/stable is used. Re-running the command with no WIZARD_RELEASE updates to the
# current stable release; WIZARD_RELEASE=<version> keeps a box on that version. WAM_REF=<branch|sha> is the
# developer override: it installs that App Manager ref unpinned. Afterwards the installer reads back what actually
# runs (App Manager commit and image, shell image, default app versions) and records it in ~/.wizard-os/release.json.
# WIZARD_STRICT=1 makes any drift from the release exit non-zero, and refuses unpinned installs (incl. WAM_REF).
# Release default source pins and core images apply before direct AND dependency installs in either mode.
set -euo pipefail
REPO="${WAM_REPO:-https://github.com/humanitylabs-org/wizard-app-manager}"
# Release metadata is mirrored publicly; the canonical OS/spec repository stays private.
RELEASE_BASE="${WIZARD_RELEASE_BASE:-https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases}"
WUSER="${WIZARD_USER:-wizard}"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
tty_ok() { [ -r /dev/tty ] && [ -w /dev/tty ] && (: </dev/tty) 2>/dev/null; }
MODE="${WIZARD_MODE:-auto}"
case "$MODE" in auto|alongside|fresh) ;; *) echo "WIZARD_MODE must be auto, alongside or fresh" >&2; exit 2 ;; esac
REF="${WAM_REF:-main}"
# ---- Hermes detection (read-only; get.sh --check prints it) ----
# An existing Hermes = <hermes home>/config.yaml plus a `hermes` command, owned by ANY Linux user (root included).
# Found by: each account's ~/.hermes (root and uid >= 1000), hermes-gateway systemd units (system and user, incl. a
# custom HERMES_HOME), and running `hermes gateway` processes. Wizard OS installs alongside as the user that owns
# that Hermes; nothing in it is moved. Several found: WIZARD_HERMES_USER=<user> or HERMES_HOME=<path> picks one.
SYSUNIT_DIR="${WIZARD_SYSTEMD_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${WIZARD_SUDOERS_DIR:-/etc/sudoers.d}"
PROC="${WIZARD_PROC_ROOT:-/proc}"   # (tests point these three at fakes)
home_of() { getent passwd "$1" | cut -d: -f6; }
hermes_bin_for() {  # hermes_bin_for <user> <home> <hermes home>: the user's `hermes` command, or fail
  local c
  if [ -n "${WIZARD_HERMES_BIN:-}" ] && [ -x "$WIZARD_HERMES_BIN" ]; then echo "$WIZARD_HERMES_BIN"; return 0; fi
  for c in "$2/.local/bin/hermes" "$3/hermes-agent/venv/bin/hermes" "$3/hermes-agent/.venv/bin/hermes" \
      "$3/hermes-agent/.hermes/bin/hermes" "$3/bin/hermes" "$2/.local/share/pipx/venvs/hermes-agent/bin/hermes" \
      "$2/.local/pipx/venvs/hermes-agent/bin/hermes" "$2/.local/share/uv/tools/hermes-agent/bin/hermes" \
      /usr/local/bin/hermes /usr/bin/hermes; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  [ "$1" = "$(id -un)" ] && command -v hermes 2>/dev/null && return 0
  return 1
}
_unit_value() {  # _unit_value <unit file> <Key> [env var]: last Key= value (Environment=VAR=... when a var is given)
  local v
  if [ -n "${3:-}" ]; then
    v="$(grep -E "^Environment=\"?$3=" "$1" 2>/dev/null | tail -1 | sed -E "s/^Environment=\"?$3=([^\" ]*).*/\\1/")"
  else
    v="$(grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2-)"
  fi
  printf '%s' "$v"
}
_root_of() {  # a profile home (<root>/profiles/<name>) belongs to its root Hermes
  case "$1" in */profiles/*) printf '%s' "${1%/profiles/*}" ;; *) printf '%s' "${1%/}" ;; esac
}
# One line per Hermes home: user|hermes home|how it was found. Duplicates are merged by hermes_scan.
_hermes_sightings() {
  local name uid home f u hh line pid
  if [ -n "${HERMES_HOME:-}" ]; then
    u="$(stat -c %U "$HERMES_HOME" 2>/dev/null || id -un)"
    echo "$u|$HERMES_HOME|HERMES_HOME"
  fi
  while IFS=: read -r name _ uid _ _ home _; do
    case "$uid" in ''|*[!0-9]*) continue ;; esac
    { [ "$uid" -eq 0 ] || { [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ]; }; } || continue
    [ -n "$home" ] && [ -d "$home" ] || continue
    [ -f "$home/.hermes/config.yaml" ] && echo "$name|$home/.hermes|home"
    for f in "$home"/.config/systemd/user/hermes-gateway*.service; do
      [ -f "$f" ] || continue
      hh="$(_unit_value "$f" "" HERMES_HOME)"
      echo "$name|${hh:-$home/.hermes}|systemd-user:$(basename "$f")"
    done
  done < <(getent passwd)
  for f in "$SYSUNIT_DIR"/hermes-gateway*.service; do
    [ -f "$f" ] || continue
    u="$(_unit_value "$f" User)"; u="${u:-root}"
    hh="$(_unit_value "$f" "" HERMES_HOME)"
    echo "$u|${hh:-$(home_of "$u")/.hermes}|systemd-system:$(basename "$f")"
  done
  # Running gateways on the host (not in a container). Root sees everyone's HERMES_HOME; others their own only.
  while read -r u pid _; do
    is_gateway_pid "$pid" || continue
    hh="$(_penv "$pid" | sed -n 's/^HERMES_HOME=//p' | head -1)"
    echo "$u|${hh:-$(home_of "$u")/.hermes}|process:$pid"
  done < <(_ps)
}
# Process probes (functions so tests can stub them).
_ps() { ps -eo user:32=,pid=,comm= 2>/dev/null; }
_pcmd() { { tr '\0' '\n' < "$PROC/$1/cmdline"; } 2>/dev/null; }
_penv() { { tr '\0' '\n' < "$PROC/$1/environ"; } 2>/dev/null; }
_pcgroup() { { cat "$PROC/$1/cgroup"; } 2>/dev/null; }
# A `hermes gateway run` process on the host: argv has "gateway" then "run", a hermes path, and no container cgroup.
is_gateway_pid() {
  local argv; argv="$(_pcmd "$1")"
  printf '%s\n' "$argv" | grep -q hermes || return 1
  printf '%s\n' "$argv" | awk 'p && $0 == "run" { f = 1 } { p = ($0 == "gateway") } END { exit !f }' || return 1
  ! _pcgroup "$1" | grep -qE 'docker|containerd|libpod|kubepods|lxc'
}
# The parent pid of a process (stubbable).
_ppid() { { awk '{print $4}' "$PROC/$1/stat"; } 2>/dev/null; }
# Fills HERMES_FOUND (lines user|home|binary|sources|verified), merged by root Hermes home, filtered by
# WIZARD_HERMES_USER / HERMES_HOME. A home is kept when its config.yaml is readable and a `hermes` command exists,
# or when a gateway is running from it (unverified: it belongs to someone this user can't read).
hermes_scan() {
  local u hh src key bin h ok
  declare -A users=() homes=() srcs=()
  local -a order=()
  while IFS='|' read -r u hh src; do
    [ -n "$u" ] && [ -n "$hh" ] || continue
    if [ -n "${WIZARD_HERMES_USER:-}" ] && [ "$u" != "$WIZARD_HERMES_USER" ]; then continue; fi
    key="$(_root_of "$hh")"
    if [ -n "${HERMES_HOME:-}" ] && [ "$key" != "$(_root_of "$HERMES_HOME")" ]; then continue; fi
    if [ -z "${users[$key]:-}" ]; then order+=("$key"); users[$key]="$u"; homes[$key]="$hh"; srcs[$key]="$src"
    else
      case ",${srcs[$key]}," in *",$src,"*) ;; *) srcs[$key]="${srcs[$key]},$src" ;; esac
      [ "$src" = HERMES_HOME ] && homes[$key]="$hh"   # an explicit (profile) home is the one to link
    fi
  done < <(_hermes_sightings)
  HERMES_FOUND=""
  for key in ${order[@]+"${order[@]}"}; do
    u="${users[$key]}"; hh="${homes[$key]}"; h="$(home_of "$u")"; ok=""
    if [ -f "$hh/config.yaml" ] && bin="$(hermes_bin_for "$u" "$h" "$hh")"; then ok=yes
    else bin="?"; ok=unverified; fi
    HERMES_FOUND="$HERMES_FOUND$u|$hh|$bin|${srcs[$key]}|$ok"$'\n'
  done
  HERMES_FOUND="${HERMES_FOUND%$'\n'}"
}
# pick_hermes: sets H_USER H_HOME H_BIN H_SRC H_OK. 0 = one Hermes, 1 = none, 2 = several (HERMES_FOUND lists them).
# Several installs always require an explicit selector; existing Wizard records are not selectors.
pick_hermes() {
  local n line
  H_USER=""; H_HOME=""; H_BIN=""; H_SRC=""; H_OK=""
  hermes_scan
  [ -n "$HERMES_FOUND" ] || return 1
  n="$(printf '%s\n' "$HERMES_FOUND" | wc -l)"
  line="$HERMES_FOUND"
  if [ "$n" -gt 1 ]; then
    line=""
    return 2
  fi
  IFS='|' read -r H_USER H_HOME H_BIN H_SRC H_OK <<<"$line"
  return 0
}
# has_hermes [user]: an existing Hermes owned by that user (default: whoever owns the one Hermes found).
has_hermes() {
  local rc=0; pick_hermes || rc=$?
  [ "$rc" -eq 0 ] && [ "$H_OK" = yes ] && { [ -z "${1:-}" ] || [ "$H_USER" = "$1" ]; }
}
# ---- end Hermes detection ----

# Which Wizard OS does this user already have? alongside | fresh | none. Re-running the command updates a box, and
# an update must keep the box's mode: a fresh box whose owner also runs Hermes on the host stays fresh.
wos_mode() {  # wos_mode user
  local h; h="$(home_of "$1")"
  if [ -n "$h" ] && [ -f "$h/.wizard-os/alongside.json" ]; then echo alongside
  elif [ -n "$h" ] && [ -f "$h/.wizard-os/apps.json" ]; then echo fresh
  else echo none; fi
}
# A fresh Wizard OS already on this box: under the wizard user, or a Runtipi it runs, with no alongside record here.
fresh_box() {  # fresh_box [user]
  local u="${1:-$(id -un)}"
  [ "$(wos_mode "$u")" = alongside ] && return 1
  [ "$(wos_mode "$u")" = fresh ] || [ "$(wos_mode "$WUSER")" = fresh ] && return 0
  { docker ps --format '{{.Names}}' 2>/dev/null || sudo -n docker ps --format '{{.Names}}' 2>/dev/null; } | grep -qx runtipi
}
# Users (root, uid >= 1000) whose Wizard OS is alongside (re-runs keep it).
alongside_users() {
  local name uid home
  while IFS=: read -r name _ uid _ _ home _; do
    case "$uid" in ''|*[!0-9]*) continue ;; esac
    { [ "$uid" -eq 0 ] || { [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ]; }; } || continue
    [ -n "$home" ] && [ -f "$home/.wizard-os/alongside.json" ] && echo "$name"
  done < <(getent passwd)
  return 0
}
# Auto mode picks alongside only on a first install next to a Hermes; WIZARD_MODE=alongside forces it.
want_alongside() {  # want_alongside [user]
  [ "$MODE" = alongside ] && return 0
  [ "$MODE" = auto ] && has_hermes "${1:-}" && ! fresh_box "${1:-}"
}
# ---- end mode helpers ----

# Running inside a Hermes agent (a chat asked its agent to install Wizard OS)? Then the install must never restart
# the Hermes gateway synchronously: that kills the agent running this script and its reply. WIZARD_AGENT_SAFE=1|0
# forces it either way.
in_hermes_agent() {
  case "${WIZARD_AGENT_SAFE:-}" in 1) return 0 ;; 0) return 1 ;; esac
  [ "${_HERMES_GATEWAY:-}" = 1 ] || [ -n "${HERMES_SESSION_PLATFORM:-}" ] || [ -n "${HERMES_GATEWAY_SESSION:-}" ] \
    || [ "${HERMES_AGENT:-}" = true ] && return 0
  local pid=$$ _i
  for _i in $(seq 1 40); do   # or any ancestor process is a Hermes gateway
    pid="$(_ppid "$pid")"
    case "$pid" in ''|0|1) return 1 ;; esac
    is_gateway_pid "$pid" && return 0
  done
  return 1
}
docker_mode() {  # rootless | rootful | none, for user $1
  local uid; uid="$(getent passwd "${1:-$(id -un)}" | cut -d: -f3)"
  if [ -S "/run/user/${uid:-x}/docker.sock" ] || case "${DOCKER_HOST:-}" in unix:///run/user/*) true ;; *) false ;; esac; then
    echo rootless
  elif [ -S /var/run/docker.sock ] || command -v docker >/dev/null 2>&1; then echo rootful
  else echo none; fi
}

# Read-only preflight: what the installer found and what it would do. Changes nothing.
preflight() {
  local rc=0 plan ver svc
  say "Wizard OS preflight (read-only: nothing is installed or changed)"
  echo "running as:        $(id -un) (uid $(id -u))${SUDO_USER:+, via sudo from $SUDO_USER}"
  if in_hermes_agent; then echo "run by an agent:   yes (a Hermes agent started this; its gateway restart will be deferred)"
  else echo "run by an agent:   no"; fi
  pick_hermes || rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "Hermes found:      SEVERAL (pick one with WIZARD_HERMES_USER=<user> or HERMES_HOME=<path>):"
    printf '%s\n' "$HERMES_FOUND" | while IFS='|' read -r u hh b s _; do echo "  - user $u, home $hh, command $b ($s)"; done
    plan="stop: several Hermes installs, none picked"
  elif [ "$rc" -eq 1 ]; then
    echo "Hermes found:      none"
  else
    echo "Hermes owner:      $H_USER"
    echo "Hermes home:       $H_HOME"
    echo "hermes command:    $H_BIN"
    svc="$(printf '%s' "$H_SRC" | tr ',' '\n' | { grep -E '^(systemd|process)' || true; } | tr '\n' ' ')"
    echo "service:           ${svc:-not running as a service}"
    if [ "$H_OK" = yes ] && { [ "$H_USER" = "$(id -un)" ] || [ "$(id -u)" -eq 0 ]; }; then
      if [ "$H_USER" = "$(id -un)" ]; then ver="$(HERMES_HOME="$H_HOME" timeout 20 "$H_BIN" --version 2>/dev/null </dev/null | head -1 || true)"
      else ver="$(timeout 20 runuser -u "$H_USER" -- env HERMES_HOME="$H_HOME" "$H_BIN" --version 2>/dev/null </dev/null | head -1 || true)"; fi
      echo "Hermes version:    ${ver:-unknown}"
      # Informational only: the installer never resets the owner's settings.
      if [ -r "$H_HOME/config.yaml" ]; then
        grep -qE '^[[:space:]]*api_server:' "$H_HOME/config.yaml" && echo "note:              config.yaml has its own platforms.api_server block (kept as is)"
        grep -qE '^mcp_servers:' "$H_HOME/config.yaml" && echo "note:              config.yaml already has mcp_servers (the wizard entry is added next to them)"
        [ -e "$H_HOME/plugins/wizard-os" ] && echo "note:              a wizard-os plugin already exists in $H_HOME/plugins"
        [ -d "$H_HOME/profiles" ] && echo "note:              Hermes profiles exist; the home above is the one linked"
      fi
    elif [ "$H_OK" = unverified ]; then
      echo "note:              a gateway runs as $H_USER, but $(id -un) can't read its home; run this as $H_USER or root"
    fi
  fi
  echo "docker:            $(docker_mode "${H_USER:-$(id -un)}")"
  echo "existing Wizard OS: $(wos_mode "$(id -un)") for $(id -un)$( [ "$(id -un)" = "$WUSER" ] || echo ", $(wos_mode "$WUSER") for $WUSER")"
  if [ -z "${plan:-}" ] && [ "$MODE" != fresh ]; then
    if [ "$rc" -eq 0 ] && [ "$H_OK" != yes ]; then plan="stop: existing Hermes cannot be verified"
    elif [ "$rc" -eq 1 ] && { [ "$MODE" = alongside ] || [ -n "${WIZARD_HERMES_USER:-}${HERMES_HOME:-}" ]; }; then
      plan="stop: selected Hermes was not found"
    fi
  fi
  if [ -z "${plan:-}" ]; then
    if [ "$MODE" = fresh ]; then plan="fresh (WIZARD_MODE=fresh)"
    elif [ "$rc" -eq 0 ] && [ "$(wos_mode "$H_USER")" = alongside ]; then plan="alongside as $H_USER (re-run: keeps alongside)"
    elif [ "$rc" -eq 0 ] && [ "$H_OK" = yes ] && want_alongside "$H_USER"; then
      if [ "$H_USER" = "$(id -un)" ] || [ "$(id -u)" -eq 0 ]; then plan="alongside as $H_USER, next to $H_HOME"
      else plan="stop: this Hermes belongs to $H_USER; run the command as $H_USER or as root"; fi
    elif [ "$rc" -eq 0 ] && [ "$H_OK" = unverified ] && [ "$MODE" = auto ] && ! fresh_box; then
      plan="stop: Hermes runs as $H_USER; run the command as $H_USER or as root"
    elif [ "$(id -u)" -eq 0 ]; then plan="fresh, as the $WUSER user (created if missing)"
    else plan="fresh, as $(id -un)"; fi
  fi
  echo "would install:     $plan"
  echo
  echo "Nothing was changed. To install: curl -fsSL aiwizards.com/os.sh | bash"
}

# Forwarded to every re-exec of this script (sudo -i drops the environment).
fwd_env() {
  printf '%s\n' WAM_IMAGE="${WAM_IMAGE:-}" WAM_DIR="${WAM_DIR:-}" WAM_REF="${WAM_REF:-}" \
    WIZARD_RELEASE="${WIZARD_RELEASE:-}" WIZARD_RELEASE_BASE="${WIZARD_RELEASE_BASE:-}" WIZARD_STRICT="${WIZARD_STRICT:-}" \
    WAM_REPO="$REPO" WIZARD_UX_IMAGE="${WIZARD_UX_IMAGE:-}" WIZARD_AGENT_SERVICE_IMAGE="${WIZARD_AGENT_SERVICE_IMAGE:-}" \
    WIZARD_SHELL_IMAGE="${WIZARD_SHELL_IMAGE:-}" WIZARD_PRIVATE_ACCESS="${WIZARD_PRIVATE_ACCESS:-tailscale}" \
    WIZARD_AGENT_SAFE="$AGENT_SAFE" WIZARD_RESTART_DELAY="${WIZARD_RESTART_DELAY:-}"
}
# as_user <user> <mode> [HERMES_HOME]: run this installer again as <user> (a user session bus, no root left over).
as_user() {
  local u="$1" m="$2" uid; uid="$(id -u "$u")"
  local -a env; mapfile -t env < <(fwd_env)
  loginctl enable-linger "$u" >/dev/null 2>&1 || true   # user units (API, dashboard) need the user's manager
  for _ in $(seq 1 "${WIZARD_BUS_WAIT:-30}"); do [ -S "/run/user/$uid/bus" ] && break; sleep 1; done
  exec sudo -iu "$u" env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    "${env[@]}" WIZARD_GITHUB=0 WIZARD_MODE="$m" ${3:+HERMES_HOME="$3"} \
    bash -c "set -o pipefail; curl -fsSL '$REPO/raw/$REF/get.sh' | bash"
}
prepare_server() {  # root only: base packages and Docker (Docker is left alone when it is new enough)
  say "Preparing the server"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 python3-yaml gh sudo >/dev/null
  if ! command -v docker >/dev/null || [ "$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)" -lt 28 ] 2>/dev/null; then
    curl -fsSL https://get.docker.com | sh >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
}
stop_several() {
  echo "Found more than one Hermes on this box, so Wizard OS doesn't know which one to install next to:" >&2
  printf '%s\n' "$HERMES_FOUND" | while IFS='|' read -r u hh _ s _; do echo "  - user $u, Hermes home $hh ($s)" >&2; done
  echo "Pick one and run the command again, e.g.:" >&2
  echo "  curl -fsSL aiwizards.com/os.sh | WIZARD_HERMES_USER=<user> bash      (or HERMES_HOME=<path>)" >&2
  echo "Nothing was changed. 'WIZARD_CHECK=1' shows what the installer finds without changing anything." >&2
  exit 2
}

[ -n "${HOME:-}" ] || { HOME="$(home_of "$(id -un)")"; export HOME; }   # a system service may run us without HOME/USER
[ -n "${USER:-}" ] || { USER="$(id -un)"; export USER; }
AGENT_SAFE=0; in_hermes_agent && AGENT_SAFE=1
export WIZARD_AGENT_SAFE="$AGENT_SAFE"
case "${1:-}" in --check|check) WIZARD_CHECK=1 ;; esac
if [ "${WIZARD_CHECK:-0}" = 1 ]; then preflight; exit 0; fi

if [ "$MODE" != fresh ]; then
  prc=0; pick_hermes || prc=$?
  if [ "$prc" -eq 2 ]; then stop_several; fi
  if { [ "$prc" -eq 1 ] && { [ "$MODE" = alongside ] || [ -n "${WIZARD_HERMES_USER:-}${HERMES_HOME:-}" ]; }; } ||
     { [ "$prc" -eq 0 ] && [ "$H_OK" != yes ]; }; then
    echo "Cannot verify the selected/existing Hermes; refusing to install a second agent. Check owner, HERMES_HOME, config.yaml and executable. Nothing was changed." >&2
    exit 2
  fi
  if [ "$prc" -eq 0 ] && [ -n "${WIZARD_HERMES_USER:-}${HERMES_HOME:-}" ]; then MODE=alongside; fi
fi

if [ "$(id -u)" -eq 0 ] && [ "$MODE" != fresh ]; then
  # Alongside the Hermes on this box, as the user that owns it (root included). A re-run keeps the box's mode.
  ALONG_USER=""; ALONG_HOME=""
  if [ "$MODE" = alongside ] || [ -n "${WIZARD_HERMES_USER:-}${HERMES_HOME:-}" ] || ! fresh_box root; then
    prc=0; pick_hermes || prc=$?
    if [ "$prc" -eq 2 ]; then stop_several; fi
    if [ "$prc" -eq 0 ] && [ "$H_OK" = yes ]; then
      ALONG_USER="$H_USER"; ALONG_HOME="$H_HOME"
    elif [ "$MODE" = alongside ]; then
      echo "WIZARD_MODE=alongside, but no Hermes was found on this box (WIZARD_CHECK=1 shows what was looked at)." >&2
      exit 2
    fi
  fi
  if [ -n "$ALONG_USER" ]; then
    say "Found Hermes for $ALONG_USER${ALONG_HOME:+ ($ALONG_HOME)}; installing Wizard OS alongside it as $ALONG_USER"
    if [ "$ALONG_USER" != root ] && ! sudo -n -u "$ALONG_USER" sudo -n true 2>/dev/null; then
      echo "Alongside requires administrator-provisioned sudo for $ALONG_USER; refusing to grant passwordless root access automatically. Nothing was changed." >&2
      exit 2
    fi
    prepare_server
    if [ "$ALONG_USER" != root ]; then
      if [ "$(docker_mode "$ALONG_USER")" = rootful ] && ! id -nG "$ALONG_USER" | tr ' ' '\n' | grep -qx docker; then
        usermod -aG docker "$ALONG_USER" && echo "added $ALONG_USER to the docker group"
      fi
      as_user "$ALONG_USER" alongside "$ALONG_HOME"
    fi
    # Root owns this Hermes: Wizard OS runs as root too. Its user units need root's user manager (linger).
    loginctl enable-linger root >/dev/null 2>&1 || true
    for _ in $(seq 1 "${WIZARD_BUS_WAIT:-30}"); do [ -S /run/user/0/bus ] && break; sleep 1; done
    export XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
    [ -z "$ALONG_HOME" ] || export HERMES_HOME="$ALONG_HOME"
    MODE=alongside
  fi
fi

if [ "$(id -u)" -eq 0 ] && [ "$MODE" != alongside ]; then
  prepare_server

  if ! id "$WUSER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$WUSER"
    echo "created user $WUSER"
  fi
  usermod -aG docker "$WUSER"
  echo "$WUSER ALL=(ALL) NOPASSWD:ALL" > "$SUDOERS_DIR/90-wizard-$WUSER"
  chmod 0440 "$SUDOERS_DIR/90-wizard-$WUSER"
  # Root's SSH keys also let you in as the wizard user.
  if [ -s /root/.ssh/authorized_keys ] && [ ! -s "/home/$WUSER/.ssh/authorized_keys" ]; then
    install -d -m 700 -o "$WUSER" -g "$WUSER" "/home/$WUSER/.ssh"
    install -m 600 -o "$WUSER" -g "$WUSER" /root/.ssh/authorized_keys "/home/$WUSER/.ssh/authorized_keys"
  fi
  loginctl enable-linger "$WUSER"
  WUID="$(id -u "$WUSER")"
  for _ in $(seq 1 "${WIZARD_BUS_WAIT:-30}"); do [ -S "/run/user/$WUID/bus" ] && break; sleep 1; done

  as_wizard() { sudo -iu "$WUSER" env XDG_RUNTIME_DIR="/run/user/$WUID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$WUID/bus" "$@"; }
  # GitHub login for private defaults, kept by the wizard user so later updates of those apps work too.
  if [ -n "${GITHUB_TOKEN:-}" ] && ! as_wizard gh auth status >/dev/null 2>&1; then
    printf '%s' "$GITHUB_TOKEN" | as_wizard gh auth login --with-token >/dev/null 2>&1 || true
  fi
  if [ "${WIZARD_GITHUB:-0}" = "1" ] && ! as_wizard gh auth status >/dev/null 2>&1 && tty_ok; then
    say "GitHub login (optional)"
        printf "Log in to GitHub for private apps? [Y/n] " >/dev/tty
    read -r -t 120 ans </dev/tty || ans=n
    if [ "${ans:-y}" != "n" ] && [ "${ans:-y}" != "N" ]; then
      as_wizard gh auth login --hostname github.com --git-protocol https --scopes read:packages </dev/tty >/dev/tty 2>&1 || echo "GitHub login skipped"
    fi
  fi

  say "Installing Wizard OS as $WUSER"
  as_user "$WUSER" fresh
fi

# ---- as the wizard user (or any user with sudo) ----
command -v git >/dev/null || sudo apt-get install -y -qq git >/dev/null
if [ "${WIZARD_GITHUB:-0}" = "1" ] && command -v gh >/dev/null && ! gh auth status >/dev/null 2>&1 && tty_ok; then
  printf "Log in to GitHub for private apps? [Y/n] " >/dev/tty
  read -r -t 120 ans </dev/tty || ans=n
  if [ "${ans:-y}" != "n" ] && [ "${ans:-y}" != "N" ]; then
    gh auth login --hostname github.com --git-protocol https --scopes read:packages </dev/tty >/dev/tty 2>&1 || true
  fi
fi
# Alongside or fresh? A re-run keeps the box's mode. On a first install next to a Hermes: one clear question on a
# terminal; without one, alongside.
if [ "$MODE" = auto ]; then
  if [ "$(wos_mode "$(id -un)")" = alongside ]; then
    MODE=alongside
  elif ! want_alongside "$(id -un)"; then
    MODE=fresh
    if has_hermes "$(id -un)"; then
      echo "Found Hermes on this box, but Wizard OS is already installed fresh here: keeping fresh mode (WIZARD_MODE=alongside switches)."
    elif ! fresh_box; then
      # A Hermes that belongs to someone else (root or another user): installing fresh here would run a second
      # agent next to it. Say so instead; root (or that user) installs alongside it.
      prc=0; pick_hermes || prc=$?
      if [ "$prc" -eq 2 ]; then stop_several; fi
      if [ "$prc" -eq 0 ] && [ "$H_USER" != "$(id -un)" ]; then
        echo "Hermes on this box belongs to $H_USER${H_HOME:+ ($H_HOME)}, not $(id -un). Run the command as $H_USER," >&2
        echo "or as root (curl -fsSL aiwizards.com/os.sh | sudo bash): Wizard OS then installs alongside it as $H_USER." >&2
        echo "WIZARD_MODE=fresh installs a separate Wizard OS for $(id -un) instead. Nothing was changed." >&2
        exit 2
      fi
    fi
  else
    MODE=alongside
    if tty_ok; then
      say "Found Hermes on this box (${H_HOME:-${HERMES_HOME:-$HOME/.hermes}})"
      printf "Install Wizard OS alongside it? Your Hermes stays as it is and gets linked to the apps. [Y/n] " >/dev/tty
      read -r -t 120 ans </dev/tty || ans=y
      case "${ans:-y}" in n|N|no|NO) MODE=fresh ;; esac
    else
      echo "Found Hermes on this box: installing alongside it (set WIZARD_MODE=fresh to skip)."
    fi
  fi
fi
export WIZARD_MODE="$MODE"
if [ "$MODE" = alongside ]; then   # the linked Hermes, for install.sh / wam / wizard (a `hermes` off PATH included)
  prc=0; pick_hermes || prc=$?
  if [ "$prc" -eq 0 ] && [ "$H_USER" = "$(id -un)" ] && [ "$H_OK" = yes ]; then
    export HERMES_HOME="${HERMES_HOME:-$H_HOME}"
    export WIZARD_HERMES_BIN="$H_BIN"
    PATH="$(dirname "$H_BIN"):$PATH"; export PATH  # older pinned wam/wizard discover Hermes through PATH
  else
    echo "Alongside requires a verified Hermes owned by $(id -un); refusing this target." >&2
    exit 2
  fi
fi
DIR="${WAM_DIR:-$HOME/wizard-app-manager}"

# Which release? (wizard-os#27) WAM_REF set = developer override, unpinned. Otherwise WIZARD_RELEASE, else the
# stable pointer. No stable pointer published yet = the previous behaviour (App Manager main), said out loud.
PINS=""
RECORD="$HOME/.wizard-os/release.json"
mkdir -p "$HOME/.wizard-os"
UNPINNED=""
unpinned() {  # unpinned <reason>: refuse it under WIZARD_STRICT=1 (box untouched); recorded once the checkout succeeds
  if [ "${WIZARD_STRICT:-0}" = "1" ]; then echo "WIZARD_STRICT=1: refusing an unpinned install ($1)" >&2; exit 3; fi
  UNPINNED="$1"
}
record_unpinned() {
  python3 -c 'import json, sys, time
with open(sys.argv[2], "w") as f:
    json.dump({"status": "unpinned", "reason": sys.argv[1], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    f.write("\n")' "$UNPINNED" "$RECORD"
}
fetch_release() {  # fetch_release <name> <dest>: 0 = fetched, 4 = not published (404 / no file), 1 = any other error
  local url="$RELEASE_BASE/$1" code
  case "$url" in
    file://*) [ -f "${url#file://}" ] || return 4; cp "${url#file://}" "$2"; return ;;
  esac
  code="$(curl -sSL --retry 3 --retry-delay 2 -o "$2" -w '%{http_code}' "$url" 2>/dev/null)" || true
  case "$code" in 200) return 0 ;; 404) return 4 ;; *) echo "fetching $url failed (HTTP ${code:-none})" >&2; return 1 ;; esac
}
if [ -z "${WAM_REF:-}" ]; then
  REL="${WIZARD_RELEASE:-}"
  if [ -z "$REL" ]; then
    frc=0; fetch_release stable "$RECORD.stable" || frc=$?
    if [ "$frc" -eq 1 ]; then
      echo "Could not read the Wizard OS stable release; not installing unpinned bits by accident. Try again, or set WIZARD_RELEASE." >&2
      exit 1
    fi
    [ "$frc" -eq 0 ] && REL="$(tr -d ' \t\r\n' < "$RECORD.stable")"
    rm -f "$RECORD.stable"
  fi
  if [ -n "$REL" ]; then
    case "$REL" in *[!0-9A-Za-z.+-]*|.*) echo "WIZARD_RELEASE '$REL' is not a version" >&2; exit 2 ;; esac
    PINS="$HOME/.wizard-os/release-pins.json"
    fetch_release "$REL.json" "$PINS.tmp" || { echo "Could not fetch Wizard OS release $REL ($RELEASE_BASE/$REL.json)" >&2; exit 1; }
    # Only validated values leave Python (40-hex commit, sha256 digests, plain image names), shell-quoted.
    PIN_ENV="$(python3 - "$PINS.tmp" "$REL" <<'PY'
import json, re, shlex, sys
path, want = sys.argv[1], sys.argv[2]
try:
    rel = json.load(open(path))
except ValueError as e:
    sys.exit(f"release {want}: not JSON ({e})")
if str(rel.get("version")) != want:
    sys.exit(f"release file says version {rel.get('version')!r}, expected {want!r}")
def image(name):
    im = (rel.get("images") or {}).get(name) or {}
    ref, dig = str(im.get("ref", "")), str(im.get("digest", ""))
    if not im:
        sys.exit(f"release {want}: images.{name} is missing; a release pins both the App Manager and the shell image")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", dig):
        sys.exit(f"release {want}: image {name} digest {dig!r} is not sha256:<64 hex>")
    repo = re.sub(r"(@.*|:[^/:]*)$", "", ref)
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+", repo):
        sys.exit(f"release {want}: image {name} ref {ref!r} is not an image name")
    return f"{repo}@{dig}"
wam = (rel.get("components") or {}).get("wizard-app-manager") or {}
sha = str(wam.get("commit", ""))
if not re.fullmatch(r"[0-9a-f]{40}", sha):
    sys.exit(f"release {want}: wizard-app-manager commit {sha!r} is not a 40-char sha")
split = "wizard-ux" in (rel.get("images") or {}) or "wizard-agent-service" in (rel.get("images") or {})
seen = set()
defaults = rel.get("default_apps", {})
if not isinstance(defaults, dict):
    sys.exit("release default_apps must be an object")
for slot, row in defaults.items():
    if row is None:
        continue
    if not isinstance(row, dict):
        sys.exit(f"release default app {slot}: expected object or null")
    if "repo" not in row and "commit" not in row:  # older version-only manifests
        continue
    app, repo, commit = (row.get(k, "") for k in ("app", "repo", "commit"))
    if (not isinstance(app, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", app) or app in seen
        or not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo)
        or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        sys.exit(f"release default app {slot}: invalid/duplicate app, owner/repo or full commit")
    seen.add(app)
pairs = [("PIN_WAM_COMMIT", sha), ("PIN_WAM_IMAGE", image("wizard-app-manager"))]
if split:
    pairs += [("PIN_UX_IMAGE", image("wizard-ux")), ("PIN_AGENT_SERVICE_IMAGE", image("wizard-agent-service"))]
else:
    pairs += [("PIN_SHELL_IMAGE", image("wizard-shell"))]
for k, v in pairs:
    print(f"{k}={shlex.quote(v)}")
PY
)" || { rm -f "$PINS.tmp"; exit 2; }
    eval "$PIN_ENV"
    mv "$PINS.tmp" "$PINS"
    export WIZARD_RELEASE_PINS="$PINS"
    REF="$PIN_WAM_COMMIT"
    if [ -n "${WAM_IMAGE:-}" ] && [ "$WAM_IMAGE" != "$PIN_WAM_IMAGE" ]; then
      echo "note: WAM_IMAGE=$WAM_IMAGE overrides the release's App Manager image"
    else
      [ -z "$PIN_WAM_IMAGE" ] || export WAM_IMAGE="$PIN_WAM_IMAGE"
    fi
    for component in SHELL UX AGENT_SERVICE; do
      pin_var="PIN_${component}_IMAGE"
      override_var="WIZARD_${component}_IMAGE"
      pin_value="${!pin_var:-}"
      override_value="${!override_var:-}"
      [ -n "$pin_value" ] || continue
      if [ -n "$override_value" ] && [ "$override_value" != "$pin_value" ]; then
        echo "note: $override_var overrides the release image"
      else
        export "$override_var=$pin_value"
      fi
    done
    say "Installing Wizard OS $REL (App Manager ${REF:0:12})"
  else
    echo "note: no Wizard OS stable release is published at $RELEASE_BASE/stable; installing the App Manager's main branch (unpinned)."
    unpinned "no stable release published at $RELEASE_BASE/stable"
  fi
else
  unpinned "WAM_REF=$WAM_REF (developer override)"
fi

[ -z "$PINS" ] || rm -f "$RECORD"   # this pinned run writes its own record; an older one must not survive it
if [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then   # a pinned commit: fetch exactly it, detached, and prove it
  if [ -d "$DIR/.git" ]; then
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
      echo "$DIR has local changes; a pinned install would run them. Commit/stash them, or use WAM_REF for a dev checkout." >&2
      exit 1
    fi
  else
    git clone -q "$REPO" "$DIR"
  fi
  git -C "$DIR" fetch -q origin "$REF"
  git -C "$DIR" checkout -q --detach "$REF"
  [ "$(git -C "$DIR" rev-parse HEAD)" = "$REF" ] || { echo "App Manager checkout is not $REF" >&2; exit 1; }
elif [ -d "$DIR/.git" ]; then
  # (an `a && b` list doesn't trip set -e: without this a bad WAM_REF ran whatever was checked out and exited 0)
  git -C "$DIR" fetch -q origin "$REF" || { echo "App Manager ref '$REF' not found in $REPO" >&2; exit 1; }
  git -C "$DIR" checkout -q -B "$REF" FETCH_HEAD
else
  git clone -q --branch "$REF" "$REPO" "$DIR"
fi
# Started by a Hermes agent: the gateway restart that linking needs must come after this run, never during it (it
# would kill the agent running this). install.sh defers it itself (its defers-hermes-restart marker). An older install.sh (a
# release from before that) restarts synchronously: there a `hermes` shim on PATH turns that one call into a
# deferred restart, scheduled once install.sh is done; every other hermes command goes to the real one.
DEFER_SHIM=""
export WIZARD_OUTER_INSTALL=1
real="${WIZARD_HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}"
rm -f "$HOME/.wizard-os/hermes-restart.pending"
if [ "$AGENT_SAFE" = 1 ] && [ "$MODE" = alongside ] && ! grep -q "^# wizard-install: defers-hermes-restart" "$DIR/install.sh"; then
  real="${WIZARD_HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}"
  DEFER_SHIM="$(mktemp -d "$HOME/.wizard-os/agent-safe-bin.XXXXXX")"
  cat > "$DEFER_SHIM/hermes" <<SHIM
#!/bin/sh
# Written by the Wizard OS installer for an agent-run install.
# Kept afterwards (a unit may have recorded this path) and then always runs the real hermes.
if [ -f "$HOME/.wizard-os/agent-safe.active" ] && [ "\$1" = gateway ] && [ "\$2" = restart ]; then
  echo deferred > "$HOME/.wizard-os/hermes-restart.pending"; exit 0
fi
exec '$real' "\$@"
SHIM
  chmod +x "$DEFER_SHIM/hermes"
  export PATH="$DEFER_SHIM:$PATH" WIZARD_HERMES_BIN="$DEFER_SHIM/hermes"
  rm -f "$HOME/.wizard-os/hermes-restart.pending"
  touch "$HOME/.wizard-os/agent-safe.active"
fi
deferred_restart() {  # after an older install.sh under an agent: schedule the restart it asked for, detached
  [ "$AGENT_SAFE" = 1 ] && [ "$MODE" = alongside ] || return 0
  rm -f "$HOME/.wizard-os/agent-safe.active"
  [ -z "$DEFER_SHIM" ] || export PATH="${PATH#"$DEFER_SHIM:"}"
  [ -f "$HOME/.wizard-os/hermes-restart.pending" ] || return 0
  rm -f "$HOME/.wizard-os/hermes-restart.pending"
  local delay="${WIZARD_RESTART_DELAY:-20}" log="$HOME/.wizard-os/hermes-restart.log" job
  case "$delay" in ''|*[!0-9]*) delay=20 ;; esac
  job="HERMES_HOME='${HERMES_HOME:-$HOME/.hermes}' '$real' gateway restart; sleep 20; '$HOME/.local/bin/wizard' doctor"
  local -a run=(systemd-run --user); [ "$(id -u)" -eq 0 ] && run=(systemd-run)
  if ! { command -v systemd-run >/dev/null && "${run[@]}" --quiet --collect --unit "wizard-hermes-restart-$(date +%s)" \
      --on-active="${delay}s" --setenv=HOME="$HOME" /bin/bash -c "exec >>'$log' 2>&1; $job" >/dev/null 2>&1; }; then
    setsid nohup /bin/bash -c "sleep $delay; exec >>'$log' 2>&1; $job" </dev/null >/dev/null 2>&1 &
  fi
  echo
  echo "Hermes will restart in ~${delay}s to load Wizard OS (this chat pauses briefly; send any message after a"
  echo "minute). Then run 'wizard doctor'. Logged in $log."
}
if [ -z "$PINS" ]; then
  unset WIZARD_RELEASE_PINS  # WAM_REF stays unpinned; old receipts cannot silently activate pins
  [ -z "$UNPINNED" ] || record_unpinned
  rc=0; bash "$DIR/install.sh" || rc=$?
  rm -f "$HOME/.wizard-os/agent-safe.active"
  [ "$rc" -ne 0 ] || deferred_restart
  exit "$rc"
fi

rc=0
bash "$DIR/install.sh" || rc=$?
# Read back what actually runs and compare it with the release (always, even after a failed install).
vrc=0
python3 - "$PINS" "$DIR" "${WIZARD_MODE:-fresh}" "$RECORD" "${WIZARD_STRICT:-0}" "$rc" <<'PY' || vrc=$?
import json, os, re, subprocess, sys, time
pins_path, wam_dir, mode, out_path, strict, install_rc = sys.argv[1:7]
rel = json.load(open(pins_path))
def write(rec):
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=2); f.write("\n")
    os.replace(tmp, out_path)
checks = []
def check(name, expected, observed, core):
    checks.append({"name": name, "expected": expected, "observed": observed, "ok": expected == observed, "core": core})
def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
try:
    docker = ["docker"] if subprocess.run(["docker", "info"], capture_output=True).returncode == 0 else ["sudo", "-n", "docker"]
except OSError:
    docker = ["docker"]
def digests_of(container):
    image_id = run(*docker, "inspect", "--format", "{{.Image}}", container)
    if not image_id:
        return []
    return [d.split("@", 1)[1] for d in json.loads(run(*docker, "image", "inspect", "--format", "{{json .RepoDigests}}", image_id) or "[]") if "@" in d]
def digest_check(name, container, core):
    want = (rel.get("images") or {}).get(name, {}).get("digest")
    if not want:
        return
    have = digests_of(container) if container else []
    check(f"image {name}", want, want if want in have else (",".join(have) or f"no running container{' ' + container if container else ''}"), core)
try:
  check("wizard-app-manager commit", rel["components"]["wizard-app-manager"]["commit"], run("git", "-C", wam_dir, "rev-parse", "HEAD"), True)
  dirty = run("git", "-C", wam_dir, "status", "--porcelain", "--untracked-files=no")
  check("App Manager checkout clean", "clean", "clean" if not dirty else "modified: " + " ".join(dirty.split()[:6]), True)
  running_names = run(*docker, "ps", "--format", "{{.Names}}").split()
  digest_check("wizard-app-manager", "runtipi" if "runtipi" in running_names else "", True)
  try:
      apps = {a["id"]: a for a in json.load(open(os.path.expanduser("~/.wizard-os/apps.json"))).get("apps", [])}
  except (OSError, ValueError):
      apps = {}
  names = run(*docker, "ps", "--format", "{{.Names}}").split()
  def container_for(app):
      # Exact Runtipi project boundary, not a substring that can match another app/test stack.
      matches = [n for n in names if n.startswith(app + "_wizardapps-")]
      return matches[0] if len(matches) == 1 else ""
  image_apps = []
  if "wizard-ux" in (rel.get("images") or {}):
      image_apps = [("wizard-agent-service", "agent-service", "wizard-agent-service"),
                    ("wizard-ux", "shell", "wizard-ux")]
  else:
      image_apps = [("wizard-shell", "shell", "wizard-shell")]
  for image_name, slot, fallback in image_apps:
      app_id = ((rel.get("default_apps") or {}).get(slot) or {}).get("app", fallback)
      container = container_for(app_id)
      want = (rel.get("images") or {}).get(image_name, {}).get("digest")
      if install_rc == "0" and mode == "alongside" and want and container and want not in digests_of(container):
          import shutil
          wam = os.path.expanduser("~/.local/bin/wam")
          wam = wam if os.access(wam, os.X_OK) else (shutil.which("wam") or wam)
          print(f"updating {app_id} to the release's image", flush=True)
          queued = True
          try:
              subprocess.run([wam, "app", "update", app_id], timeout=900)
          except (OSError, subprocess.TimeoutExpired) as e:
              queued = False
              print(f"note: {wam} app update {app_id} failed: {e}", flush=True)
          for _ in range(int(os.environ.get("WIZARD_SHELL_WAIT", "60")) if queued else 0):
              names = run(*docker, "ps", "--format", "{{.Names}}").split()
              container = container_for(app_id)
              if container and want in digests_of(container):
                  break
              time.sleep(5)
      digest_check(image_name, container, mode == "alongside")
      if mode == "alongside" and checks[-1]["name"] == f"image {image_name}" and not checks[-1]["ok"]:
          checks[-1]["remedy"] = f"~/.local/bin/wam app update {app_id}, then re-run this command"
  for slot, d in (rel.get("default_apps") or {}).items():
      if not d or (mode == "alongside" and d.get("alongside")):
          continue
      app_id = d.get("app") or f"<no app for {slot}>"
      a = apps.get(app_id)
      check(f"default app {slot}", f"{app_id} {d.get('index_version')}",
            f"{app_id} {a.get('version')}" if a else f"{app_id} not installed", False)
      if d.get("commit"):
          check(f"default app source {slot}", d["commit"], (a or {}).get("commit") or "not installed", True)
except Exception as e:   # never leave a run without a record
    check("read-back", "completed", f"error: {type(e).__name__}: {e}"[:200], True)
core_bad = [c for c in checks if c["core"] and not c["ok"]]
drift = [c for c in checks if not c["core"] and not c["ok"]]
status = ("install-failed" if install_rc != "0" else "mismatch" if core_bad else "drift" if drift else "pinned")
rec = {"release": rel.get("version"), "mode": mode, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "status": status, "install_rc": int(install_rc), "checks": checks}
write(rec)
print(f"\nWizard OS {rel.get('version')}: what runs vs the release")
for c in checks:
    print(f"  {'PASS' if c['ok'] else ('FAIL' if c['core'] else 'DRIFT')}  {c['name']}: {c['observed']}"
          + ("" if c["ok"] else f" (release: {c['expected']})") + (f"\n        fix: {c['remedy']}" if c.get("remedy") else ""))
print(f"  -> {status}; recorded in {out_path}")
sys.exit(1 if core_bad else (3 if drift and strict == "1" else 0))
PY
rm -f "$HOME/.wizard-os/agent-safe.active"
if [ "$rc" -eq 0 ] && [ "$vrc" -eq 0 ]; then deferred_restart; fi
[ "$rc" -eq 0 ] || exit "$rc"
exit "$vrc"
