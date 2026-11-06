# App-owned private data permissions

An app whose init/runtime owns its data permissions declares `"private_data": true` in the JSON recipe selected by `wizard.md`'s `runtime.config`. This is an optional boolean; omitted or false keeps upstream Runtipi's permission behaviour. Do not opt an app in merely because it handles private information: its init/runtime must create and own any new writable bind directories itself.

For an opted-in recipe, `AppFilesManager.setAppDataDirPermissions()` does not invoke recursive chmod, chown, or walk app-data. Install, start and update use that single guard. Update latches the union of the installed and target policy **before target preparation and backup** and retains it through recovery, including recovery to an older unflagged recipe. A malformed or unreadable installed declaration fails closed rather than assuming legacy permission widening is permitted.

This does not repair modes an old updater already widened. It does not disable app init helpers, grant revocation, network policy or backups. A newly created bridge folder still receives its established initial access mode; an opted-in app's existing bridge folder modes are left alone. A changed or missing grant file can still be created or reconciled. That is required security state management, not a zero-side-effects update promise.

## Root cause

`packages/backend/src/modules/apps/app-files-manager.ts`'s `setAppDataDirPermissions()` ran `chmod -Rf a+rwx <app-data>` unconditionally. `AppLifecycleCommand.ensureAppDir()` calls it during target preparation, **before the stopped backup**, and during actual deployment. Both install and update call it again after compose up, undoing the permissions a root init helper just established. The running manager identified itself through the OCI source label as this repository, revision `9309d85767203983aa18917e53660d2c968bd2aa`.

## Unchanged grants

`WizardStorage.prepare()` still calls global grant reconciliation. There is deliberately no cache keyed only by requested grants: missing keys, provider changes and revocation must remain repairable. Existing `atomic()` compares each desired grant document's bytes before replacement. `WizardStorage.writeGrants()` now compares authoritative state ignoring its timestamp before replacing it, and compares each desired registry mirror's bytes before replacing that mirror. Both provider-first save phases still run, so stale or newly created mirrors are repaired even when authority is unchanged, and interrupted-save recovery retains its ordering. Thus a settled update does not rewrite manager state or registry mirrors. Non-private bridge chmod is conditional on an actual mode mismatch; private apps' existing bridge directories are never chmodded. Network enforcement is unchanged and is **not claimed** to be side-effect-free when network state has drifted.

## Compatibility survey

Recipe/config and container `User` metadata only; no owner app-data, message content, databases, container env/logs/filesystems or private agent homes were inspected. Directory presence in the installed recipe collection is not proof the app is currently running. The survey found 34 recipe directories, all without the new flag. No other app is opted in by this change.

Explicit non-root services with app-data binds and no inline permission initialiser were found in: `wizard/defleur-video`, `wizardapps/airlock`, `wizardapps/defleur-video`, `wizardapps/mizel`, `wizardapps/morning`, `wizardapps/notes`, `wizardapps/notifications`, `wizardapps/profiles`, `wizardapps/reference`, `wizardapps/stories`, `wizardapps/wizard-connection-router`, and `wizardapps/wizard-shopping`. These are **potential dependencies** on the existing chmod, not a claim that changing their real data modes was tested. Their default behaviour is retained. `core/transcriber` uses an image-default non-root user (`ubuntu`) with an app-data bind, so also retains the legacy default.

`core/wizard-chatgpt`, `wizardapps/tasks`, `wizardapps/wizard-recorder`, and `wizardapps/wizard-tinfoil` have root data-init helpers but remain unflagged: this fix does not assume those helpers cover every path. Messenger has root `unified-init` and `connector-init` helpers and explicit `1000:1000` services; its separate source PR is the first opt-in. No Messenger install, update, restart or owner-data mode inspection is part of this work.

The remaining checked recipes were: `core/browser`, `core/wizard-claude`, `wizard/hello-wizard`, `wizardapps/agent-philosopher`, `wizardapps/agent-private`, `wizardapps/connected-accounts`, `wizardapps/food`, `wizardapps/meditate`, `wizardapps/movies`, `wizardapps/outline`, `wizardapps/search-preview`, `wizardapps/wisdom`, `wizardapps/wizard-calendar`, `wizardapps/wizard-claude`, `wizardapps/wizard-files`, and `wizardapps/wizard-quest`. Some use image-default users or named storage rather than direct app-data binds; none is made to depend on new permission behaviour.

## Regression checks

After `bun install --frozen-lockfile --ignore-scripts` and building `packages/common`:

- `node --test tests/private-data-permissions.test.mjs` exercises real production install/update/ensure, the real permission guard, real filesystem operations and the real BackupManager/ArchiveService. Only DI, Docker and compose generation are fixtures. Assertions cover post-init install, existing and first opt-in update, sticky old opt-in, rollback to an unflagged recipe, 0700/0600 in the real archive and legacy chmod/fail-closed behaviour. `PERMISSION_TEST_BASE_REV=<base>` loads the production files from a baseline commit for the red regression run.
- `packages/backend/src/modules/docker/__tests__/wizard-foreign-grants.test.ts` checks settled prepare/reconcile preserves grant keys, inode, mtime, ctime and modes of synthetic private bridge paths, state and a registry mirror, then checks actual revocation still works.

All fixtures are synthetic and live outside installed app-data. Existing CI executes these tests; do not use a live private app as a test fixture.
