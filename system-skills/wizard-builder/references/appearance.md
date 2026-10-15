# Shared Appearance v1 source

The authoritative tokens and components live in [wizard-os/style/](https://github.com/humanitylabs-org/wizard-os/tree/main/style). Read that folder before designing. The template carries only a generated distribution snapshot, pinned by `templates/hello-wizard/appearance-provenance.json`, so it works offline. No palette or client is independently maintained here.

## Template imports

- `web/boot.js` ← `style/boot.js`: hosted marker before first paint.
- `web/wizard-tokens.css` ← `style/tokens.css`: generated from `style/tokens.json`.
- `web/wizard-ui.css` ← `style/ui.css`: card, chip, bar, sheet, row, button, empty state, composer and frame.
- `/wizard-os-home/core/app-client.js`: shared capability-negotiated WizardApp v1, including Appearance v1. No copied per-app transport.
- `web/standalone.css`: retained system-media light fallback before host appearance arrives, separate from the immutable canonical CSS.
- `web/styles.css`: app-specific layout. This is the file the builder edits.

Appearance owns surfaces and ink; the app owns layout, content and intentional typography. Hosted canvas is transparent; standalone uses the skin base. The accepted skins are Nebula (default), Grove, Tide, Ember and Signal. Selection belongs in the shell. Generic light-mode transport stays available without reviving retired skins. Static type, spacing, radius and motion roles are CSS defaults; the v1 host message carries colours only.
Paper is retired; use one of the accepted skins from the shell registry.

Use the classes documented in the canonical style README. Keep existing app accents and serif reading where intentional. Use `WizardApp.back.register/push` for domain callbacks and `back.useHistory` only for real routes. Core owns root guards; the app never copies them. Read `references/system-functions.md` and complete its mandatory QA before handoff.

## Refresh the immutable pin

From the WAM source bundle:

```sh
python3 tools/sync_wizard_style.py --source /path/to/wizard-os --revision <exact-committed-SHA>
python3 tools/sync_wizard_style.py --check --source /path/to/wizard-os
```

Refresh requires a matching source HEAD, committed runtime files and a passing JSON/CSS drift check. Review canonical changes in Wizard OS first; carry the generated snapshot in a WAM PR for Miguel/Oscar. `--check` without a source verifies the shipped offline snapshot; generation refuses a drifted snapshot. Schema 3 pins only canonical CSS/boot assets: shared runtime transport is served by the gateway, never copied or exempted from the mandatory source gate.

## Acceptance

Exercise a fresh app inside the actual shell in window and fullscreen. Switch all accepted skins, inspect screenshots and assert transparent backgrounds from the app body through the frame. Compare the tokens/window material to Chats. Preserve a saved record, unsent input and scroll position across the change. Check nested/root Back and cold canonical entry. Use a synthetic light context to test generic light support; it is a transport fixture, not an extra skin choice. Source lint and a compatible manifest do not prove hosted appearance.

Daily Wizard already supplies the appearance host. This PR changes the source template only; it does not deploy apps or change the manifest admission policy.
