#!/usr/bin/env bash
# Install (or switch to) the Wizard App Manager.
#
# The Wizard App Manager is Runtipi with its app store replaced by the Wizard App Index. It runs as the same
# containers Runtipi uses, so this script installs stock Runtipi's base (if missing) and then swaps the main
# image for the Wizard build. Apps, app data and settings stay where they are.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNTIPI_ROOT="${RUNTIPI_ROOT:-$HOME/runtipi}"
IMAGE="${WAM_IMAGE:-ghcr.io/humanitylabs-org/wizard-app-manager:latest}"

# Fail before private access, Docker, files or services change. Validation needs no YAML dependency.
(cd "$HERE" && python3 -m wam.release_pins)

# Default private-access provider is native Tailscale. Existing tailnets are preserved; Cloudflare is opt-in.
bash "$HERE/scripts/setup-private-access.sh"

if [ ! -f "$RUNTIPI_ROOT/.env" ]; then
  echo "No install at $RUNTIPI_ROOT; installing the base (Runtipi) first, box-only"
  # Box-only from the very first start: dashboard and proxy listen on loopback, never on the public interface.
  # Reach them later through Cloudflare Tunnel or Tailscale.
  mkdir -p "$RUNTIPI_ROOT/user-config"
  [ -f "$RUNTIPI_ROOT/user-config/tipi-compose.yml" ] || cat > "$RUNTIPI_ROOT/user-config/tipi-compose.yml" <<'YML'
# Wizard OS: box-only. Nothing listens on the public interface; use Cloudflare Tunnel or Tailscale to reach it.
services:
  runtipi-reverse-proxy:
    ports: !override
      - 127.0.0.1:8480:80
      - 127.0.0.1:8443:443
YML
  ENVF="$(mktemp)"
  printf 'NGINX_PORT=8480\nNGINX_PORT_SSL=8443\nINTERNAL_IP=127.0.0.1\n' > "$ENVF"
  # A private temp dir, never a fixed /tmp name: a leftover /tmp/runtipi-setup.sh owned by another user (root, from
  # an earlier install) can't be overwritten in sticky /tmp, and curl then fails with exit 23 before Runtipi exists.
  SETUP_DIR="$(mktemp -d)"
  curl -fsSL https://setup.runtipi.io -o "$SETUP_DIR/runtipi-setup.sh"
  (cd "$(dirname "$RUNTIPI_ROOT")" && sudo bash "$SETUP_DIR/runtipi-setup.sh" --env-file "$ENVF")
  rm -rf "$ENVF" "$SETUP_DIR"
  # Runtipi's setup runs as root; hand the install to this user (not data/, the database's own files).
  sudo chown "$(id -u):$(id -g)" "$RUNTIPI_ROOT"
  sudo find "$RUNTIPI_ROOT" -mindepth 1 -maxdepth 1 ! -name data -exec chown -R "$(id -u):$(id -g)" {} +
