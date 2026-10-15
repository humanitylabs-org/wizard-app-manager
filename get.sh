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
# WIZARD_STRICT=1 makes any drift from the release exit non-zero, and refuses unpinned installs (incl. WAM_REF).
# Alongside mode applies the shell pin (and updates a shell left on an older digest); a fresh box takes the shell
# the index serves, so there a different shell digest is reported as DRIFT until the App Manager can pin it.
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
    WAM_REPO="$REPO" WIZARD_UX_IMAGE="${WIZARD_UX_IMAGE:-}" WIZARD_AGENT_SERVICE_IMAGE="${WIZARD_AGENT_SERVICE_IMAGE:-}" WIZARD_SHELL_IMAGE="${WIZARD_SHELL_IMAGE:-}" \
    WIZARD_PRIVATE_ACCESS="${WIZARD_PRIVATE_ACCESS:-tailscale}" bash -c "set -o pipefail; curl -fsSL '$REPO/raw/$REF/get.sh' | bash"
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
    WAM_REPO="$REPO" WIZARD_UX_IMAGE="${WIZARD_UX_IMAGE:-}" WIZARD_AGENT_SERVICE_IMAGE="${WIZARD_AGENT_SERVICE_IMAGE:-}" WIZARD_SHELL_IMAGE="${WIZARD_SHELL_IMAGE:-}" WIZARD_PRIVATE_ACCESS="${WIZARD_PRIVATE_ACCESS:-tailscale}" \
    bash -c "set -o pipefail; curl -fsSL '$REPO/raw/$REF/get.sh' | bash"
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
if [ -z "$PINS" ]; then
  [ -z "$UNPINNED" ] || record_unpinned
  exec bash "$DIR/install.sh"
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
[ "$rc" -eq 0 ] || exit "$rc"
exit "$vrc"
