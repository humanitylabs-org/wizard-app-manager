#!/bin/bash
cd ~/.hermes/cache/scratch/caddytest
OWNER=$(cd ~/.local/share/wizard-search-releases/consolidated-20261105/wam-api-v2/main-1fe95685830b && /usr/bin/python3 -c "from wam import store_install as s; print(s.owner_login())")
echo owner_known=$([ -n "$OWNER" ] && echo yes || echo no)
timeout 40 python3 stub.py "$OWNER" &
timeout 40 ~/.local/bin/caddy-private-gateway run --config Caddyfile --adapter caddyfile >caddy.log 2>&1 &
sleep 4
for u in _updates _main-compare "_connections/api/connections" "_connections/" _remove/x _host-access/x _views/x wizard-mail; do printf "%-32s " "$u"; curl -s -m15 -o /tmp/o.txt -w '%{http_code} ' http://127.0.0.1:29988/wizard-os-home/store-install/$u; head -c 110 /tmp/o.txt | tr '\n' ' '; echo; done
wait
