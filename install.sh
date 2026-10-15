#!/usr/bin/env bash
# Install (or switch to) the Wizard App Manager.
#
# The Wizard App Manager is Runtipi with its app store replaced by the Wizard App Index. It runs as the same
# containers Runtipi uses, so this script installs stock Runtipi's base (if missing) and then swaps the main
# image for the Wizard build. Apps, app data and settings stay where they are.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNTIPI_ROOT="${RUNTIPI_ROOT:-$HOME/runtipi}"
# Alongside foundation snapshot: existing published images, no feature-source changes.
# Pins are set here so get.sh's sudo/user handoff cannot drop them.
if [ "${WIZARD_MODE:-fresh}" = alongside ]; then
  export WAM_IMAGE="${WAM_IMAGE:-ghcr.io/humanitylabs-org/wizard-app-manager@sha256:8afef790d701d8bdd6728187f943c5d4de50cd09da5dc29f245f636af32c1117}"
  export WIZARD_UX_IMAGE="${WIZARD_UX_IMAGE:-ghcr.io/humanitylabs-org/wizard-ux@sha256:f662feab8a9eab66340a7c9426e4279ee1ee764615fd2ce5cfbdd048a4ee1bf0}"
  export WIZARD_AGENT_SERVICE_IMAGE="${WIZARD_AGENT_SERVICE_IMAGE:-ghcr.io/humanitylabs-org/wizard-agent-service@sha256:6d3eae8ca20b41b1973fbd027efdc281aa59abea6deba96091e0a0243dd04f48}"
fi
IMAGE="${WAM_IMAGE:-ghcr.io/humanitylabs-org/wizard-app-manager:latest}"

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
  curl -fsSL https://setup.runtipi.io -o /tmp/runtipi-setup.sh
  (cd "$(dirname "$RUNTIPI_ROOT")" && sudo bash /tmp/runtipi-setup.sh --env-file "$ENVF")
  rm -f "$ENVF"
  # Runtipi's setup runs as root; hand the install to this user (not data/, the database's own files).
  sudo chown "$(id -u):$(id -g)" "$RUNTIPI_ROOT"
  sudo find "$RUNTIPI_ROOT" -mindepth 1 -maxdepth 1 ! -name data -exec chown -R "$(id -u):$(id -g)" {} +
fi
python3 -c 'import yaml' 2>/dev/null || sudo apt-get install -y python3-yaml
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
# Text edit (the file may use compose tags like !override that a YAML round-trip would drop).
python3 - "$OVR" "$IMAGE" <<'PY'
import re, sys, pathlib
p, image = pathlib.Path(sys.argv[1]), sys.argv[2]
s = p.read_text() if p.is_file() else ""
lines = s.splitlines()
if not any(re.match(r"^services:\s*$", l) for l in lines):
    lines += ["services:"]
i = next((n for n, l in enumerate(lines) if re.match(r"^  runtipi:\s*$", l)), None)
if i is None:
    lines += ["  runtipi:", f"    image: {image}", "    pull_policy: if_not_present"]
else:
    j = i + 1
    while j < len(lines) and (lines[j].startswith("    ") or not lines[j].strip()):
        j += 1
    block = [l for l in lines[i + 1:j] if not re.match(r"^    (image|pull_policy):", l)]
    lines[i + 1:j] = [f"    image: {image}", "    pull_policy: if_not_present"] + block
p.write_text("\n".join(lines) + "\n")
PY
# Point the existing Wizard store at the index (older installs had a local git store here).
"${DOCKER[@]}" exec runtipi-db psql -U tipi -d tipi -qc "UPDATE app_store SET url='https://apps.aiwizards.com', hash=encode(sha256('https://apps.aiwizards.com'::bytea),'hex'), name='Wizard Apps', enabled=true WHERE slug='wizardapps'" >/dev/null 2>&1 || true
if ! "${DOCKER[@]}" image inspect "$IMAGE" >/dev/null 2>&1; then "${DOCKER[@]}" pull "$IMAGE"; fi
(cd "$RUNTIPI_ROOT" && "${DOCKER[@]}" compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-deps runtipi)

# 2) wam command + box-only API service
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.wizard-os"
cat > "$HOME/.local/bin/wam" <<EOF
#!/usr/bin/env bash
cd "$HERE" && exec python3 -m wam "\$@"
EOF
chmod +x "$HOME/.local/bin/wam"

cat > "$HOME/.config/systemd/user/wizard-app-manager-api.service" <<EOF
[Unit]
Description=wam API (127.0.0.1:7790; changes need ~/.wizard-os/api-token)
After=network-online.target

[Service]
Environment=RUNTIPI_ROOT=$RUNTIPI_ROOT
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 -m wam serve
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

