# Developer mode and temporary full server access

Decision 97's source setting is owner-only, off by default, and not a release channel.
The owner Approve page is the existing `wam serve` Store listener at
`/wizard-os-home/store-install/_approve/developer` on the box's Tailscale origin
(local listener: `http://127.0.0.1:7791/_approve/developer`). It uses the same
root peer + pinned, untagged Tailscale owner + same-origin POST checks as app
access approvals. An agent bearer or a forged identity header cannot approve it.
A box-local administrator can use the protected manager CLI instead:

```sh
cd /var/lib/wizard-os/wizard-app-manager
sudo env HOME=/var/lib/wizard-os WIZARD_OS_HOME=/var/lib/wizard-os/.wizard-os \
  RUNTIPI_ROOT=/var/lib/wizard-os/runtipi python3 -m wam developer on \
  --repo https://github.com/owner/wizard-app-manager --ref personal-branch
# Same environment and working directory for each following owner command:
# python3 -m wam developer apply
# python3 -m wam developer grant --seconds 3600
# python3 -m wam developer revoke
# python3 -m wam developer off
```

The ordinary agent's `wam` is only the thin HTTP client, not this protected CLI.
`developer on` saves an HTTPS repository and ref; `developer apply` installs from
it through the ordinary detached installer with `WAM_REPO` and `WAM_REF`. The
protected checkout's own `get.sh` is the bootstrap: using an older public script
would miss the separated-manager route. The existing git `origin` is switched to
the selected fork, and back to the official repository on return. A root-entered
human CLI drops subprocesses to the protected state owner's uid before any git or
installer execution; neither subprocess runs as root. An already
prepared wizard-system identity uses its protected marker instead of inspecting
an agent home or requiring an agent user bus. Saving a
source is deliberately separate from installing it. Automatic release updates
are paused and ordinary release apply is refused while developer mode is on.
The current commit/ref and distance from **locally cached** `origin/main` are
exposed in status; status never fetches code or invents a behind count.

`developer off` first revokes server access, restores the official stable/latest
source (the owner's existing release channel), then starts the same release
installer used by Updates, clearing personal source/image/pin overrides. It forces
that reinstall even if a stale receipt names the current release. Until a new
successful release receipt appears, the status banner says return is pending,
including when installation failed. `--setting-only` restores the source without
starting installation, for an owner intentionally deferring it (and acceptance
which must retain the exact candidate). It still revokes access. Use `off` again
to retry. A source setting is never restored from an agent-controlled backup.

## Fixed root boundary

Only owner-approved host preparation installs `scripts/install-server-access.py`
and the standalone helper `/usr/local/libexec/wizard-os/server-access`. The helper
imports no manager/agent code; Python isolated mode ignores Python environment
injection. Root-owned, non-writable ancestors and a root-only config pin the host
agent identity. A single sudoers line authorizes **wizard-system only** for exact
`grant`, `revoke`, `status` invocations. Extra args are refused by sudoers and the
helper. The grant's small JSON stdin accepts only duration and the migration flag;
there is no username, path, command or timer name input.

- Alongside agent: the pinned host Hermes user, with no group/layout changes.
- **Fresh agent: the `hermes-agent` container. Full server access is unsupported.**
  Container sudo does not grant host sudo; we do not add a Docker socket, privileged
  mount, host broker, host account impersonation or root shell. API/page/CLI return
  an explicit unsupported explanation. The owner keeps their SSH/admin login.
- Grant: maximum/default seven days, shorter owner-selected seconds accepted.
  `visudo -cf` validates the candidate, `visudo -c` checks the complete policy before
  and after its atomic installation. Failure removes the grant. No grant is
  installed if the root expiry timer is inactive.
- `/etc/sudoers.d/91-wizard-agent-access` is root-owned 0440, includes sudo's own
  `NOTAFTER` deadline (denial even if the timer is delayed), full `(ALL:ALL)` sudo,
  and `Defaults:<agent> logfile="/var/log/wizard-os-agent-sudo.log"`.
- The persistent root `wizard-agent-access-expiry.timer` checks every 15 seconds
  and on boot, removing the sudoers include and root grant record after expiry.
- SHA256 of every `/etc/sudoers.d/*` and helper entry is recorded at grant start.
  Status/doctor report additions, changes and removals. Extensions retain the
  previous baseline, except the expiry comment of our own sudoers file, so they
  cannot hide unrelated drift. Drift is a warning, not a rollback of admin policy.
- An agent deliberately given root can disable logging/timers or tamper with the
  checker itself. These are visibility/safety measures, **not a sandbox for root**.
  Owner confirmation explicitly says the grant can change the whole server.
- Host re-preparation revokes an existing grant; nightly/app updates never install
  or update this root package. Runtipi and the manager remain wizard-system-owned.

Slice 6 calls `developer.migration_grant(home, agent_was_in_docker_group)` **before
removing existing docker membership**. The helper independently checks that
membership and its root-only, permanent `migration-used` ledger, grants exactly
seven days without enabling developer mode, and refuses a second migration grant
even after revoke/expiry/host re-preparation. Extension requires developer mode.
No migration is performed by Slice 5.

## API and shell follow-up

- `GET /api/developer` or `/api/status`: selected source, enabled state, running
  commit, cached behind count, banner/return-pending flag, automatic-update pause,
  server access expiry/unsupported reason/drift.
- `POST /api/developer`: `{enabled:true,repo,ref}` or `{enabled:false}`.
- `POST /api/developer/server-access`: `{action:"grant",seconds:...}` or
  `{action:"revoke"}`. Grant is refused when developer mode is off.
- `POST /api/developer/apply`: `{}`.
- All writes use the existing owner boundary, including the `_owner/` equivalents
  on the Store listener. The Approve page wraps the same finite operations.
- `wam status` works over the thin agent transport; `wam doctor` shows
  `Agent has full server access until <UTC time>` as a failed/red health row.
  `/api/doctor` adds `severity:"red"`; drift is another failed warning row.

REUSE: StoreHandler, owner.check, existing Approve page assets and update installer.
EXTEND: owner operation allowlist, doctor projection, thin read-only status command.
NEW: standalone root helper, its root host package and developer state module.
No Wizard UX or Agent Service source changes belong to this slice. Follow-up:
show the returned banner persistently, render the red Health row using severity,
and add Settings → Developer mode pointing at the protected Approve page. The
App Manager confirmation page is implemented here; shell-wide presentation is not.
