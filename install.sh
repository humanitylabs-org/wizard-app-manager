#!/usr/bin/env bash
# Install wizard-app-manager as a user service next to a stock Runtipi.
# Apps come from the Wizard App Index (https://apps.aiwizards.com); nothing searches GitHub from this box.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNTIPI_ROOT="${RUNTIPI_ROOT:-$HOME/runtipi}"

if [ ! -f "$RUNTIPI_ROOT/.env" ]; then
  echo "Runtipi not found at $RUNTIPI_ROOT; installing stock Runtipi"
  curl -fsSL https://setup.runtipi.io -o /tmp/runtipi-setup.sh
  sudo bash /tmp/runtipi-setup.sh
fi
python3 -c 'import yaml' 2>/dev/null || sudo apt-get install -y python3-yaml
command -v git >/dev/null || sudo apt-get install -y git

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.wizard-os"
cat > "$HOME/.local/bin/wam" <<EOF
#!/usr/bin/env bash
cd "$HERE" && exec python3 -m wam "\$@"
EOF
chmod +x "$HOME/.local/bin/wam"

cat > "$HOME/.config/systemd/user/wizard-app-manager.service" <<EOF
[Unit]
Description=Wizard App Manager API (127.0.0.1:7790; changes need ~/.wizard-os/api-token)
After=network-online.target

[Service]
Environment=RUNTIPI_ROOT=$RUNTIPI_ROOT
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 -m wam serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF

# Every 10 minutes: mirror the Wizard App Index into Runtipi's "Wizard Apps" store, so Runtipi's own
# App Store, Install, Update and Uninstall buttons cover every Wizard app.
cat > "$HOME/.config/systemd/user/wizard-app-manager-sync.service" <<EOF
[Unit]
Description=Mirror the Wizard App Index into Runtipi's Wizard Apps store

[Service]
Type=oneshot
Environment=RUNTIPI_ROOT=$RUNTIPI_ROOT
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 -m wam sync
EOF

cat > "$HOME/.config/systemd/user/wizard-app-manager-sync.timer" <<EOF
[Unit]
Description=Sync Wizard Apps every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user disable --now wizard-app-manager-refresh.timer 2>/dev/null || true   # old per-box GitHub search
rm -f "$HOME/.config/systemd/user/wizard-app-manager-refresh."{service,timer}
systemctl --user daemon-reload
systemctl --user enable --now wizard-app-manager.service wizard-app-manager-sync.timer
systemctl --user restart wizard-app-manager.service
sleep 2
"$HOME/.local/bin/wam" sync >/dev/null || echo "first sync failed; run: wam sync"
"$HOME/.local/bin/wam" skin on || true
"$HOME/.local/bin/wam" doctor
