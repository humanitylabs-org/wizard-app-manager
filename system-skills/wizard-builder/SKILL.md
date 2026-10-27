---
name: wizard-builder
description: Use when building, porting or changing apps for Wizard OS (wizard.md packages).
version: 0.2.0
author: James DeFleur (defleur-agents), Hermes Agent
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [wizard-apps, builder]
    related_skills: []
---
# Build an app for Wizard OS

Owner-facing words (wizard-os DECISIONS #84): an app with a screen is an **app**; a screenless app agents use is an **agent app**. Never say "Wizard app" to the owner.

Use the smallest artifact that finishes the owner's job. This system skill lives outside generated app skills, so installs and removals preserve it. Candidate capabilities are source scaffolding, checking, isolated local staging and publication preparation. Production install and public execution remain separate gates.

## When to use
- An owner asks to build a small app, wrap an existing app, change its screen, or prepare sharing.
- Start with an installed app's shipped skill for ordinary use; reach for the builder when code or packaging must change.

## Prerequisites
Wizard OS installs a complete, matching skill/tooling/templates bundle automatically. In `terminal`, run `export PATH="${WIZARD_BUILDER_ROOT:-$HOME/.wizard-os/builder}/current/bin:$PATH"; wam builder-bundle doctor` before building. This works with the stock local container terminal and the VM's configured SSH terminal: SSH executes on the host and uses its own `$HOME`, not a container-only path. Do not change the owner's terminal backend. Python 3.10+ and PyYAML are installed by Wizard OS (the container uses stock Hermes' Python); Docker with Compose is needed only to run a local app. If doctor fails, report the missing capability and repair with the Wizard installer, not a private checkout or personal skill. Use the configured build worker for sustained builds/tests. Keep the owner's active apps intact.

## Choose the lightest level
1. Shell setting/theme/layout: use the shell's supported configuration.
2. Report/dashboard: a page under `media/Pages/<slug>/`.
3. Small change to an installed app's screen ("move this", "hide that"): an overlay with `wam views customize <app> --mode overlay`. See `references/views.md`.
4. Redesign of an installed app's screen: an own version with `wam views customize <app>`, edited, then `wam views apply <app>`; it survives app updates. See `references/views.md`.
5. Independent process/database or install on another Wizard: app source under `dev/<id>/`.
6. Existing upstream app: add metadata, packaging, MCP and a skill in a wrapper; see `references/port.md`.

## Supported shell skins
Use the shell registry as the source of truth. The accepted set is **Nebula (default), Grove, Tide, Ember, Signal**. Paper is retired by the owner. Generated apps inherit the active shell appearance; keep skin selection in the shell. Use the accepted set for examples and QA, and map any saved Paper choice to Nebula.

## Chat is the Chats thread (James, 22 Oct 2026; mandatory)
Any feature where the owner talks to a model ("Ask AI", "discuss", "assistant", "chat about this") opens the real Chats thread over the app window with `WizardApp.nav.openThread(sessionId, {reply})`; the app supplies only context and the item → session mapping. The `wizard-ui.css` kit is styling, never a licence to build a composer or transcript. Isolation requirements are met with an agent bound to the app, not a separate UI. `wam check` fails `app-chat` markup. Read `references/chat.md` before planning any conversational feature; its parity + fault-injection gate is the definition of done.


## Finishing actions leave the screen (James, 25 Oct 2026; every build)
Dismiss, hide, skip, archive, delete, mark done, save-and-close: the one tap does the action AND returns the owner to the screen he came from. Never leave him on a confirmation view that needs a second tap (Back, ✕, Done). In a framed Wizard app call `WizardApp.nav.back()` right after the write succeeds (it runs the shell's Back past the app's own views; it returns false when the app is hidden or unfocused, then show a toast instead). In a list, the item disappears in place. Undo lives in a toast or inside the app, never as a step before leaving. QA asserts one tap lands on the previous screen (model: wizard-ux `tools/qa-app-done-returns.cjs`).

## Reuse check (mandatory, before writing code)
One implementation per capability. Before planning, read the Wizard OS component inventory (`docs/COMPONENTS.md` in wizard-ux; its App section is the `WizardApp` facade). List every capability the job needs (chat, draft, back, theme, share, capture, upload, settings, auth, markdown, errors, model picker, reading place, anything drawn or animated) and write one line each in the plan, worker brief, PR body and completion report:
```
Reuse:
- <capability>: REUSE <entry point from its row>
- <capability>: EXTEND <module> because <row> lacks <thing> (row updated)
- <capability>: NEW because no row covers it (lands in the shell's web/core with a row, never in the app)
Visual: <side-by-side screenshot of the existing element and yours, for anything drawn or animated>
```
Tests for a REUSE line exercise the shared path named in its row, not only the outcome. Delegated workers get this block and the inventory in their brief. Why: an app's homemade Ask AI chat and a PNG orb were rebuilt beside polished shared components.
## Shared system functions (James-approved, mandatory)
Read `references/system-functions.md` **before** editing any view. Always reuse `WizardApp` from `/wizard-os-home/core/app-client.js`; apps own task state/domain callbacks, never capture, sharing, root exit guards, system theme storage, SW or install implementations. This reference and its executable gate ship in the installed builder skill, so future agents load the rule automatically. No new `wizard.md` field is approved by this work.

## Build and verify
1. Recover one user sentence and 2–4 visible actions. Inspect installed capabilities before making a new app. Choose a lowercase id; source is separate from `media/`.
2. `terminal(command="wam new habit-tracker --request 'Build me a habit tracker'")` creates local source from hello-wizard. This template already includes daily habits, diagnostics, API, MCP and vanilla view. Other jobs require actual source edits; the request is recorded, not magically implemented by the CLI.
3. Use `read_file`, `write_file` and `patch` to adapt that source to the named job. Build the screen from the canonical Wizard OS `style/` snapshot (`wizard-tokens.css`, `wizard-ui.css`, `wizard-sheet.js`, `boot.js`), pinned offline by `appearance-provenance.json`; never edit those generated files and keep its `boot.js` + shared `/wizard-os-home/core/app-client.js` inclusion: the app then looks like Chats in every skin, follows live skin changes, and paints no background of its own when hosted (the window glass is its canvas). Put only app-specific styles in `styles.css`, using the kit's variables, never literal colours. Follow the [app design rules](https://github.com/humanitylabs-org/wizard-os/blob/main/style/APP-DESIGN-RULES.md): the window is the page (no `max-width` on the frame; more columns via `.wz-grid` as it widens), layout by window width (container queries, never device), one pop-up (`.wz-sheet` via `WzSheet.open`, never `alert/confirm/prompt`), connector apps use status card + accounts + one action, no hex colours. Register domain-only Back callbacks with `WizardApp.back.register/push`; routed apps use `back.useHistory`. Real route history is allowed, app root history sentinels are not. See references/appearance.md. Keep user text as data, use a documented API for every view action, and expose the same job through narrow MCP tools. Record copied dependency origin/license. Read `references/build.md` for the contract.
4. `terminal(command="wam check <source>")` runs the unchanged approved manifest checker **plus the mandatory shared-system-function source gate**. Its `design:` warnings flag rule breaks (frame `max-width`, colour literals, `alert/confirm/prompt`); fix them. `terminal(command="wam check <source> --proposal")` additionally applies PR24 API/view-source rules explicitly. Missing API is a transition warning in 0.2. Future 0.3 remains unapproved/unknown.
5. `terminal(command="wam install-local <source> --stage-only --run --port 18799")` builds/runs only a loopback local developer container. The result is a staging manifest, **not** the production apps registry. Run on the configured worker; record hostname, exact source hash and image id.
6. **Mandatory QA before handoff:** follow the executable and browser gate in `references/system-functions.md`, not only `wam check`. On the configured worker record candidate/source hashes, exact commands and receipts for phone + desktop, light + dark, window + fullscreen, state-preserving live theme, root/nested/overlay Back and on-screen close parity. Exercise sharing/capture when the app uses them, including cancelled/unsupported/denied/busy/recovery and held updates. Exercise add → list → mark → readback → undo through both screen and MCP/API, then restart and read back saved data. Back stays inside the app; Home exits the device app. A successful build or source lint alone does not finish the user's job.
7. Show the owner a tested private preview and distinguish staged from shipped. Do not reimplement shared system functions. New task-specific shared extraction outside the agreed API remains separate optional scope.

## Share
`terminal(command="wam publish <source> --repo owner/name --output <outside-source-plan.json>")` prepares the exact source hash, files, source patch, public GitHub settings/topic, catalog request and separate signing handoff. Read `references/publish.md`. Public repo creation, pushing, catalog publication and signing require the owner's explicit approval of that prepared artifact. `--execute` refuses: this candidate has no public executor. Never call prepared publication published.

## Pitfalls
- Generated app skills are managed by the registry. The OS builder is separately registered through `skills.external_dirs`; `wizard link` handles alongside installs, and the installer prepares stock containers. `wam builder-bundle install` repairs the bundle; `wam builder-bundle fresh` prepares the stock container mount/discovery (restart the agent to adopt a new mount). Do not manually copy it into personal skills.
- `<source>` placeholders are arguments the agent resolves, not commands to paste literally.
- Local stage is a developer proof; production install, launcher refresh and same-origin routing belong to the lifecycle integration.
- The installed content-addressed bundle includes all templates, references, UI assets and Python modules; a source checkout is not needed during use. Bare third-party wheels without these assets are not a supported provisioning source.
- Proposed view machinery, cheap-model evaluation and public publication stay open until their own evidence exists.

## Verification
Read the real tool outputs, check persisted state after each mutation, load this skill through stock Hermes in a fresh profile, and rehearse the complete source bundle on a second isolated box. Finish with preview, candidate revision, verified behavior and one real remaining gate.
