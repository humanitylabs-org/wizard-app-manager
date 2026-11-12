# Graceful Agent Service restarts (#642)

Switch: `wizard/graceful-restart` (Settings > Try new features, For testers only). Off by default: with it off every
restart stops the Agent Service straight away, as before. It needs the Agent Service's own gate too
(`WIZARD_GRACEFUL_RESTART=1`, wizard-agent-service `docs/graceful-restart.md`); a service without it answers 404 and
is restarted as before.

Code: `wam/graceful_restart.py`, wired into `cli.app_lifecycle` (verb `restart`) and `cli.app_update`, so it covers
`wam app restart|update wizard-agent-service` (also the flag-first form, `--no-backup` first), a WizardOS release moving
the service, and settings changes that recreate it. With the switch on, a bulk `wam app update|restart` (`--all`, or
several apps with the service among them) is refused with the commands to run instead, so runtipi-cli never restarts
the service behind the gate.

## What happens

1. `POST /wizard/v1/restart` on the service's loopback port: new sends are saved and held, running turns go on.
2. `GET` every 3 s (also renews the service's 2-minute hold lease) until `phase` is `ready`, then restart.
   A lost poll is retried, never read as "nothing runs"; a service that stays silent for 60 s after the hold was
   taken calls the restart off.
3. `overdue` (still running past the wait limit, 10 min by default): the owner is told what still runs (the
   Notifications app, and a line on the terminal; the gate keeps polling while it waits) and answers with
   - `wam agent-restart wait [--minutes N]`: `POST` again with the new limit;
   - `wam agent-restart now`: restart now;
   - `wam agent-restart cancel`: call it off (`DELETE`), held sends launch at once, nothing changes.
   The same answer goes over the owner API: `GET/POST /api/agent-restart` on `wam serve` (bearer token),
   body `{"action": "wait"|"now"|"cancel", "minutes": n}`. With no answer within 30 min
   (`WAM_GRACEFUL_RESTART_ASK_S`) the restart is called off, so a turn is never cut without the owner's yes.
4. After the restart `/wizard/health` must answer within 3 min. For an update that fails it, the backup the update
   just made is restored (`app_updates.rollback`: the previous release and image) and health is checked again. A
   plain restart keeps its image, so a failed check is reported, not rolled back.

The waiting state and the owner's answer live in `<wizard-os home>/agent-restart.json`; it is removed when the gate ends.

## Not covered yet

- The full installer moves the service with `wam app update wizard-agent-service`, so it waits too; installer steps
  that restart Runtipi itself (all apps at once) do not go through this gate.
- The shell's "Restarting when chats finish" state and wait / restart now buttons (wizard-ux) are their own issue.
