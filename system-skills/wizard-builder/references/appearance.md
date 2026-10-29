# Shared Appearance v1 source

The authoritative tokens and components live in [wizard-os/style/](https://github.com/humanitylabs-org/wizard-os/tree/main/style). Read that folder before designing. The template carries only a generated distribution snapshot, pinned by `templates/hello-wizard/appearance-provenance.json`, so it works offline. No palette or client is independently maintained here.

## Template imports

- `web/boot.js` ← `style/boot.js`: hosted marker before first paint.
- `web/wizard-tokens.css` ← `style/tokens.css`: generated from `style/tokens.json`.
- `web/wizard-sheet.js` ← `style/sheet.js`: `WzSheet.open(dialog)` wires the one pop-up's five closes (×, Esc, outside tap, Back, swipe down).
- `web/wizard-ui.css` ← `style/ui.css`: card, chip, bar, sheet, row, button, empty state, single-line add field and frame. Look only: talking to an agent is the real Chats thread (`WizardApp.nav.openThread`), never a composer built from these classes. Inside Wizard, `WizardApp` swaps this copy for the shell's live kit (`/wizard-os-home/kit/wizard-ui.css`) and layers the active skin and My skin after `styles.css`, so skins can restyle `.wz-*`; never edit the copy (the canonical hash gate detects drift), put app styles in `styles.css`.
- `/wizard-os-home/core/app-client.js`: shared capability-negotiated WizardApp v1, including Appearance v1. No copied per-app transport.
- `web/standalone.css`: retained system-media light fallback before host appearance arrives, separate from the immutable canonical CSS.
- `web/styles.css`: app-specific layout. This is the file the builder edits.

Appearance owns surfaces and ink; the app owns layout, content and intentional typography. Hosted canvas is transparent; standalone uses the skin base. The accepted skins are Nebula (default), Grove, Tide, Ember and Signal. Selection belongs in the shell. Generic light-mode transport stays available without reviving retired skins. Static type, spacing, radius and motion roles are CSS defaults; the v1 host message carries colours only.
Paper is retired; use one of the accepted skins from the shell registry.

Use the classes and visual rules documented in the canonical `APP-DESIGN-RULES.md`; the style README covers distribution only. Keep existing app accents and serif reading where intentional. Use `WizardApp.back.register/push` for domain callbacks and `back.useHistory` only for real routes. Core owns root guards; the app never copies them. Read `references/system-functions.md` and complete its mandatory QA before handoff.

## Design rules

[`style/APP-DESIGN-RULES.md`](https://github.com/humanitylabs-org/wizard-os/blob/main/style/APP-DESIGN-RULES.md) is the source; in short:

1. **The window is the page.** No `max-width` on `.wz-app` or the top frame; side padding is `--wz-gutter`. Wider windows get more columns (`.wz-grid`); only long text caps with `.wz-prose`.
2. **Width-based layout.** Container queries on `wz-app` (narrow <600, medium 600–959, wide ≥960 px), never device or user agent.
3. **No own title header** in a window; the shell names the app.
4. **One pop-up:** `<dialog class="wz-sheet">` opened with `WzSheet.open`. No `alert/confirm/prompt`, drawers or full-page takeovers.
5. **Connector apps:** `.wz-conn` status card with one `.wz-pill`, accounts as `.wz-row` (+ `.wz-meter`), one primary `.wz-button`, details in `<details class="wz-more">`.
6. **Kit only:** colours/type from kit variables, never hex/rgb/hsl in app CSS; 44 px minimum targets, 15 px body text and the 13 px label floor, through the guide’s named roles.

Style admission uses the canonical source-only `style/check_style.py` and hash policy mirrored into `wam`: new `hello-wizard` apps fail on every literal colour, font family, px type size or kit drift. Existing apps warn about baseline debt; new/changed violations fail. Review exceptions in the canonical policy with exact file hash and a reason; never silence the gate in an app. Frame `max-width` and `alert/confirm/prompt` retain their existing transition warnings.

**App checklist:** loads `wizard-ui.css`, frame has no `max-width`; looks right at 360, 700 and 1200 px in two skins (one light); every pop-up is a `.wz-sheet` closing five ways; connector apps follow pattern 5; no own title header; no hex colours.

## Refresh the immutable pin

From the WAM source bundle:

```sh
python3 tools/sync_wizard_style.py --source /path/to/wizard-os --revision <exact-committed-SHA>
python3 tools/sync_wizard_style.py --check --source /path/to/wizard-os
```

Refresh requires a matching source HEAD, committed runtime files and a passing JSON/CSS drift check. Review canonical changes in Wizard OS first; carry the generated snapshot in a WAM PR for Miguel/Oscar. `--check` without a source verifies the shipped offline snapshot; generation refuses a drifted snapshot. Schema 3 pins only canonical CSS/boot/sheet assets: shared runtime transport is served by the gateway, never copied or exempted from the mandatory source gate.

## Acceptance

Exercise a fresh app inside the actual shell in window and fullscreen. Switch all accepted skins, inspect screenshots and assert transparent backgrounds from the app body through the frame. Compare the tokens/window material to Chats. Preserve a saved record, unsent input and scroll position across the change. Check nested/root Back and cold canonical entry. Use a synthetic light context to test generic light support; it is a transport fixture, not an extra skin choice. Source lint and a compatible manifest do not prove hosted appearance.

Daily Wizard already supplies the appearance host. This PR changes the source template only; it does not deploy apps or change the manifest admission policy.
