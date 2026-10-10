#!/usr/bin/env bash
# Wizard OS, one command on a fresh Ubuntu/Debian server (run as root, or as a user with sudo):
#
#   curl -fsSL aiwizards.com/os.sh | bash
#
# What it does:
#   1. As root: installs Docker, git, gh and python3-yaml, and creates the `wizard` user (docker group,
#      passwordless sudo) that owns everything. Skipped when run by an existing sudo user.
#   2. Asks once for a GitHub login, only if a default app is still private (team only). Skip it and those
#      apps are left out; everything else installs. Set WIZARD_GITHUB=0 to never ask.
#   3. As the wizard user: clones the Wizard App Manager and runs install.sh (Runtipi with the Wizard image,
#      box-only, the `wam` command, then the default app in every parking spot).
#
# Options (environment): WIZARD_USER (default wizard), WAM_DIR, WAM_IMAGE, WIZARD_GITHUB=0, GITHUB_TOKEN.
set -euo pipefail
REPO="https://github.com/humanitylabs-org/wizard-app-manager"
WUSER="${WIZARD_USER:-wizard}"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
tty_ok() { [ -r /dev/tty ] && [ -w /dev/tty ] && (: </dev/tty) 2>/dev/null; }

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
  if [ "${WIZARD_GITHUB:-1}" != "0" ] && ! as_wizard gh auth status >/dev/null 2>&1 && tty_ok; then
    say "GitHub login (optional)"
    echo "Some default apps (the Wizard shell) are still private to the Humanity Labs team."
    printf "Log in to GitHub to include them? [Y/n] " >/dev/tty
    read -r -t 120 ans </dev/tty || ans=n
    if [ "${ans:-y}" != "n" ] && [ "${ans:-y}" != "N" ]; then
      as_wizard gh auth login --hostname github.com --git-protocol https --scopes read:packages </dev/tty >/dev/tty 2>&1 || echo "GitHub login skipped"
    fi
  fi

  say "Installing Wizard OS as $WUSER"
  exec sudo -iu "$WUSER" env XDG_RUNTIME_DIR="/run/user/$WUID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$WUID/bus" \
    WAM_IMAGE="${WAM_IMAGE:-}" WAM_DIR="${WAM_DIR:-}" WIZARD_GITHUB=0 \
    bash -c "curl -fsSL '$REPO/raw/main/get.sh' | bash"
fi

# ---- as the wizard user (or any user with sudo) ----
command -v git >/dev/null || sudo apt-get install -y -qq git >/dev/null
if [ "${WIZARD_GITHUB:-1}" != "0" ] && command -v gh >/dev/null && ! gh auth status >/dev/null 2>&1 && tty_ok; then
  printf "Some default apps are still private to the team. Log in to GitHub to include them? [Y/n] " >/dev/tty
  read -r -t 120 ans </dev/tty || ans=n
  if [ "${ans:-y}" != "n" ] && [ "${ans:-y}" != "N" ]; then
    gh auth login --hostname github.com --git-protocol https --scopes read:packages </dev/tty >/dev/tty 2>&1 || true
  fi
fi
DIR="${WAM_DIR:-$HOME/wizard-app-manager}"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only -q; else git clone -q "$REPO" "$DIR"; fi
exec bash "$DIR/install.sh"
