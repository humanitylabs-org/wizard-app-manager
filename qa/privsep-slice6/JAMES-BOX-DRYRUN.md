# James-box migration dry run — metadata only, NO LIVE CHANGES

Status: **not safe to run yet**. This is a read-only inventory and a proposed owner
operation. No service, Docker group, operator, file, container or published route
on the live legacy box was changed. No private-agent file, Messenger database,
recording, meeting content or app-data payload was opened. Private agent entries
below came only from top-level directory names and Compose mount metadata.

Use `$LEGACY_HOME` for the selected Hermes user's home below. Paths are portable;
no personal home path or credential belongs in this document.

## Observed layout

- `$LEGACY_HOME/runtipi` and `$LEGACY_HOME/.wizard-os` exist and have UID/GID 1000.
  The login has the `docker` supplementary group, with no sudo/wheel/admin group.
  No sudoers snippet names were found. This is not a claim that every possible
  sudo rule was audited; the production migration queries the legacy account's
  policy from its root-only authority checker.
- The user unit `wizard-app-manager-api.service` runs Python from
  `$LEGACY_HOME/workspace/wizard-app-manager`, points Runtipi at
  `$LEGACY_HOME/runtipi`, and uses the default manager state directory. The
  manager config's port is 7790. Only these metadata fields were inspected.
- `hermes-gateway.service` runs the stock Hermes gateway from the selected
  account's Hermes installation. Its UID/home should not change.
- The top-level state names include `agents`, `apps.json`, `api-token`,
  `app-manager.json`, `builder`, `host-approvals.json`, `network-approvals.json`,
  `network-state.json`, `skills`, `linked.json`, `link-backup`, `mcp-token`,
  `backup-state.json`, `backup-identity`, `source-cache`, and `private-catalog`.
  Secret files were **not read**. `apps.json` top-level keys are `apps`, `schema`,
  `updated_at`. The shared registry exists at `runtipi/app-data/wizard-os/apps.json`;
  only its presence/name was checked, not its contents.
- Crucially, **`.wizard-os/alongside.json` is absent**. Do not infer a new Hermes
  API or shell port and move the box on that guess.
- Top-level agent directory metadata shows `archimedes`, `philosopher`, and
  `private` at UID/GID 1000. Nothing inside those directories was read.
- Agent Compose overrides bind `.wizard-os/agents/philosopher/home` and
  `.wizard-os/agents/private/home` to `/opt/data`. Those sources must follow the
  copied state tree, with all internal numeric owners unchanged.
- A core Claude override references `runtipi/app-data/core/wizard-claude/router-grants`.
  Its pointer must follow Runtipi. The grant data and login files were not opened.
- A calendar recipe has an absolute source-release mount under
  `runtipi/repos/wizardapps/apps/wizard-calendar/releases/<commit>`. Rewriting just
  `.env` and user overrides is insufficient: installed Compose metadata must also
  follow the moved tree. Slice 6 includes this metadata surface.
- No builder substring appeared in the installed Wizard-store override metadata.
  This does not establish that the builder is unused: its manager bundle exists.

Installed Wizard-store recipe directory names:

```
agent-philosopher  agent-private  browser  defleur-video  hermes-agent
transcriber  wizard-agent-service  wizard-calendar  wizard-claude
wizard-connectors  wizard-defleur-video  wizard-files  wizard-game-demo
wizard-meditation  wizard-meeting  wizard-messenger  wizard-mizel
wizard-movies  wizard-notes  wizard-notifications  wizard-profiles
wizard-quest  wizard-realtime-voice  wizard-recorder  wizard-reference
wizard-shopping  wizard-stories  wizard-transcriber  wizard-ux
wizard-wisdom
```

Names alone do not establish that every app is running or healthy. The migration
inventories running-container labels without reading container environments or
application files. The privacy seal applies equally to inactive app data.

## Proposed owner-run changes after the blockers below are mapped

1. Keep a separate administrator login, stop known manager maintenance/user
   units and Wizard containers, and make a root-only opaque archive copy of
   Runtipi, manager state, units and main-agent integration. No payload parser,
   content hash or database reader is used on this live box by this dry run.
2. Place the runtime and protected manager checkout under `/var/lib/wizard-os`.
   Change only management-tree ownership to `wizard-system`; keep app-data,
   database, media and extra-agent numeric owners. Preserve the immutable snapshot.
3. Repoint Compose metadata, shared registry pointers, builder and agent links.
   Keep the host Hermes and Claude login share under the selected agent's UID.
   Regenerate protected system units, rather than promoting user-written commands.