fi
# The Python that runs wam needs PyYAML. Do not trust whichever python3 is first on PATH: a Hermes agent puts its own
# bundled Python (no PyYAML) first for the commands it runs, and wam would then fail mid-install. Pick the first
# python3 that imports yaml (PATH first, then the system one); otherwise install python3-yaml for the system Python.
pick_py() {
  local c
  for c in "${WIZARD_PYTHON:-}" "$(command -v python3 2>/dev/null || true)" /usr/bin/python3 /bin/python3; do
    [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import yaml' 2>/dev/null && { echo "$c"; return 0; }
  done
  return 1
}
PY="$(pick_py)" || { sudo apt-get install -y python3-yaml >/dev/null; PY="$(pick_py)"; } \
  || { echo "Wizard OS needs Python 3 with PyYAML (apt-get install -y python3-yaml), then run the command again." >&2; exit 1; }
command -v git >/dev/null || sudo apt-get install -y git
# An existing sudo user who isn't in the docker group yet (Runtipi's setup installs Docker but doesn't add
# the user) gets "permission denied" from plain `docker`. Use sudo for Docker then.
DOCKER=(docker)
docker info >/dev/null 2>&1 || DOCKER=(sudo docker)
# Shared, read-only copy of apps.json for app containers (the Wizard shell mounts it).
mkdir -p "$RUNTIPI_ROOT/app-data/wizard-os" 2>/dev/null || { sudo mkdir -p "$RUNTIPI_ROOT/app-data/wizard-os" && sudo chown "$(id -u):$(id -g)" "$RUNTIPI_ROOT/app-data/wizard-os"; }

# 1) Run the Wizard App Manager image instead of Runtipi's (user-config override; survives Runtipi's own files).
mkdir -p "$RUNTIPI_ROOT/user-config"
OVR="$RUNTIPI_ROOT/user-config/tipi-compose.yml"
[ -f "$OVR" ] && cp "$OVR" "$OVR.bak-$(date -u +%Y%m%dT%H%M%SZ)"
# Preserve owner settings and Compose tags (!override/!reset), including box-only bindings.
(cd "$HERE" && "$PY" -m wam.release_pins "$OVR" "$IMAGE")
# wam#95: the runtime records (.env, database files, app records) can survive while the base containers are gone
# (Docker reinstalled, containers removed). Start the database and queue from those records before anything talks to
# them; --no-recreate leaves running ones exactly as they are, so a rerun on a healthy box changes nothing.
(cd "$RUNTIPI_ROOT" && "${DOCKER[@]}" compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-recreate --wait runtipi-db runtipi-queue)
# Apps the owner left running, read before the App Manager starts: its status sync marks an app whose containers are
# gone as stopped, which would lose that intent. `wam recover` restarts these; apps the owner stopped stay stopped.
RUNNING_APPS="$("${DOCKER[@]}" exec runtipi-db psql -U tipi -d tipi -Atc "SELECT app_name || ':' || app_store_slug FROM app WHERE status = 'running'" 2>/dev/null || true)"
# Point the existing Wizard store at the index (older installs had a local git store here).
"${DOCKER[@]}" exec runtipi-db psql -U tipi -d tipi -qc "UPDATE app_store SET url='https://apps.aiwizards.com', hash=encode(sha256('https://apps.aiwizards.com'::bytea),'hex'), name='Wizard Apps', enabled=true WHERE slug='wizardapps'" >/dev/null 2>&1 || true
if ! "${DOCKER[@]}" image inspect "$IMAGE" >/dev/null 2>&1; then "${DOCKER[@]}" pull "$IMAGE"; fi
RECREATE=()
if [ -n "${WIZARD_RELEASE_PINS:-}" ]; then
  # The released manager image consumes its usual index protocol; start the local pin view first.
  # Pins are atomically replaced; recreate the file mount and clear the manager's index cache.
  RECREATE=(--force-recreate)
  (cd "$RUNTIPI_ROOT" && "${DOCKER[@]}" compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --force-recreate --wait wizard-release-index)
fi
(cd "$RUNTIPI_ROOT" && "${DOCKER[@]}" compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d "${RECREATE[@]}" --no-deps runtipi)
# The reverse proxy answers the health check below; recreate it only when it is missing (wam#95).
(cd "$RUNTIPI_ROOT" && "${DOCKER[@]}" compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-recreate --no-deps runtipi-reverse-proxy)

# 2) wam command + box-only API service
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.wizard-os"
cat > "$HOME/.local/bin/wam" <<EOF
#!/usr/bin/env bash
export WIZARD_RELEASE_PINS="\${WIZARD_RELEASE_PINS-${WIZARD_RELEASE_PINS:-}}"
cd "$HERE" && exec "$PY" -m wam "\$@"
EOF
chmod +x "$HOME/.local/bin/wam"

cat > "$HOME/.config/systemd/user/wizard-app-manager-api.service" <<EOF
[Unit]
Description=wam API (127.0.0.1:7790; changes need ~/.wizard-os/api-token)
After=network-online.target

[Service]
Environment=RUNTIPI_ROOT=$RUNTIPI_ROOT
Environment=WIZARD_RELEASE_PINS=${WIZARD_RELEASE_PINS:-}
WorkingDirectory=$HERE
ExecStart=$PY -m wam serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF

# Retire pieces the index-backed store replaced: per-box sync timer, local git store server, skin proxy.
systemctl --user disable --now wizard-app-manager-sync.timer wizard-app-manager.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/wizard-app-manager-sync."{service,timer} "$HOME/.config/systemd/user/wizard-app-manager.service"
"${DOCKER[@]}" rm -f wizard-store wizard-skin >/dev/null 2>&1 || true
rm -f "$RUNTIPI_ROOT/traefik/dynamic/wizard-skin.yml"

loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now wizard-app-manager-api.service
systemctl --user restart wizard-app-manager-api.service
# Updates (wam/updates.py): Automatic unless the owner chose Ask me. Every install and rerun makes the nightly
# update timer match that mode; it never writes the owner's choice, so an explicit Ask me is kept.
"$HOME/.local/bin/wam" updates --ensure-timer >/dev/null 2>&1 || echo "note: could not set up the nightly update timer (wam updates --ensure-timer)"

# Every required step below either succeeds or is recorded here; the summary at the end reports exactly what
# happened (wizard-os#28). FAILED = Wizard OS is not usable as installed; DEGRADED = usable, something is missing.
FAILED=()
DEGRADED=()
OUTCOME="$HOME/.wizard-os/install-outcome.json"

# Complete OS-owned skill, tooling and assets, never a private Hermes skill.
"$HOME/.local/bin/wam" builder-bundle install || FAILED+=("could not install the stock Wizard builder bundle")
if [ "${WIZARD_MODE:-fresh}" = fresh ] && [ ${#FAILED[@]} -eq 0 ]; then
  "$HOME/.local/bin/wam" builder-bundle fresh || FAILED+=("could not provision stock Hermes builder discovery and mount")
fi

healthy=0
for _ in $(seq 1 "${WIZARD_HEALTH_TRIES:-60}"); do curl -fsS -o /dev/null "http://127.0.0.1:$(grep -E '^NGINX_PORT=' "$RUNTIPI_ROOT/.env" | cut -d= -f2 || echo 80)/api/health" 2>/dev/null && { healthy=1; break; }; sleep 2; done
[ "$healthy" = 1 ] || FAILED+=("the App Manager did not answer its health check within 2 minutes")
# wam#95: installed apps whose containers are gone (records kept) are not reinstalled by bootstrap. Start the Wizard
# shell / Agent Service and every app the owner left running; owner-stopped apps stay stopped. No-op when all run.
if [ "$healthy" = 1 ]; then
  REC_ARGS=()
  while IFS= read -r u; do [ -z "$u" ] || REC_ARGS+=(--running "$u"); done <<<"$RUNNING_APPS"
  if rec="$("$HOME/.local/bin/wam" recover ${REC_ARGS[@]+"${REC_ARGS[@]}"})"; then
    split="$(printf '%s' "$rec" | python3 -c 'import json,sys
try: r = json.load(sys.stdin)
except ValueError: print("\n(unreadable recover result)"); raise SystemExit
f, core = r.get("failed") or {}, set(r.get("core") or [])
print("; ".join(f"{k}: {v}" for k, v in f.items() if k in core))
print("; ".join(f"{k}: {v}" for k, v in f.items() if k not in core))' 2>/dev/null || printf '\n(unreadable recover result)')"
    rec_core="$(printf '%s\n' "$split" | sed -n 1p)"
    rec_rest="$(printf '%s\n' "$split" | sed -n 2p)"
    [ -z "$rec_core" ] || FAILED+=("the Wizard shell / Agent Service containers could not be restarted: $rec_core")
    [ -z "$rec_rest" ] || DEGRADED+=("some installed apps could not be restarted: $rec_rest")
  else
    DEGRADED+=("could not restart installed apps whose containers are missing (wam recover failed)")
  fi
fi
"$HOME/.local/bin/wam" doctor --fix >/dev/null 2>&1 || true   # repairs what it can; the next run is the verdict
"$HOME/.local/bin/wam" apps >/dev/null || DEGRADED+=("could not write the installed-app list (wam apps)")
# The verdict is after bootstrap: a fresh agent cannot answer before it exists.

# The generic `wizard` CLI (apps, tools, call, link, unlink, doctor). Vendored from wizard-os tools/wizard.
WIZ_CLI="$HOME/.local/bin/wizard"
if ! PYTHON="$PY" sh "$HERE/wizard-cli/install_wizard_cli.sh" >/dev/null 2>&1; then
  WIZ_CLI="$HOME/.local/bin/wizard-os"
  PYTHON="$PY" sh "$HERE/wizard-cli/install_wizard_cli.sh" --name wizard-os || { WIZ_CLI=""; FAILED+=("could not install the wizard command"); }
fi
# Same interpreter as wam: under an agent, `env python3` can be the agent's bundled Python, which lacks PyYAML, and
# `wizard link` would then misread the owner's config.yaml as empty.
[ -z "$WIZ_CLI" ] || sed -i "1s|.*|#!$PY|" "$WIZ_CLI"

# Run by a Hermes agent (get.sh sets WIZARD_AGENT_SAFE=1 when this runs under a Hermes gateway, e.g. a chat asked its
# agent to install Wizard OS)? Restarting the gateway now would kill the agent running this script and cut off its
# reply. Then everything else finishes first, and the restart is scheduled detached, after the summary.
# wizard-install: defers-hermes-restart   (get.sh checks this marker before an agent-run install)
RESTART_NOW=1
[ "${WIZARD_AGENT_SAFE:-0}" = 1 ] && RESTART_NOW=""
RESTART_LATER=""
schedule_hermes_restart() {  # outside this process tree (its own systemd unit), so it survives the restart it does
  local delay="${WIZARD_RESTART_DELAY:-20}" hermes log="$HOME/.wizard-os/hermes-restart.log" job
  case "$delay" in ''|*[!0-9]*) delay=20 ;; esac
  hermes="${WIZARD_HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}"
  job="sleep 2; echo \"\$(date -u +%FT%TZ) restarting the Hermes gateway for Wizard OS\"; HERMES_HOME='${HERMES_HOME:-$HOME/.hermes}' '$hermes' gateway restart; sleep 20; '$HOME/.local/bin/wizard' doctor"
  local -a run=(systemd-run --user)
  [ "$(id -u)" -eq 0 ] && run=(systemd-run)
  if command -v systemd-run >/dev/null && "${run[@]}" --quiet --collect --unit "wizard-hermes-restart-$(date +%s)" \
      --on-active="${delay}s" --setenv=HOME="$HOME" --setenv=PATH="$PATH" \
      /bin/bash -c "exec >>'$log' 2>&1; $job" >/dev/null 2>&1; then
    return 0
  fi
  # No systemd: a detached session of its own.
  setsid nohup /bin/bash -c "sleep $delay; exec >>'$log' 2>&1; $job" </dev/null >/dev/null 2>&1 &
}

# 2b) Alongside an existing Hermes (WIZARD_MODE=alongside, set by get.sh): the host's Hermes fills the agent spot
#     and is linked to the apps; the hermes-agent app is never installed. `wizard unlink` reverses the link.
#     A failed link rolls itself back (Hermes is left exactly as it was) and fails the install.
#     Never on a failed install: linking points Hermes at a gateway that isn't up and restarts it (PR19 review).
if [ "${WIZARD_MODE:-fresh}" = "alongside" ] && [ ${#FAILED[@]} -gt 0 ]; then
  echo "Alongside mode: the install already failed, so your Hermes (${HERMES_HOME:-$HOME/.hermes}) was not touched."
elif [ "${WIZARD_MODE:-fresh}" = "alongside" ]; then
  echo "Alongside mode: Wizard OS runs next to your Hermes (${HERMES_HOME:-$HOME/.hermes}); nothing in it is moved."
  # Active threads use the supported Hermes HTTP session reader. Keep archive/database sharing opt-in.
  if ! "$HOME/.local/bin/wam" alongside ${WIZARD_SHELL_PORT:+--shell-port "$WIZARD_SHELL_PORT"} >/dev/null; then
    FAILED+=("could not record alongside mode (wam alongside)")
  elif ! "$HOME/.local/bin/wizard" link ${RESTART_NOW:+--restart} ${WIZARD_PLUGIN_REF:+--plugin-ref "$WIZARD_PLUGIN_REF"} \
      ${WIZARD_PLUGIN_SOURCE:+--plugin-source "$WIZARD_PLUGIN_SOURCE"}; then
    FAILED+=("'wizard link' failed (its message is above); your Hermes was left as it was. Fix it, then re-run the install")
  elif [ -z "$RESTART_NOW" ]; then
    RESTART_LATER=1   # agent-safe: the gateway restarts after this script has printed its summary
  else
    # Hermes' API server answers once the gateway has restarted with its key.
    ORIGIN="$("$HOME/.local/bin/wam" alongside --detect 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_origin"])' || echo http://127.0.0.1:8642)"
    up=0
    for _ in $(seq 1 45); do curl -fsS -o /dev/null "$ORIGIN/health" 2>/dev/null && { up=1; break; }; sleep 2; done
    [ "$up" = 1 ] || DEGRADED+=("your Hermes API ($ORIGIN) did not answer after the restart")
  fi
fi

# 3) Fill every empty parking spot (shell, agent service, agent, files, ...) with its default app from the index.
#    Spots with no published default yet are skipped. WAM_NO_BOOTSTRAP=1 installs the App Manager only.
if [ "${WAM_NO_BOOTSTRAP:-0}" != "1" ] && [ "$healthy" = 1 ]; then
  if boot="$("$HOME/.local/bin/wam" bootstrap)"; then
    # The apps in the shell / agent-service spots are the only way to use Wizard OS: if one of them failed the
    # install failed. Any other default app that failed leaves a usable box (degraded). Line 1 = failed core apps,
    # line 2 = the rest; bootstrap only installs the defaults named in wam/defaults.json.
    split="$(printf '%s' "$boot" | python3 -c 'import json,sys
core = {(v or {}).get("app") for k, v in json.load(open(sys.argv[1]))["slots"].items() if k in ("shell", "agent-service")}
try: f = json.load(sys.stdin).get("failed") or {}
except ValueError: print("\n(unreadable bootstrap result)"); raise SystemExit
print("; ".join(f"{k}: {v}" for k, v in f.items() if k in core))
print("; ".join(f"{k}: {v}" for k, v in f.items() if k not in core))' "$HERE/wam/defaults.json" 2>/dev/null \
      || printf '\n(unreadable bootstrap result)')"
    core_failed="$(printf '%s\n' "$split" | sed -n 1p)"
    failed_apps="$(printf '%s\n' "$split" | sed -n 2p)"
    [ -z "$core_failed" ] || FAILED+=("the Wizard shell / Agent Service did not install, so Wizard OS can't be used: $core_failed")
    [ -z "$failed_apps" ] || DEGRADED+=("some default apps did not install: $failed_apps")
  else
    DEGRADED+=("default apps could not be installed (wam bootstrap failed)")
  fi
fi
# Alongside: (re)install the App Store data service for the installed shell on every run, so a rerun repairs a
# failed/outdated one (bootstrap only sets it up when it installs the shell itself).
if [ "${WIZARD_MODE:-fresh}" = "alongside" ] && [ "$healthy" = 1 ]; then
  sd="$("$HOME/.local/bin/wam" store-data 2>/dev/null || true)"
  printf '%s' "$sd" | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin).get("state") not in ("ready", "skipped"))' 2>/dev/null \
    || DEGRADED+=("the App Store data service could not be set up (wam store-data)")
