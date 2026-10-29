#!/usr/bin/env bash
# Root host phase, fetched by get.sh, never run by the nightly updater.
# Reuse named-user host preparation (Runtipi + Docker + Tailscale operator).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Fresh host preparation requires root.' >&2; exit 2; }
owner="${1:?service account required}"
repo="${2:?bootstrap repository required}"
ref="${3:?bootstrap ref required}"
sudoers="${4:-/etc/sudoers.d}"
[[ "$owner" =~ ^[a-z_][a-z0-9_-]*$ ]] && [ "$owner" != root ] && id "$owner" >/dev/null 2>&1 || {
  echo 'Expected an existing non-root service account.' >&2; exit 2;
}
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
curl -fsSL "$repo/raw/$ref/scripts/prepare-named-user.sh" -o "$work/prepare.sh"
bash "$work/prepare.sh" "$owner"
# This package is intentionally not installed by an unprivileged wam invocation.
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq unattended-upgrades >/dev/null
curl -fsSL "$repo/raw/$ref/scripts/root-helpers/os-patch" -o "$work/os-patch"
# Refuse writable/symlinked ancestors, including a preexisting helper or sudoers
# file. The root phase never loads or executes the service account's checkout.
python3 - "$sudoers" <<'PY'
import os, stat, sys
from pathlib import Path
for dest in (Path('/usr/local/libexec/wizard-os'), Path(sys.argv[1])):
    for p in reversed((dest, *dest.parents)):
        if p.is_symlink():
            raise SystemExit('Refusing symlink in privileged path: ' + str(p))
        if not p.exists():
            p.mkdir(mode=0o755)
        s = p.stat()
        if s.st_uid != 0 or s.st_mode & 0o022 or not stat.S_ISDIR(s.st_mode):
            raise SystemExit('Privileged directory must be root-owned and not writable by others: ' + str(p))
for p in (Path('/usr/local/libexec/wizard-os/os-patch'), Path(sys.argv[1]) / 'wizard-os-helpers'):
    if p.is_symlink():
        raise SystemExit('Refusing privileged symlink: ' + str(p))
PY
install -o root -g root -m 0755 "$work/os-patch" /usr/local/libexec/wizard-os/os-patch
# "" is sudoers' exact empty argument list, NOT its default of arbitrary arguments.
printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/wizard-os/os-patch ""\n' "$owner" > "$work/sudoers"
chmod 0440 "$work/sudoers"
visudo -cf "$work/sudoers"
stage="$(mktemp "$sudoers/.wizard-os-helpers.XXXXXX")"
trap 'rm -rf "$work"; rm -f "${stage:-}"' EXIT
install -o root -g root -m 0440 "$work/sudoers" "$stage"
mv -fT "$stage" "$sudoers/wizard-os-helpers"
# Only retire our exact historical grant, after both replacement files validate.
# A customized file is administrator policy: stop for review instead of deleting it.
legacy="$sudoers/90-wizard-$owner"
if [ -e "$legacy" ] || [ -L "$legacy" ]; then
  if [ ! -L "$legacy" ] && [ "$(cat "$legacy")" = "$owner ALL=(ALL) NOPASSWD:ALL" ]; then
    rm -f "$legacy"
  else
    echo "Review $legacy as administrator; it is not the historical Wizard grant." >&2
    exit 2
  fi
fi
curl -fsSL "$repo/raw/$ref/scripts/root-helpers/server-access" -o "$work/server-access"
curl -fsSL "$repo/raw/$ref/scripts/install-server-access.py" -o "$work/install-server-access.py"
/usr/bin/python3 -I "$work/install-server-access.py" "$work/server-access" --fresh "$(getent passwd "$owner" | cut -d: -f6)/.wizard-os"
# Approval custody is always a root system socket, never a user-unit bind.
# Stage trusted-ref source under root-owned ancestors for this host phase.
custody_stage="$(mktemp -d /root/wizard-approval.XXXXXX)"
curl -fsSL "$repo/raw/$ref/scripts/install-approval-custody.py" -o "$custody_stage/install.py"
curl -fsSL "$repo/raw/$ref/wam/approval_custody.py" -o "$custody_stage/approval_custody.py"
manager_home="$(getent passwd "$owner" | cut -d: -f6)"
/usr/bin/python3 -I "$custody_stage/install.py" install "$owner" "$manager_home/.wizard-os" "${WAM_DIR:-$manager_home/wizard-app-manager}" "$custody_stage/approval_custody.py"
rm -rf "$custody_stage"
printf 'Fresh host prepared: fixed helpers, persistent approval custody; no blanket sudo grant.\n'
