---
name: wizard-builder
description: Use when building, porting or changing Wizard apps.
version: 0.1.1
author: James DeFleur (defleur-agents), Hermes Agent
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [wizard-apps, builder]
    related_skills: []
---
# Build a Wizard app

Use the smallest artifact that finishes the owner's job. This system skill lives outside generated app skills, so installs and removals preserve it. Candidate capabilities are source scaffolding, checking, isolated local staging and publication preparation. Production install and public execution remain separate gates.

## When to use
- An owner asks to build a small app, wrap an existing app, change its screen, or prepare sharing.
- Start with an installed app's shipped skill for ordinary use; reach for the builder when code or packaging must change.

## Prerequisites
A complete Wizard App Manager source bundle with `wam`, Python 3.10+ and PyYAML; Docker with Compose for local execution. Source-bundle install: `terminal(command="python3 -m pip install -e <source-folder>")`. Use the host's configured build worker for sustained builds/tests. Run everything in an isolated developer home; keep the owner's active apps intact.

## Choose the lightest level
1. Shell setting/theme/layout: use the shell's supported configuration.
2. Report/dashboard: a page under `media/Pages/<slug>/`.
3. Small change to an installed app's screen ("move this", "hide that"): an overlay with `wam views customize <app> --mode overlay`. See `references/views.md`.
4. Redesign of an installed app's screen: an own version with `wam views customize <app>`, edited, then `wam views apply <app>`; it survives app updates. See `references/views.md`.
5. Independent process/database or install on another Wizard: app source under `dev/<id>/`.
6. Existing upstream app: add metadata, packaging, MCP and a skill in a wrapper; see `references/port.md`.

## Supported shell skins
Use the shell registry as the source of truth. The accepted set is **Nebula (default), Grove, Tide, Ember, Signal**. Paper is retired by the owner. Generated apps inherit the active shell appearance; keep skin selection in the shell. Use the accepted set for examples and QA, and map any saved Paper choice to Nebula.

## Shared system functions (James-approved, mandatory)
Read `references/system-functions.md` **before** editing any view. Always reuse `WizardApp` from `/wizard-os-home/core/app-client.js`; apps own task state/domain callbacks, never capture, sharing, root exit guards, system theme storage, SW or install implementations. This reference and its executable gate ship in the installed builder skill, so future agents load the rule automatically. No new `wizard.md` field is approved by this work.

## Build and verify
1. Recover one user sentence and 2–4 visible actions. Inspect installed capabilities before making a new app. Choose a lowercase id; source is separate from `media/`.
2. `terminal(command="wam new habit-tracker --request 'Build me a habit tracker'")` creates local source from hello-wizard. This template already includes daily habits, diagnostics, API, MCP and vanilla view. Other jobs require actual source edits; the request is recorded, not magically implemented by the CLI.
3. Use `read_file`, `write_file` and `patch` to adapt that source to the named job. Build the screen from the template's `wizard-ui.css` kit (rows, section labels, composer, empty state) and keep its `boot.js` + shared `/wizard-os-home/core/app-client.js` inclusion: the app then looks like Chats in every skin, follows live skin changes, and paints no background of its own when hosted (the window glass is its canvas). Put only app-specific styles in `styles.css`, using the kit's variables, never literal colours. Register domain-only Back callbacks with `WizardApp.back.register/push`; routed apps use `back.useHistory`. Real route history is allowed, app root history sentinels are not. See references/appearance.md. Keep user text as data, use a documented API for every view action, and expose the same job through narrow MCP tools. Record copied dependency origin/license. Read `references/build.md` for the contract.
4. `terminal(command="wam check <source>")` runs the unchanged approved manifest checker **plus the mandatory shared-system-function source gate**. `terminal(command="wam check <source> --proposal")` additionally applies PR24 API/view-source rules explicitly. Missing API is a transition warning in 0.2. Future 0.3 remains unapproved/unknown.
5. `terminal(command="wam install-local <source> --stage-only --run --port 18799")` builds/runs only a loopback local developer container. The result is a staging manifest, **not** the production apps registry. Run on the configured worker; record hostname, exact source hash and image id.
6. Exercise add → list → mark → readback → undo through both screen and MCP/API, then restart and read back saved data. Probe errors and root/nested Back: Back stays inside the app, Home exits the device app. A successful build alone does not finish the user's job.
7. Show the owner a tested private preview and distinguish staged from shipped. Do not reimplement shared system functions. New task-specific shared extraction outside the agreed API remains separate optional scope.

## Share
`terminal(command="wam publish <source> --repo owner/name --output <outside-source-plan.json>")` prepares the exact source hash, files, source patch, public GitHub settings/topic, catalog request and separate signing handoff. Read `references/publish.md`. Public repo creation, pushing, catalog publication and signing require the owner's explicit approval of that prepared artifact. `--execute` refuses: this candidate has no public executor. Never call prepared publication published.

## Pitfalls
- Generated skills are managed by the app registry. Put the builder in `system-skills/` and link it to Hermes using `wam builder-skill`; no per-app MCP connection/restart.
- `<source>` placeholders are arguments the agent resolves, not commands to paste literally.
- Local stage is a developer proof; production install, launcher refresh and same-origin routing belong to the lifecycle integration.
- Current builder requires a complete source checkout; wheel-only installation does not include template assets yet.
- Proposed view machinery, cheap-model evaluation and public publication stay open until their own evidence exists.

## Verification
Read the real tool outputs, check persisted state after each mutation, load this skill through stock Hermes in a fresh profile, and rehearse the complete source bundle on a second isolated box. Finish with preview, candidate revision, verified behavior and one real remaining gate.