fi
PENDING=()
if [ "${WIZARD_MODE:-fresh}" = "alongside" ] && [ ${#FAILED[@]} -eq 0 ]; then
  if [ -n "$RESTART_LATER" ]; then
    # Hermes has not loaded the link yet (its restart comes last), so FAIL lines here may only mean "not yet".
    echo; "$HOME/.local/bin/wizard" doctor || PENDING+=("wizard doctor: some checks pass only after the Hermes restart; run 'wizard doctor' again in a minute")
  else
    echo; "$HOME/.local/bin/wizard" doctor || DEGRADED+=("wizard doctor reported FAIL lines (above)")
  fi
fi
if [ "${WIZARD_MODE:-fresh}" = fresh ] && [ "${WAM_NO_BOOTSTRAP:-0}" != 1 ] && [ "$healthy" = 1 ]; then
  "$HOME/.local/bin/wam" builder-bundle fresh --restart || FAILED+=("stock Hermes could not adopt builder discovery/mount")
fi
"$HOME/.local/bin/wam" doctor || DEGRADED+=("wam doctor reported FAIL lines (above)")

if [ ${#FAILED[@]} -gt 0 ]; then status=failed; elif [ ${#DEGRADED[@]} -gt 0 ]; then status=degraded; else status=ok; fi
python3 - "$OUTCOME" "$status" "${WIZARD_MODE:-fresh}" "${#FAILED[@]}" ${FAILED[@]+"${FAILED[@]}"} ${DEGRADED[@]+"${DEGRADED[@]}"} <<'PY' || true
import json, os, sys, time
path, status, mode, nf, rest = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5:]
rec = {"status": status, "mode": mode, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "failed": rest[:nf], "degraded": rest[nf:]}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(rec, f, indent=2); f.write("\n")
os.replace(tmp, path)
PY

echo
"$HOME/.local/bin/wam" slots || true
echo
# 4) Private links (wizard-app-manager#18): when this box is on the owner's Tailscale, each installed app page gets its
#    own https://<box>.<tailnet>.ts.net:<port> with stock `tailscale serve`, and the links are printed here.
#    Skipped on a failed install: there is nothing usable to link to.
if [ "$status" != failed ] && [ "${WIZARD_PRIVATE_ACCESS:-tailscale}" = "tailscale" ]; then
  "$HOME/.local/bin/wam" links || true
  echo
  # 5) The Troubleshooter (wizard-os#93): the way back when chat is down. A native user service, not an app, so it keeps
  #    working (and can restart things) when Runtipi, the shell, the Agent Service or Hermes are broken. Mounted at
  #    /troubleshoot/ on the shell's addresses (so it runs after `wam links`); the guardian only reports. The release
  #    pins its commit. A hand-installed one is left alone; WIZARD_TROUBLESHOOTER=0 skips it.
  if ! "$HOME/.local/bin/wam" troubleshooter; then
    echo "note: the Troubleshooter is not fully set up ('wam troubleshooter' shows why; ~/.wizard-os/troubleshooter.json)"
  fi
  echo
fi
[ ${#PENDING[@]} -eq 0 ] || { printf 'Pending: %s\n' "${PENDING[@]}"; echo; }
if [ -n "$RESTART_LATER" ] && [ "$status" != failed ]; then
  if [ "${WIZARD_OUTER_INSTALL:-0}" = 1 ]; then
    echo deferred > "$HOME/.wizard-os/hermes-restart.pending"
    echo "Hermes restart is pending the outer installer's release verification."
  else
    schedule_hermes_restart
    echo "Hermes will restart in ~${WIZARD_RESTART_DELAY:-20}s to load Wizard OS (this chat pauses briefly; send any"
    echo "message after a minute). The restart and a fresh 'wizard doctor' are logged in ~/.wizard-os/hermes-restart.log."
    echo
  fi
fi
case "$status" in
  ok)
    echo "Wizard OS is installed (box-only: nothing listens on the public interface)."
    echo "Next: run 'wam slots' or 'wam apps' as $(id -un). Re-run the same curl command any time to update." ;;
  degraded)
    echo "Wizard OS is installed, but not everything is working:"
    printf '  - %s\n' ${DEGRADED[@]+"${DEGRADED[@]}"}
    echo "Re-run the same curl command to retry; 'wam doctor' shows the current state."
    # Unattended image builds keep working by default; WIZARD_STRICT=1 makes a degraded install exit non-zero.
    [ "${WIZARD_STRICT:-0}" != 1 ] || exit 3 ;;
  failed)
    echo "Wizard OS did NOT finish installing:" >&2
    printf '  - %s\n' ${FAILED[@]+"${FAILED[@]}"} >&2
    [ ${#DEGRADED[@]} -eq 0 ] || printf '  - %s\n' ${DEGRADED[@]+"${DEGRADED[@]}"} >&2
    if [ -f "$OUTCOME" ]; then
      echo "Re-run the same curl command once it is fixed. Recorded in $OUTCOME." >&2
    else
      echo "Re-run the same curl command once it is fixed." >&2
    fi
    exit 1 ;;
esac
