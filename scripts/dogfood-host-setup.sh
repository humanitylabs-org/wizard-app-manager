#!/usr/bin/env bash
# One-time root setup to dogfood the Wizard App Manager on an existing Hermes host
# (no VM: Runtipi runs on the same machine as Hermes, the Agent Service and the UX).
#
#   sudo bash scripts/dogfood-host-setup.sh <user>
#
# 1. Installs Docker Engine (official script) and lets <user> use it.
# 2. Keeps every Docker-published port off the public interface. Runtipi's Traefik
#    publishes 80/443 on all interfaces; on a VPS that would be public. A DOCKER-USER
#    rule drops new inbound connections on the default-route interface, so Runtipi and
#    its apps answer only on loopback (and through the private tailnet gateway).
# 3. Installs stock Runtipi with its own installer, unmodified, in /home/<user>/runtipi.
# After this, <user> runs ./install.sh from the repo with no root.
set -euo pipefail
U="${1:?usage: sudo bash dogfood-host-setup.sh <user>}"
H="$(getent passwd "$U" | cut -d: -f6)"
IFACE="$(ip -o route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')"

if ! command -v dockerd >/dev/null; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
fi
usermod -aG docker "$U"

# Public interface: allow replies, drop new inbound to any container port.
iptables -C DOCKER-USER -i "$IFACE" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null \
  || iptables -I DOCKER-USER 1 -i "$IFACE" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -C DOCKER-USER -i "$IFACE" -j DROP 2>/dev/null \
  || iptables -I DOCKER-USER 2 -i "$IFACE" -j DROP
cat > /etc/systemd/system/wizard-docker-public-guard.service <<EOF
[Unit]
Description=Keep Docker-published ports off $IFACE (Wizard dogfood)
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'iptables -C INPUT -i $IFACE -p tcp --dport 7790 -j DROP 2>/dev/null || iptables -I INPUT 1 -i $IFACE -p tcp --dport 7790 -j DROP; iptables -C DOCKER-USER -i $IFACE -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null || iptables -I DOCKER-USER 1 -i $IFACE -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN; iptables -C DOCKER-USER -i $IFACE -j DROP 2>/dev/null || iptables -I DOCKER-USER 2 -i $IFACE -j DROP'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now wizard-docker-public-guard.service

if [ ! -f "$H/runtipi/.env" ]; then
  cd "$H"
  curl -fsSL https://setup.runtipi.io -o /tmp/runtipi-setup.sh
  bash /tmp/runtipi-setup.sh          # stock installer; creates ./runtipi
fi
# The App Manager (running as $U) reads Runtipi's .env for the API secret. Nothing else changes.
chgrp "$U" "$H/runtipi/.env" && chmod 640 "$H/runtipi/.env"

# The App Manager's own port (7790) serves the compiled store to Runtipi over the Docker
# bridge; keep it off the public interface too.
iptables -C INPUT -i "$IFACE" -p tcp --dport 7790 -j DROP 2>/dev/null \
  || iptables -I INPUT 1 -i "$IFACE" -p tcp --dport 7790 -j DROP
echo "OK: docker + stock Runtipi ready; public $IFACE blocked for container ports."
echo "Next (as $U, no root): cd ~/workspace/wizard-app-manager && ./install.sh && wam doctor"
