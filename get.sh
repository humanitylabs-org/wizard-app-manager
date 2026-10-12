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
#
# Release pins (wizard-os#27): the install follows one Wizard OS release file, wizard-os/releases/<version>.json,
# which names the App Manager commit and the image digests. WIZARD_RELEASE=<version> picks one; without it the
# release named in wizard-os/releases/stable is used. Re-running the command with no WIZARD_RELEASE updates to the
# current stable release; WIZARD_RELEASE=<version> keeps a box on that version. WAM_REF=<branch|sha> is the
# developer override: it installs that App Manager ref unpinned. Afterwards the installer reads back what actually
# runs (App Manager commit and image, shell image, default app versions) and records it in ~/.wizard-os/release.json.
# WIZARD_STRICT=1 makes any drift from the release exit non-zero.
set -euo pipefail
REPO="${WAM_REPO:-https://github.com/humanitylabs-org/wizard-app-manager}"
RELEASE_BASE="${WIZARD_RELEASE_BASE:-https://raw.githubusercontent.com/humanitylabs-org/wizard-os/main/releases}"
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
    WIZARD_MODE=alongside WAM_IMAGE="${WAM_IMAGE:-}" WAM_DIR="${WAM_DIR:-}" WAM_REF="${WAM_REF:-}" \
    WIZARD_RELEASE="${WIZARD_RELEASE:-}" WIZARD_RELEASE_BASE="${WIZARD_RELEASE_BASE:-}" WIZARD_STRICT="${WIZARD_STRICT:-}" \
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
    WAM_IMAGE="${WAM_IMAGE:-}" WAM_DIR="${WAM_DIR:-}" WIZARD_GITHUB=0 WIZARD_MODE=fresh WAM_REF="${WAM_REF:-}" \
    WIZARD_RELEASE="${WIZARD_RELEASE:-}" WIZARD_RELEASE_BASE="${WIZARD_RELEASE_BASE:-}" WIZARD_STRICT="${WIZARD_STRICT:-}" \
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

# Which release? (wizard-os#27) WAM_REF set = developer override, unpinned. Otherwise WIZARD_RELEASE, else the
# stable pointer. No stable pointer published yet = the previous behaviour (App Manager main), said out loud.
PINS=""
if [ -z "${WAM_REF:-}" ]; then
  REL="${WIZARD_RELEASE:-}"
  [ -n "$REL" ] || REL="$(curl -fsSL "$RELEASE_BASE/stable" 2>/dev/null | tr -d ' \t\r\n' || true)"
  if [ -n "$REL" ]; then
    case "$REL" in *[!0-9A-Za-z.+-]*|.*) echo "WIZARD_RELEASE '$REL' is not a version" >&2; exit 2 ;; esac
    mkdir -p "$HOME/.wizard-os"
    PINS="$HOME/.wizard-os/release-pins.json"
    curl -fsSL "$RELEASE_BASE/$REL.json" -o "$PINS.tmp" || { echo "Could not fetch Wizard OS release $REL ($RELEASE_BASE/$REL.json)" >&2; exit 1; }
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
        return ""
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
for k, v in (("PIN_WAM_COMMIT", sha), ("PIN_WAM_IMAGE", image("wizard-app-manager")),
             ("PIN_SHELL_IMAGE", image("wizard-shell"))):
    print(f"{k}={shlex.quote(v)}")
PY
)" || { rm -f "$PINS.tmp"; exit 2; }
    eval "$PIN_ENV"
    mv "$PINS.tmp" "$PINS"
    REF="$PIN_WAM_COMMIT"
    if [ -n "${WAM_IMAGE:-}" ] && [ "$WAM_IMAGE" != "$PIN_WAM_IMAGE" ]; then
      echo "note: WAM_IMAGE=$WAM_IMAGE overrides the release's App Manager image"
    else
      [ -z "$PIN_WAM_IMAGE" ] || export WAM_IMAGE="$PIN_WAM_IMAGE"
    fi
    if [ -n "${WIZARD_SHELL_IMAGE:-}" ] && [ "$WIZARD_SHELL_IMAGE" != "$PIN_SHELL_IMAGE" ]; then
      echo "note: WIZARD_SHELL_IMAGE=$WIZARD_SHELL_IMAGE overrides the release's shell image"
    else
      [ -z "$PIN_SHELL_IMAGE" ] || export WIZARD_SHELL_IMAGE="$PIN_SHELL_IMAGE"
    fi
    say "Installing Wizard OS $REL (App Manager ${REF:0:12})"
  else
    echo "note: couldn't read the Wizard OS stable release ($RELEASE_BASE/stable); installing the App Manager's main branch (unpinned)."
  fi
