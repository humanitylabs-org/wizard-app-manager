#!/bin/sh
# Distro matrix for wam/os_patch.py: in each image, as root, check -> apply -> check, then prove the policy-rc.d guard
# is gone and no service was started. Output: one JSON line per image. Usage: os_patch_matrix.sh <os_patch.py> [image...]
set -u
SRC=$1; shift
IMAGES=${*:-"debian:12 ubuntu:24.04 ubuntu:22.04 fedora:40 rockylinux:9 almalinux:9 opensuse/leap:15.6 archlinux:latest alpine:3.19"}
for img in $IMAGES; do
  docker run --rm -i --platform linux/arm64 -v "$SRC":/os_patch.py:ro "$img" sh -s <<'EOF' 2>&1 | tail -1
set -u
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  if command -v apt-get >/dev/null; then apt-get update -qq >/dev/null && apt-get install -y -qq python3 >/dev/null 2>&1
  elif command -v dnf >/dev/null; then dnf -y -q install python3 >/dev/null 2>&1
  elif command -v zypper >/dev/null; then zypper -nq install python3 >/dev/null 2>&1
  elif command -v pacman >/dev/null; then pacman -Sy --noconfirm python >/dev/null 2>&1
  elif command -v apk >/dev/null; then apk add -q python3 >/dev/null 2>&1; fi
  PY=$(command -v python3)
fi
python3 - <<'P'
import json, subprocess, os
def j(*a):
    p = subprocess.run(["python3", "/os_patch.py", *a], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=3000)
    try: return json.loads(p.stdout)
    except ValueError: return {"state": "crash", "rc": p.returncode, "err": (p.stderr or p.stdout)[-500:]}
b = j("check", "--refresh"); a = j("apply"); c = j("check")
print(json.dumps({"distro": b.get("distro"), "family": b.get("family"), "can_apply": b.get("can_apply"),
  "before": [b.get("state"), len(b.get("security") or []), len(b.get("held") or [])],
  "apply": a.get("state"), "installed": len(a.get("installed") or []), "not_installed": a.get("not_installed"),
  "restarts": a.get("restarts"), "reason": (a.get("reason") or "")[-200:],
  "after": [c.get("state"), len(c.get("security") or [])], "reboot": c.get("reboot_required"),
  "guard_left": os.path.exists("/usr/sbin/policy-rc.d") and "wizard-os" in open("/usr/sbin/policy-rc.d").read(), "err": b.get("err") or a.get("err"), "held": [x["name"] for x in c.get("held") or []], "left": (c.get("security") and [x["name"] for x in c["security"]]) or []}))
P
EOF
done
