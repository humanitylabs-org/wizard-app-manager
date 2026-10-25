#!/bin/sh
# Render the shell's nginx config from two settings, then run nginx in the foreground.
#   WIZARD_AGENT_SERVICE_ORIGIN  where the Agent Service answers (default http://wizard-agent-service:8787)
#   WIZARD_SHELL_LISTEN          what nginx listens on (default 8788; alongside mode: 127.0.0.1:<port>)
set -eu
: "${WIZARD_AGENT_SERVICE_ORIGIN:=http://wizard-agent-service:8787}"
: "${WIZARD_SHELL_LISTEN:=8788}"
printf '%s' "$WIZARD_AGENT_SERVICE_ORIGIN" | grep -Eq '^http://[A-Za-z0-9._-]+(:[0-9]{1,5})?$' \
  || { printf 'WIZARD_AGENT_SERVICE_ORIGIN must be an http origin like http://host:port (no TLS inside the box)\n' >&2; exit 2; }
printf '%s' "$WIZARD_SHELL_LISTEN" | grep -Eq '^(127\.0\.0\.1:)?[0-9]{1,5}$' \
  || { printf 'WIZARD_SHELL_LISTEN must be a port or 127.0.0.1:<port>\n' >&2; exit 2; }
# Host network (listening on 127.0.0.1) has no Docker DNS: the service must be an IP literal there.
case "$WIZARD_SHELL_LISTEN" in 127.0.0.1:*)
  printf '%s' "$WIZARD_AGENT_SERVICE_ORIGIN" | grep -Eq '^http://[0-9.]+:[0-9]{1,5}$' \
    || { printf 'on the host network WIZARD_AGENT_SERVICE_ORIGIN must be http://<ip>:<port>\n' >&2; exit 2; } ;;
esac
export WIZARD_AGENT_SERVICE_ORIGIN WIZARD_SHELL_LISTEN
cp /etc/wizard-shell/static.conf /tmp/wizard-static.conf
cp /etc/wizard-shell/proxy.conf /tmp/wizard-proxy.conf
cp /etc/wizard-shell/proxy-mcp.conf /tmp/wizard-proxy-mcp.conf
# Substitute only these two names; nginx's own $variables stay as they are.
envsubst '${WIZARD_AGENT_SERVICE_ORIGIN} ${WIZARD_SHELL_LISTEN}' < /etc/wizard-shell/nginx.conf.template > /tmp/nginx.conf
nginx -t -q -c /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g 'daemon off;'
