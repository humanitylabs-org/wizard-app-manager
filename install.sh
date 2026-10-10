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

if [ ! -f "$RUNTIPI_ROOT/.env" ]; then
  echo "No install at $RUNTIPI_ROOT; installing the base (Runtipi) first"
  curl -fsSL https://setup.runtipi.io -o /tmp/runtipi-setup.sh
  sudo bash /tmp/runtipi-setup.sh
fi
python3 -c 'import yaml' 2>/dev/null || sudo apt-get install -y python3-yaml
command -v git >/dev/null || sudo apt-get install -y git

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
docker exec runtipi-db psql -U tipi -d tipi -qc "UPDATE app_store SET url='https://apps.aiwizards.com', hash=encode(sha256('https://apps.aiwizards.com'::bytea),'hex'), name='Wizard Apps', enabled=true WHERE slug='wizardapps'" >/dev/null 2>&1 || true
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi
(cd "$RUNTIPI_ROOT" && docker compose -p runtipi -f docker-compose.yml -f user-config/tipi-compose.yml up -d --no-deps runtipi)

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
docker rm -f wizard-store wizard-skin >/dev/null 2>&1 || true
rm -f "$RUNTIPI_ROOT/traefik/dynamic/wizard-skin.yml"

loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now wizard-app-manager-api.service
systemctl --user restart wizard-app-manager-api.service

for i in $(seq 1 60); do curl -fsS -o /dev/null "http://127.0.0.1:$(grep -E '^NGINX_PORT=' "$RUNTIPI_ROOT/.env" | cut -d= -f2 || echo 80)/api/health" 2>/dev/null && break; sleep 2; done
"$HOME/.local/bin/wam" doctor --fix || true
"$HOME/.local/bin/wam" apps >/dev/null || true
"$HOME/.local/bin/wam" doctor || true

# 3) Fill every empty parking spot (shell, agent service, agent, files, ...) with its default app from the index.
#    Spots with no published default yet are skipped. WAM_NO_BOOTSTRAP=1 installs the App Manager only.
if [ "${WAM_NO_BOOTSTRAP:-0}" != "1" ]; then "$HOME/.local/bin/wam" bootstrap; "$HOME/.local/bin/wam" slots; fi
