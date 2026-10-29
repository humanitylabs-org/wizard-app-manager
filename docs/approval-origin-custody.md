# Approval-origin address custody

Status: implementation plus staged acceptance, not an authenticated tailnet pass.

**EXTEND** `owner_confirm`: keep its exact root-peer/login pin and short-lived,
body-bound, single-use confirmation protocol. The HTTP worker no longer binds the
approval address itself. No approval route setup API or elevated operation is
added to the agent client.

## One lifetime contract

Both fresh and alongside host preparation install the same root-owned system
socket, `wizard-owner-approval.socket`, before publishing any approval Serve site.
PID 1 retains `127.0.0.1:7792` independently of the manager worker. The worker
receives exactly one named activation descriptor; it verifies the loaded root
unit, persistent enablement, tailscaled dependency ordering, exact system service
MainPID, kernel socket inode/root UID, address/type, and descriptor declaration.
Environment variables alone are not authority. Missing, duplicate, wrong-name or
wrong-socket activation is a fatal manager startup error, before generic manager
listeners start. There is no user-unit or direct-bind fallback.

The socket has no `PartOf`/stop propagation from the worker. Its manual stop is
refused. Manager crashes, stops, failed startups and updates leave the address
held. Worker start limits and socket trigger limits are explicitly disabled and
read back: systemd otherwise propagates a service start-limit hit into socket
closure despite manual-stop protection. Worker restart backoff is one second.
A same-UID manager self-update uses a PID-pinned signal, not privileged
systemctl or a new sudo grant. The agent/session UID has neither permission.
`tailscaled.service` requires and starts after the socket, including persistent
route restoration after boot. Shutdown ordering stops tailscaled first. If
custody cannot start, tailscaled cannot start through this dependency.

## Publication, disable and removal

`wam approval-origin enable` requires persistent custody and reads back the exact
sole-handler route. It refuses existing sites without its custody provenance,
including ones already pointing at the expected port. Legacy same-origin pages
cannot be made trustworthy merely by replacing the server. Recovery requires
route withdrawal and a **new HTTPS port**. Retired origins are recorded and cannot
be reused by the supported enable path.

`wam approval-origin disable` removes the persistent Serve site, reads back its
absence, retires the origin and expires pending confirmations. It **does not stop
or disable the custody socket**. Failed route removal/readback retains recovery
metadata and custody. Migration refuses before quiescing an old worker if its
approval route exists without independent custody.

Only the owner-approved root host phase can remove custody. The fixed sequence is:
remove all approval routes and saved-origin routes; acknowledge their absence;
persist origin retirement; stop tailscaled and acknowledge inactivity; stop and
disable the worker; lift manual-stop protection as root; stop/disable the socket
and read back inactivity. Tailscaled stays stopped until an explicit administrator
restart. No root installation/removal helper is exposed to agents, and no root
agent/session assignment is introduced.

## Verification boundaries

`tests/test_approval_custody.py` and `tests/test_owner_confirm.py` exercise synthetic
policy/identity negatives. Installer command fixtures stub activation; they are
not kernel or systemd acceptance.

`scripts/qa-approval-custody-vm.py` is staged for a **fresh disposable systemd VM**.
It requires a parent-created `/run/wizard-disposable-privsep` marker, refuses real
Tailscale state/existing manager units, uses synthetic Tailscale configuration and
identity, and runs actual systemd/socket activation. Its cases include preoccupied
address refusal, continuous low-UID bind-negative attempts during abrupt exit,
restart, stop, update-shaped replacement and failed startup, wrong live FD and
changed loaded-unit refusals, failed route-removal acknowledgement, and a bind
positive control only after safe explicit removal. The fixture worker uses the
actual activation module and ApprovalHandler; an invalid-activation case uses the
actual full manager entrypoint. It does not replace full installed fresh/alongside
application lifecycle acceptance or reboot testing.

The owning parent must run this through its queued disposable VM harness and stop
the VM afterwards. Suggested guest invocation after staging the exact candidate
under a readable protected `/opt/wam-candidate` tree and installing python3-yaml:

```
install -m 0600 /dev/null /run/wizard-disposable-privsep
python3 /opt/wam-candidate/scripts/qa-approval-custody-vm.py
```

Before claiming complete, an owner-authorized tailnet test still needs real
Tailscale Serve ingress: exact owner success, different-user and tagged-node
refusal, pin drift, actual dedicated HTTPS origin and all browser intent gates,
worker outage/restart/update capture negatives while that real route persists,
and ordered boot/removal. Synthetic headers/logins are not that evidence. Never
copy owner authentication material into this fixture.