# Every required step below either succeeds or is recorded here; the summary at the end reports exactly what
# happened (wizard-os#28). FAILED = Wizard OS is not usable as installed; DEGRADED = usable, something is missing.
FAILED=()
DEGRADED=()
OUTCOME="$HOME/.wizard-os/install-outcome.json"

healthy=0
for _ in $(seq 1 "${WIZARD_HEALTH_TRIES:-60}"); do curl -fsS -o /dev/null "http://127.0.0.1:$(grep -E '^NGINX_PORT=' "$RUNTIPI_ROOT/.env" | cut -d= -f2 || echo 80)/api/health" 2>/dev/null && { healthy=1; break; }; sleep 2; done
[ "$healthy" = 1 ] || FAILED+=("the App Manager did not answer its health check within 2 minutes")
"$HOME/.local/bin/wam" doctor --fix >/dev/null 2>&1 || true   # repairs what it can; the next run is the verdict
"$HOME/.local/bin/wam" apps >/dev/null || DEGRADED+=("could not write the installed-app list (wam apps)")
"$HOME/.local/bin/wam" doctor || DEGRADED+=("wam doctor reported FAIL lines (above)")

# The generic `wizard` CLI (apps, tools, call, link, unlink, doctor). Vendored from wizard-os tools/wizard.
sh "$HERE/wizard-cli/install_wizard_cli.sh" >/dev/null 2>&1 || sh "$HERE/wizard-cli/install_wizard_cli.sh" --name wizard-os \
  || FAILED+=("could not install the wizard command")

# 2b) Alongside an existing Hermes (WIZARD_MODE=alongside, set by get.sh): the host's Hermes fills the agent spot
#     and is linked to the apps; the hermes-agent app is never installed. `wizard unlink` reverses the link.
#     A failed link rolls itself back (Hermes is left exactly as it was) and fails the install.
#     Never on a failed install: linking points Hermes at a gateway that isn't up and restarts it (PR19 review).
if [ "${WIZARD_MODE:-fresh}" = "alongside" ] && [ ${#FAILED[@]} -gt 0 ]; then
  echo "Alongside mode: the install already failed, so your Hermes (${HERMES_HOME:-$HOME/.hermes}) was not touched."
elif [ "${WIZARD_MODE:-fresh}" = "alongside" ]; then
  echo "Alongside mode: Wizard OS runs next to your Hermes (${HERMES_HOME:-$HOME/.hermes}); nothing in it is moved."
  if ! "$HOME/.local/bin/wam" alongside ${WIZARD_SHELL_PORT:+--shell-port "$WIZARD_SHELL_PORT"} >/dev/null; then
    FAILED+=("could not record alongside mode (wam alongside)")
  elif ! "$HOME/.local/bin/wizard" link --restart ${WIZARD_PLUGIN_REF:+--plugin-ref "$WIZARD_PLUGIN_REF"} \
      ${WIZARD_PLUGIN_SOURCE:+--plugin-source "$WIZARD_PLUGIN_SOURCE"}; then
    FAILED+=("'wizard link' failed (its message is above); your Hermes was left as it was. Fix it, then re-run the install")
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
if [ "${WIZARD_MODE:-fresh}" = "alongside" ] && [ ${#FAILED[@]} -eq 0 ]; then
  echo; "$HOME/.local/bin/wizard" doctor || DEGRADED+=("wizard doctor reported FAIL lines (above)")
fi

if [ ${#FAILED[@]} -gt 0 ]; then status=failed; elif [ ${#DEGRADED[@]} -gt 0 ]; then status=degraded; else status=ok; fi
python3 - "$OUTCOME" "$status" "${WIZARD_MODE:-fresh}" "${#FAILED[@]}" "${FAILED[@]}" "${DEGRADED[@]}" <<'PY' || true
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
case "$status" in
  ok)
    echo "Wizard OS is installed (box-only: nothing listens on the public interface)."
    echo "Next: run 'wam slots' or 'wam apps' as $(id -un). Re-run the same curl command any time to update." ;;
  degraded)
    echo "Wizard OS is installed, but not everything is working:"
    printf '  - %s\n' "${DEGRADED[@]}"
    echo "Re-run the same curl command to retry; 'wam doctor' shows the current state."
    # Unattended image builds keep working by default; WIZARD_STRICT=1 makes a degraded install exit non-zero.
    [ "${WIZARD_STRICT:-0}" != 1 ] || exit 3 ;;
  failed)
    echo "Wizard OS did NOT finish installing:" >&2
    printf '  - %s\n' "${FAILED[@]}" >&2
    [ ${#DEGRADED[@]} -eq 0 ] || printf '  - %s\n' "${DEGRADED[@]}" >&2
    if [ -f "$OUTCOME" ]; then
      echo "Re-run the same curl command once it is fixed. Recorded in $OUTCOME." >&2
    else
      echo "Re-run the same curl command once it is fixed." >&2
    fi
    exit 1 ;;
esac