4. Rotate the manager token, give the agent only its scoped token/client, transfer
   **only** the Tailscale operator setting to `wizard-system`, and compare Serve
   metadata exactly. Before accepting the result, the owner must check each real
   published link from their authenticated device; the clean VM has no tailnet.
5. Record pre-existing host-access approvals, start apps, check doctor and Agent
   Service UID/health. Issue the logged, revocable seven-day grant because this
   agent previously had Docker authority. Only then remove Docker membership and
   end old account sessions so their cached groups cannot outlive the change.
6. Before normal use, exercise rollback from the separate administrator login:
   original data, units, groups, operator and tokens return from the snapshot.
   Then migrate again; rollback must not mint a second seven-day grant.

## Things that do NOT migrate cleanly yet

- **Missing alongside identity:** the current implementation deliberately refuses
  this box at inventory. A reviewed owner mapping of the actual Hermes origin,
  service/shell ports and history-sharing opt-ins is needed before migration.
  Never create that mapping by reading any private agent's files.
- **Additional user services referencing the old trees:** `mindfeed.service`,
  `wizard-heavy-siblings-main-preview.service`, `wizard-stage-agents-shim.service`,
  `wizard-stage-agent-service.service`, `wizard-stage-ready-open-ask.service`, and
  `wizard-heavy-siblings-fix-preview.service`. The inventory refuses these unknown
  writers. An owner must retire them or approve protected, path-correct equivalents
  before any copy. No service was stopped for this inventory.
- **Nondefault manager checkout:** the live unit uses the `workspace` checkout,
  not `$LEGACY_HOME/wizard-app-manager`. Review any remaining absolute executable
  pointers to that checkout; do not make a compatibility symlink back into an
  agent-writable tree. The new runtime uses the fetched protected checkout only.
- **Existing Claude grants and privileged local apps:** migrated mounts retain
  their prior owner-approved access, not a new blanket exception. Their exact
  policy/approval results need the sealed disposable rehearsal and an owner review.
- **Relocation is more than Compose:** saved per-app `app.env` contains derived
  `ROOT_FOLDER_HOST`, `APP_DATA_DIR` and `APP_SOURCE_DIR` values. Leaving those
  unchanged made admission evaluate the old paths even after copying the files.
  Migration now rewrites the finite management metadata (including recipe/source
  descriptors, rendered Compose, registry and approval records), then asks
  Runtipi's normal renderer to refresh every installed app's environment from its
  full saved form (relocating only path-valued references) before recovery.
  Unset database fields are omitted rather than posted as invalid JSON null;
  explicit false/zero values and configured ports/auth settings are preserved. The
  builder's generated discovery links and host interpreter binding move too;
  its relative `current` link and hashed release contents stay intact. Application and private-agent payloads
  are not searched for path strings; doing that would violate the privacy seal.
  The Service's verified projection of the **new** private-agent directory at an
  old **container destination** remains for opaque registry compatibility; it is
  not an old host source. The checker uses the same exact-fragment provenance
  validator as admission and still refuses any changed source or extra stale
  setting. Running-container bind sources are checked independently after recover.
- **Existing network overlays need their bridges:** the three stopped apps in
  the failed disposable run were Browser, Claude and ChatGPT. Runtipi's real logs
  name missing external `wizard-app-*` networks, not login or image failures.
  Recovery now reconciles the normal topology before starting any stopped app.
- **Approvals must move before restart:** the original installed risk snapshot
  is sealed before definitions change, and existing approval risks are translated
  to the corresponding protected paths rather than re-enrolling from rewritten
  recipes. The Agent Service override is regenerated from protected wiring;
  Hermes itself and its login/cache paths stay at the selected agent's UID.
- **Manager credential ownership:** a legacy box may not have an `agent-token`.
  Root rotation must assign both protected tokens to `wizard-system`, not leave
  a newly created 0600 token owned by root. The pre-cutover doctor also makes an
  actual agent-scoped App Manager API request; direct CLI checks alone cannot
  prove that the daemon can read its token or that the thin-client handoff works.
- **Failed-cutover rollback is a separate check:** the rehearsal injects an
  exception immediately before recovery, rolls back, and verifies original app
  containers and canaries before attempting the real migration. Rollback uses
  `wam app start`, not top-level `wam start` (which controls Runtipi itself).
- **Live authenticated links:** this inventory did not log in to Tailscale, publish
  a route, read node credentials or fetch private app pages. Real published-link
  resolution remains an owner-controlled final check.

The branch remains do-not-merge. A staged job, unit tests or this dry run is not
proof that the live box can be migrated safely today.
