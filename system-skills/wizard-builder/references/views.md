# Changing how an installed app looks (custom views)

Candidate for wizard-os #30 (proposal spec/VIEWS.md). Tool: `wam views …`. State lives in `~/.wizard-os/views/<app-id>/`; no folder means the app's own screen, unchanged.

Pick the lightest step: shell setting → page → overlay → own version → view app.

## Overlay ("move the add button to the top", "hide that", "bigger text")
1. `wam views customize <app> --mode overlay` → edit `overlay/overlay.css` (optional `overlay.js`).
2. List every selector the change depends on in `overlay/overlay.json` as `{"requires": ["#add-form"]}`. Prefer ids and stable classes.
3. `wam views apply <app>`. It refuses if the app's screen lacks a required selector.
4. The door adds the two tags to the app's own HTML at `/apps/<app>/`. If an update or the browser finds a required selector gone, or the script throws, the overlay switches itself off and the note says which change stopped applying. Fix it, then `apply` again.

## Own version ("redesign this screen")
1. `wam views customize <app>` copies the screen source (`view.source.path`) into `src/` (git: `upstream` = app, `main` = owner) and prints the folder to edit.
2. Edit only files under that folder. Keep calls on relative `api/…` paths: the door forwards `/views/<app>/api/…` to the app's declared API and nowhere else.
3. `wam views apply <app>` commits, builds and switches. The build runs inside the app's own container (no network, read-only root); a failed build switches nothing.
4. Tell the owner what changed and open `/views/<app>/`.

## After an app update
Run `wam views update <app>` (the App Manager should call it after every update).
- `active`: app changes merged in, rebuilt, owner changes kept.
- `update_conflict`: the app changed the same lines. The owner's version stays on screen. Show the owner both lines in plain words and ask which to keep, then `wam views resolve <app> --keep mine|upstream`. Non-conflicting app changes come in either way.
- `update_build_failed`: the last working version stays on screen; fix the source, then `apply`.
- Overlay `suspended`: a required selector is gone; adjust the overlay, then `apply`.

## Undo
"Put it back to normal": `wam views revert <app>`. History stays; `wam views reactivate <app>` brings it back.

## Status and serving
`wam views status <app>`, `wam views index` (the reference door's URLs). `wam views serve` remains the standalone reference door; it is not required by the shell.

### Shell integration (Skins and Views slice 5)

- WAM publishes the successful build and a small read-only `views/<id>/view.json` projection into the **existing** Runtipi `app-data/wizard-os` share, on apply/update/revert/reactivate and on `wam apps`. The owner's `src/`, git history and build logs are never copied. `RUNTIPI_ROOT` or this home's `app-manager.json:runtipi_root` selects that share; an isolated home without either never writes another install.
- The Agent Service reads the projection beside its configured `apps.json` and reports each app's `view: {mode, url}`. It serves own screens at `/wizard/views/<id>/` under its already-published `/wizard` gateway. This is the shell-accessible equivalent of the reference door's `/views/<id>/`, without a new port or Serve mount. Existing builds are backfilled by `wam apps`.
- Both shell layouts select the own URL, keep Appearance v1 and normal app/Back lifecycle, and show **Your version**. Failed HTML/assets return to the original with a note naming the file; this is a session fallback, not deletion or permanent deactivation.
- **Use original** opens shell Settings. Only its genuine confirmation tap sends `POST /wizard-os-home/store-install/_views/<id>` with `{\"action\":\"revert\"}`. This is the existing Settings/Store listener and its owner/Tailscale/Origin/JSON gate; it calls `views.revert` and returns only app/active. The shell reads it and the registry back before reporting the switch. No iframe message can authorize it.
- Relative `api/…` uses a 307 to the original app's same-origin, declared API path, keeping that route's existing authentication/CSRF/grant handling; it is not a new privileged proxy. External-origin app API URLs are deliberately refused. Overlay mode retains the original URL and the reference door's existing overlay injection; this slice does not introduce a second injector.

Own-version creation still requires locally available screen source, as `wam views customize` already documents; this does not fetch otherwise unavailable vendor source. No shell, service, image or owner state is deployed by these changes.
