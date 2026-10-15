---
name: wizard-builder
description: Use when building, porting or changing Wizard apps.
version: 0.2.0
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
Paper is retired; use one of the accepted skins from the shell registry.

## Build and verify
1. Recover one user sentence and 2–4 visible actions. Inspect installed capabilities before making a new app. Choose a lowercase id; source is separate from `media/`.
2. `terminal(command="wam new habit-tracker --request 'Build me a habit tracker'")` creates local source from hello-wizard. This template already includes daily habits, diagnostics, API, MCP and vanilla view. Other jobs require actual source edits; the request is recorded, not magically implemented by the CLI.
3. Use `read_file`, `write_file` and `patch` to adapt that source to the named job. Read the canonical [Wizard OS style/](https://github.com/humanitylabs-org/wizard-os/tree/main/style) tokens and components; the template ships their generated, hash-locked offline snapshot, not a separately designed kit. Keep `wizard-tokens.css`, `wizard-ui.css`, `boot.js` and `theme-client.js` unchanged. Put app-specific layout in `styles.css` with shared roles. Hosted the app paints no background of its own; live skin changes preserve its state. Keep Back hosted-safe: no `history.pushState` inside a frame. See references/appearance.md. Keep user text as data, use a documented API for every view action, and expose the same job through narrow MCP tools. Record copied dependency origin/license. Read `references/build.md` for the contract.
4. `terminal(command="wam check <source>")` uses the unchanged approved checker. `terminal(command="wam check <source> --proposal")` additionally applies PR24 API/view-source rules explicitly. Missing API is a transition warning in 0.2. Future 0.3 remains unapproved/unknown.
5. `terminal(command="wam install-local <source> --stage-only --run --port 18799")` builds/runs only a loopback local developer container. The result is a staging manifest, **not** the production apps registry. Run on the configured worker; record hostname, exact source hash and image id.
6. Exercise add → list → mark → readback → undo through both screen and MCP/API, then restart and read back saved data. Probe errors and root/nested Back: Back stays inside the app, Home exits the device app. A successful build alone does not finish the user's job.
7. Show the owner a tested private preview and distinguish staged from shipped. End every build by asking: "is there a component here other apps could share?" Shared extraction is optional scope.

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
