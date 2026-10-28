# Existing-box migration (owner action only)

This branch is **do not merge**. No updater runs this migration. Do not try it on
a daily-driver box before the clean-guest migration and rollback report is green.

Keep a separate administrator/root SSH login open. The selected Hermes user's
sessions are ended at the final step so old processes cannot retain Docker group
credentials. The stock Hermes user service comes back under its original UID.

Download `scripts/prepare-privilege-migration.py` from the **reviewed full commit**
and run it with the isolated system Python as root, passing the account, its
Hermes home and that same full commit. It fetches its own protected checkout;
it never elevates an agent-written checkout. The root-only journal and opaque
pre-migration snapshot live in `/var/lib/wizard-os-migration` (0700).

The state machine inventories metadata, stops known user units and Wizard
containers, snapshots with `cp -a --reflink=auto`, copies the Runtipi and manager
trees, regenerates protected units and alongside wiring, rotates manager and
agent tokens, transfers only the Tailscale operator right, restarts apps and
checks every doctor row and existing Serve target. App-data, database, media and
extra-agent homes keep all numeric owners. Only management trees change owner.
The host Hermes home and Claude login share stay at the agent UID; Compose
mounts for extra agents, builder and pointer skills follow the relocated trees.
Python never parses app payloads, recordings or private agent homes.

Only after doctor succeeds does the one-time seven-day migration grant run for
an agent previously in Docker. Removing Docker membership is the final authority
change. The grant is root-equivalent, logged, expires automatically, is revocable
with the owner server-access control, and does not enable developer mode. A
root-only consumed marker survives rollback; rollback cannot renew seven days.
Health stays red while the explicit grant is on. This warning is not permission
to bypass the thin client.

## Resume and rollback

Repeat the same host-preparation invocation after an interruption. Each step has
a durable journal entry; partial snapshot copies are discarded before retry.
`wam privilege status` from the administrator login shows the checkpoint.
`wam privilege rollback` from that login restores the original state and tokens,
unit files and activation, supplementary groups, operator, and app/database data
from the frozen snapshot. It **discards changes since migration**, so use it
before resuming real work, not as an everyday restore. Keep the snapshot until
the owner has accepted the migrated box. Never expose its contents to an agent.

Root can also enter the protected CLI directly:

```
/usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-privilege rollback
```

For a rehearsal checkpoint, that CLI accepts `migrate --stop-after relocate`;
resuming without the option exercises the same journal path. No special test
implementation copies the data. The runner compares the entire stopped app-data
tree at that checkpoint before any application starts writing again.

## Agent repair instructions

On a migrated box, use the **thin `wam` client**, including `wam doctor`, `wam
logs`, named lifecycle commands, repository backup and restore, and local build
upload. Do not start a second local App Manager, run Docker, change operator
rights, edit system units or seek sudo. If Health says owner action is needed,
give the owner that exact message. The general OS repair kit's Docker/systemd
commands are administrator instructions, not agent instructions.

## Refusals that need owner mapping

Root agents, pre-existing sudo/admin membership or policy, a conflicting operator,
a pre-existing unjournaled destination, a missing alongside identity file, a
nonstandard Hermes home, or additional user services referencing the old trees
stop before data moves. Custom live services and their drop-ins need reviewed
protected equivalents, not blind promotion of user-written commands. See the
metadata-only [James-box dry run](../qa/privsep-slice6/JAMES-BOX-DRYRUN.md).
