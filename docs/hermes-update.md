# Owner-controlled host Hermes updates

**Hermes updates are paused by the shared Wizard lock.** Status and dry-run are
read-only; real host or `hermes-agent` app updates refuse until a reviewed
promotion exists in `wam/hermes-lock.json` and the operator explicitly sets
`WIZARD_HERMES_PROMOTION=<approved-id>`. Ordinary OS reruns skip installed Hermes;
Automatic clears this opt-in. Other Wizard app updates are unchanged. Fresh
installs consume the locked recipe even without an OS snapshot. Public installer
metadata in `releases/hermes-lock.json` must be byte-identical to the packaged
lock. Both vendor wizard-os `releases/hermes-lock.json` and `tools/hermes_lock.py`.

The initial freeze preserves the existing September baseline, whose acceptance
record has a known compacted-history visibility limitation. It is not a newly
certified latest release and never automatically downgrades an alongside host.
Promotion requires seven full days after publication, bug review, exact-source
acceptance/canary and rollback evidence, a recommendation, and non-author admin
approval (James for stable). See
[the shared contract](https://github.com/humanitylabs-org/wizard-os/pull/215).
Do not deploy the global/default change without its clean-VM fresh/alongside
installer acceptance and non-author approval. Raw Docker/Runtipi admin commands,
stock `hermes update` and owner cron must be audited separately.

```sh
wam hermes status --json
wam hermes update --dry-run --json
wam hermes update --wait-quiet 1800 --json
```

`wam updates` also includes the `hermes` status object for Settings → Updates.
Automatic OS updates **never update a host Hermes**. The installer agent's
“Never reinstall or update Hermes” rule is unchanged. Only an explicit owner
command opts in to the host update and gateway restart.

The target is the `hermes` block in the snapshot named by
`~/.wizard-os/release.json`, fetched as `<RELEASE_BASE>/<release>.json` (the
existing `WIZARD_RELEASE_BASE` override is supported). It is not the latest
upstream tag or upstream main, and does not skip ahead to a different OS release.
A snapshot without a Hermes block reports `no_pin` and changes nothing.

Alongside mode identifies the host home, API port and owner from `alongside.json`.
Only `<hermes_home>/hermes-agent` git checkouts with `venv/bin/python` are
supported. Package-manager, desktop and other installs report `manual` and are
untouched. Fresh-box container Hermes reports `container`: its version moves
with the release's default app rather than this command.

Run the command as the linked Hermes owner, **in a terminal outside the gateway's
service cgroup**. Keep it running until it finishes. The command refuses to
restart itself from inside that service. A matching active `hermes-gateway*`
user/system unit is required; ambiguous or unsupervised installations are left
for manual updating. No service units are installed or rewritten.

## Safety and recovery

- The final numeric version tag must resolve to the pinned full commit after a
  tag fetch. Dirty tracked files, downgrades, unknown running versions, wrong
  users and insufficient disk space refuse the update. Disk admission reserves
  two source-state sizes plus 512 MiB for backup and dependency work.
- Backups under `~/.wizard-os/backups/hermes-<timestamp>-<id>/` contain a verified
  online SQLite snapshot, config, dotenv, optional install stamp, previous commit
  and the venv's package freeze. Backup directories are mode 0700; secret-bearing
  files and receipts are mode 0600. Treat backups as credentials and retain them
  until the update is accepted.
- Work census includes the Agent Service queue, gateway `active_work`, unexpired
  turn leases, pending/running delegations and running cron executions. Missing
  optional files/tables are idle; unreadable existing state or an unknown
  configured Agent Service queue blocks the swap. The quiet wait defaults to
  1800 seconds. The gateway admission drain is owned by this attempt and work
  is checked again after requesting it.
- Only the exact tag is checked out, on the existing branch (or `main` for a
  detached checkout). Installation uses `uv pip --python <venv-python>` or that
  venv's `pip`, then the venv's import smoke check. No frontend build is run;
  missing `hermes_cli/web_dist` is recorded in receipt notes.
- Restart uses SIGUSR1 to the unit's fresh MainPID when ExecReload declares a
  USR1 reload, otherwise a scoped systemctl restart. `/health` must report the
  target version within 600 seconds.
- A swap/restart/health failure restores the previous commit, package pins,
  config/dotenv/stamp, restarts and verifies the old health version. Packages
  newly introduced by the failed update are removed. **The live state.db is not
  replaced during rollback**, so writes made after the snapshot are retained.
- `~/.wizard-os/hermes-update.json` records `running`, `updated`, `busy`,
  `refused`, `rolled_back` or `rollback_failed`, with from/to, backup path, PID,
  boot ID, timings and notes. A lost runner/reboot is reported as `interrupted`.
  A flock lock prevents overlapping attempts and is never unlinked on exit.
- An interrupted/failed rollback needs operator inspection before another run:
  preserve the receipt and backup, confirm the actual checkout, venv and gateway
  version, and finish recovery before archiving the old receipt. A failed
  rollback retains this attempt's admission drain for manual recovery; never
  remove another maintenance owner's drain.

Dry-run checks local tag identity, disk and supervision but does not fetch tags,
back up, install dependencies, signal/restart the gateway or write an update
receipt. It acquires the same lock. Missing local tags therefore fail dry-run
rather than silently mutating the checkout's refs.

## Isolated tests

```sh
uv run --with pytest --with pyjwt python3 -m pytest tests/test_hermes_update.py tests/test_updates.py -q
uv run --with pytest --with pyjwt python3 -m pytest -q
node --test tests/runtipi-open.test.mjs
```

The new tests use disposable git checkouts, fake venv executables, SQLite and a
loopback HTTP server. Unit discovery, systemctl commands and signals are faked;
no real Hermes installation or service is updated. On long checkout paths,
add `--basetemp=./t` to keep existing Unix-socket test paths below the OS limit.
