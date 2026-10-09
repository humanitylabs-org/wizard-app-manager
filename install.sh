#!/usr/bin/env bash
# Install wizard-app-manager as user services next to a stock Runtipi.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNTIPI_ROOT="${RUNTIPI_ROOT:-$HOME/runtipi}"

if [ ! -f "$RUNTIPI_ROOT/.env" ]; then
  echo "Runtipi not found at $RUNTIPI_ROOT; installing stock Runtipi"
  curl -fsSL https://setup.runtipi.io -o /tmp/runtipi-setup.sh
  sudo bash /tmp/runtipi-setup.sh
fi
python3 -c 'import yaml' 2>/dev/null || sudo apt-get install -y python3-yaml

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.wizard-os"
cat > "$HOME/.local/bin/wam" <<EOF
#!/usr/bin/env bash
cd "$HERE" && exec python3 -m wam "\$@"
EOF
chmod +x "$HOME/.local/bin/wam"

cat > "$HOME/.config/systemd/user/wizard-app-manager.service" <<EOF
[Unit]
Description=Wizard App Manager (API + compiled Runtipi store)
After=network-online.target

[Service]
Environment=RUNTIPI_ROOT=$RUNTIPI_ROOT
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 -m wam serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF

cat > "$HOME/.config/systemd/user/wizard-app-manager-refresh.service" <<EOF
[Unit]
Description=Wizard App Manager: find and compile apps

[Service]
Type=oneshot
Environment=RUNTIPI_ROOT=$RUNTIPI_ROOT
WorkingDirectory=$HERE
ExecStart=/usr/bin/env python3 -m wam refresh
EOF

cat > "$HOME/.config/systemd/user/wizard-app-manager-refresh.timer" <<EOF
[Unit]
Description=Refresh the Wizard app store every 30 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF

loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now wizard-app-manager.service wizard-app-manager-refresh.timer
sleep 2
systemctl --user start wizard-app-manager-refresh.service || true
"$HOME/.local/bin/wam" doctor
