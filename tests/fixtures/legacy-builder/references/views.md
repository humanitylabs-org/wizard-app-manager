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
`wam views status <app>`, `wam views index` (what the launcher opens). The door (`wam views serve`) is the reference for the Agent Service's same-origin routes; routes come from disk and survive restarts. Keep custom views only in `~/.wizard-os/views/`; change the look through a view, leaving the installed app untouched.
