#!/usr/bin/env bash
# Wizard OS, one command on a fresh Ubuntu/Debian server (run as root, or as a user with sudo):
#
#   curl -fsSL aiwizards.com/os.sh | bash
#
# Already running Hermes on this box? Run the same command as the user who owns that Hermes: Wizard OS installs
# *alongside* it (the App Manager, the apps, and the Wizard shell pointed at your Hermes; your agent is linked to the
# apps with `wizard link`, reversible with `wizard unlink`). Nothing in your Hermes is migrated or moved.
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
# existing Hermes is found; a re-run keeps the mode the box already has), WIZARD_USER (default wizard), WAM_DIR, WAM_IMAGE, WAM_REF (branch to install), WIZARD_GITHUB=1, GITHUB_TOKEN.
set -euo pipefail
REPO="https://github.com/humanitylabs-org/wizard-app-manager"
WUSER="${WIZARD_USER:-wizard}"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
tty_ok() { [ -r /dev/tty ] && [ -w /dev/tty ] && (: </dev/tty) 2>/dev/null; }
MODE="${WIZARD_MODE:-auto}"
case "$MODE" in auto|alongside|fresh) ;; *) echo "WIZARD_MODE must be auto, alongside or fresh" >&2; exit 2 ;; esac
REF="${WAM_REF:-main}"
# An existing Hermes = the `hermes` command plus ~/.hermes/config.yaml, for the given user (default: this one).
has_hermes() {  # has_hermes [user]
  local h; h="$(getent passwd "${1:-$(id -un)}" | cut -d: -f6)"
  [ -n "$h" ] && [ -f "${HERMES_HOME:-$h/.hermes}/config.yaml" ] && { [ -x "$h/.local/bin/hermes" ] || { [ -z "${1:-}" ] && command -v hermes >/dev/null; }; }
}

# Which Wizard OS does this user already have? alongside | fresh | none. Re-running the command updates a box, and
# an update must keep the box's mode: a fresh box whose owner also runs Hermes on the host stays fresh.
wos_mode() {  # wos_mode user
  local h; h="$(getent passwd "$1" | cut -d: -f6)"
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
# Auto mode picks alongside only on a first install next to a Hermes; WIZARD_MODE=alongside forces it.
want_alongside() {  # want_alongside [user]
  [ "$MODE" = alongside ] && return 0
  [ "$MODE" = auto ] && has_hermes "${1:-}" && ! fresh_box "${1:-}"
}

# Root with an existing Hermes owned by the sudo user: install alongside as that user, not as a new wizard user.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && want_alongside "$SUDO_USER"; then
  say "Found Hermes for $SUDO_USER; installing Wizard OS alongside it as $SUDO_USER"
  SUID="$(id -u "$SUDO_USER")"
  exec sudo -iu "$SUDO_USER" env XDG_RUNTIME_DIR="/run/user/$SUID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$SUID/bus" \
    WIZARD_MODE=alongside WAM_IMAGE="${WAM_IMAGE:-}" WAM_DIR="${WAM_DIR:-}" WAM_REF="$REF" \
    WIZARD_PRIVATE_ACCESS="${WIZARD_PRIVATE_ACCESS:-tailscale}" bash -c "curl -fsSL '$REPO/raw/$REF/get.sh' | bash"
fi

if [ "$(id -u)" -eq 0 ]; then
  say "Preparing the server"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 python3-yaml gh sudo >/dev/null
  if ! command -v docker >/dev/null || [ "$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)" -lt 28 ] 2>/dev/null; then
    curl -fsSL https://get.docker.com | sh >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true

  if ! id "$WUSER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$WUSER"
    echo "created user $WUSER"
  fi
  usermod -aG docker "$WUSER"
  echo "$WUSER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-wizard-$WUSER"
  chmod 0440 "/etc/sudoers.d/90-wizard-$WUSER"
  # Root's SSH keys also let you in as the wizard user.
  if [ -s /root/.ssh/authorized_keys ] && [ ! -s "/home/$WUSER/.ssh/authorized_keys" ]; then
    install -d -m 700 -o "$WUSER" -g "$WUSER" "/home/$WUSER/.ssh"
    install -m 600 -o "$WUSER" -g "$WUSER" /root/.ssh/authorized_keys "/home/$WUSER/.ssh/authorized_keys"
  fi
  loginctl enable-linger "$WUSER"
  WUID="$(id -u "$WUSER")"
  for _ in $(seq 1 30); do [ -S "/run/user/$WUID/bus" ] && break; sleep 1; done

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
  exec sudo -iu "$WUSER" env XDG_RUNTIME_DIR="/run/user/$WUID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$WUID/bus" \
    WAM_IMAGE="${WAM_IMAGE:-}" WAM_DIR="${WAM_DIR:-}" WIZARD_GITHUB=0 WIZARD_MODE=fresh WAM_REF="$REF" \
    WIZARD_PRIVATE_ACCESS="${WIZARD_PRIVATE_ACCESS:-tailscale}" \
    bash -c "curl -fsSL '$REPO/raw/$REF/get.sh' | bash"
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
  elif ! want_alongside; then
    MODE=fresh
    has_hermes && echo "Found Hermes on this box, but Wizard OS is already installed fresh here: keeping fresh mode (WIZARD_MODE=alongside switches)."
  else
    MODE=alongside
    if tty_ok; then
      say "Found Hermes on this box (${HERMES_HOME:-$HOME/.hermes})"
      printf "Install Wizard OS alongside it? Your Hermes stays as it is and gets linked to the apps. [Y/n] " >/dev/tty
      read -r -t 120 ans </dev/tty || ans=y
      case "${ans:-y}" in n|N|no|NO) MODE=fresh ;; esac
    else
      echo "Found Hermes on this box: installing alongside it (set WIZARD_MODE=fresh to skip)."
    fi
  fi
fi
export WIZARD_MODE="$MODE"
DIR="${WAM_DIR:-$HOME/wizard-app-manager}"
if [ -d "$DIR/.git" ]; then
  # Stop on either failure: never run install.sh from a stale checkout.
  git -C "$DIR" fetch -q origin "$REF"
  git -C "$DIR" checkout -q -B "$REF" FETCH_HEAD
else
  git clone -q --branch "$REF" "$REPO" "$DIR"
fi
exec bash "$DIR/install.sh"
