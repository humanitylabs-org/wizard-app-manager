#!/bin/bash
# Real-systemd proof that `os_patch apply` installs security fixes without restarting any service.
# Run as root on a disposable VM: restart_proof.sh /path/os_patch.py
set -u
SRC=$1
pids() { systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}' | sort |
  while read -r u; do echo "$u $(systemctl show -p MainPID --value "$u") $(systemctl show -p ExecMainStartTimestampMonotonic --value "$u")"; done; }
# a stand-in for a chat run: a transient service writing every second
systemd-run --unit=wizard-fake-work --property=Restart=no bash -c 'while :; do date +%s >> /var/tmp/fake-work.log; sleep 1; done' >/dev/null
sleep 2
pids > /var/tmp/before.txt
boot_before=$(cat /proc/sys/kernel/random/boot_id)
python3 "$SRC" check --refresh > /var/tmp/check-before.json
python3 "$SRC" apply > /var/tmp/apply.json; rc=$?
pids > /var/tmp/after.txt
python3 - "$rc" "$boot_before" <<'P'
import json, sys
a = json.load(open('/var/tmp/apply.json')); b = json.load(open('/var/tmp/check-before.json'))
before = dict(l.split(" ", 1) for l in open('/var/tmp/before.txt').read().splitlines())
after = dict(l.split(" ", 1) for l in open('/var/tmp/after.txt').read().splitlines())
restarted = sorted(u for u in before if u in after and before[u] != after[u])
gone = sorted(set(before) - set(after))
log = [int(x) for x in open('/var/tmp/fake-work.log').read().split()]
gaps = max((y - x for x, y in zip(log, log[1:])), default=0)
print(json.dumps({"apply_rc": int(sys.argv[1]), "state": a.get("state"), "pending_before": len(b.get("security") or []),
  "held_before": [x["name"] for x in b.get("held") or []], "installed": len(a.get("installed") or []),
  "not_installed": a.get("not_installed"), "seconds": a.get("seconds"), "services_running_before": len(before),
  "services_restarted": restarted, "services_stopped": gone, "fake_work_max_gap_s": gaps,
  "same_boot": open('/proc/sys/kernel/random/boot_id').read().strip() == sys.argv[2],
  "reboot_required": a.get("reboot_required"), "needs_restart": a.get("needs_restart"),
  "guard_removed": "wizard-os" not in (open('/usr/sbin/policy-rc.d').read() if __import__('os').path.exists('/usr/sbin/policy-rc.d') else ""),
  "failed_units": __import__('subprocess').run(["systemctl","--failed","--no-legend","--plain"],capture_output=True,text=True).stdout.split("\n")[:5]}, indent=1))
P
