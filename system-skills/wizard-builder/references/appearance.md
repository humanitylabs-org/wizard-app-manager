# App styling default (MVP)

Goal: an app the agent builds should look like part of Wizard OS, in whichever skin the owner wears, without the agent choosing a single colour.

## What the template ships
- `web/boot.js`: marks a hosted page before first paint, so it never flashes a canvas.
- `web/theme-client.js`: appearance v1 client from the App styling lane, byte-for-byte (provenance in `appearance-provenance.json`). The shell sends skin, colour scheme, presentation and colour roles; the client sets `--wz-*` variables and `data-skin` / `data-color-scheme` / `data-presentation` on `<html>`.
- `web/wizard-ui.css`: the kit. It copies Chats' visual language (system UI type at the shell's sizes, ink hierarchy, hairline separators, faint tinted fills, pill input, round accent action) and derives every value from the `--wz-*` roles.
- `web/styles.css`: the app's own layout only, written with the kit variables.

## Rules
1. Hosted, paint nothing behind the app. The shell's window glass is the canvas, as it is for Chats. Only a standalone tab paints `--canvas`. An opaque app background is what makes apps look "jet black" next to Chats.
2. Colours come from kit variables: `--ink`, `--ink-2`, `--ink-3`, `--line`, `--line-2`, `--fill`, `--fill-2`, `--accent`, `--on-accent`, `--danger`. No hex values in app CSS except inside the kit's fallbacks.
3. Use kit components before inventing: `.wz-app` (frame), `.wz-head` (hidden in a window, where the shell's title bar already names the app), `.wz-scroll`, `.wz-sec` + `.wz-sec-h`, `.wz-list` + `.wz-row` (+ `.wz-tile`, `.wz-check`, `.wz-main b/small`), `.wz-empty`, `.wz-compose` + `.wz-input` + `.wz-send`, `.wz-status`.
4. Layout, content and intentional typography (a reader's serif, a music app's artwork) stay the app's. Skins re-colour; they do not redesign.
5. Back: hosted apps answer `window.__wizardCloseOverlay()` (true only when they closed something of their own) and never push history inside a frame. Standalone: first Back at the root warns, a second within 2 s may leave.

## Verify before showing the owner
Open the app inside the shell (not only standalone), switch every skin, and look: the app must sit on the same glass as Chats, text readable, no black or white slab. Check a saved record, an unsent input and scroll position survive a skin change without reload; window and full screen; nested and root Back; top-level history does not grow when the app loads.

## MVP limits
The shell side (transparent frame once an app acknowledges appearance v1) is proven in the builder preview and handed to the App styling owner; daily Wizard does not have it yet. Skin shape (Signal's square corners) is keyed on `data-skin` in the kit until the protocol carries shape and display-type roles. Fonts stay system UI; skin display faces are a later protocol role.