fi

if [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then   # a pinned commit: fetch exactly it, detached, and prove it
  if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch -q origin "$REF"
  else
    git clone -q "$REPO" "$DIR"
  fi
  git -C "$DIR" checkout -q --detach "$REF"
  [ "$(git -C "$DIR" rev-parse HEAD)" = "$REF" ] || { echo "App Manager checkout is not $REF" >&2; exit 1; }
elif [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$REF" && git -C "$DIR" checkout -q -B "$REF" FETCH_HEAD
else
  git clone -q --branch "$REF" "$REPO" "$DIR"
fi
[ -n "$PINS" ] || exec bash "$DIR/install.sh"

rc=0
bash "$DIR/install.sh" || rc=$?
# Read back what actually runs and compare it with the release (always, even after a failed install).
vrc=0
python3 - "$PINS" "$DIR" "${WIZARD_MODE:-fresh}" "$HOME/.wizard-os/release.json" "${WIZARD_STRICT:-0}" <<'PY' || vrc=$?
import json, os, re, subprocess, sys, time
pins_path, wam_dir, mode, out_path, strict = sys.argv[1:6]
rel = json.load(open(pins_path))
checks = []
def check(name, expected, observed, core):
    checks.append({"name": name, "expected": expected, "observed": observed, "ok": expected == observed, "core": core})
def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
docker = ["docker"] if subprocess.run(["docker", "info"], capture_output=True).returncode == 0 else ["sudo", "-n", "docker"]
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
check("wizard-app-manager commit", rel["components"]["wizard-app-manager"]["commit"], run("git", "-C", wam_dir, "rev-parse", "HEAD"), True)
digest_check("wizard-app-manager", "runtipi", True)
try:
    apps = {a["id"]: a for a in json.load(open(os.path.expanduser("~/.wizard-os/apps.json"))).get("apps", [])}
except (OSError, ValueError):
    apps = {}
names = run(*docker, "ps", "--format", "{{.Names}}").split()
shell_app = ((rel.get("default_apps") or {}).get("shell") or {}).get("app", "wizard-shell")
digest_check("wizard-shell", next((n for n in names if shell_app in n), ""), False)
for slot, d in (rel.get("default_apps") or {}).items():
    if not d or (mode == "alongside" and d.get("alongside")):
        continue
    a = apps.get(d["app"])
    check(f"default app {slot}", f"{d['app']} {d.get('index_version')}",
          f"{d['app']} {a.get('version')}" if a else f"{d['app']} not installed", False)
core_bad = [c for c in checks if c["core"] and not c["ok"]]
drift = [c for c in checks if not c["core"] and not c["ok"]]
status = "mismatch" if core_bad else ("drift" if drift else "pinned")
rec = {"release": rel.get("version"), "mode": mode, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "status": status, "checks": checks}
tmp = out_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(rec, f, indent=2); f.write("\n")
os.replace(tmp, out_path)
print(f"\nWizard OS {rel.get('version')}: what runs vs the release")
for c in checks:
    print(f"  {'PASS' if c['ok'] else ('FAIL' if c['core'] else 'DRIFT')}  {c['name']}: {c['observed']}"
          + ("" if c["ok"] else f" (release: {c['expected']})"))
print(f"  -> {status}; recorded in {out_path}")
sys.exit(1 if core_bad else (3 if drift and strict == "1" else 0))
PY
[ "$rc" -eq 0 ] || exit "$rc"
exit "$vrc"
