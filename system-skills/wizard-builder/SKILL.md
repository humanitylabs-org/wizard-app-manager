---
name: wizard-builder
description: Use when building, porting or changing apps for Wizard OS (wizard.md packages).
version: 0.3.0
author: James DeFleur (defleur-agents), Hermes Agent
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [wizard-apps, builder]
    related_skills: []
---
# Build an app for Wizard OS

Build from tested parts; the platform is tested once, not per app. A new app targets **about 20 minutes and 30 tool
calls**. Never write new test harnesses, Playwright scripts or release packets for a single app: `wam verify` is the
whole pre-install check, and the owner using the app is the final one.

Owner-facing words: an app with a screen is an **app**; a screenless app agents use is an **agent app**. Never say
"Wizard app" to the owner. Visual authority: [APP-DESIGN-RULES.md](https://github.com/humanitylabs-org/wizard-os/blob/main/style/APP-DESIGN-RULES.md).

## Prerequisites
In `terminal`: `export PATH="${WIZARD_BUILDER_ROOT:-$HOME/.wizard-os/builder}/current/bin:$PATH"; wam builder-bundle doctor`.
This works in the stock container terminal and over the VM's SSH terminal; do not change the owner's terminal backend.
`wam verify` needs Docker; a headless Chromium is optional (screenshots are skipped with a note without one). If doctor
fails, report the missing piece and repair with the Wizard installer, not a private checkout. Keep the owner's apps intact.

## Choose the lightest level
1. Shell setting/theme/layout: the shell's own settings.
2. Report/dashboard: a page under `media/Pages/<slug>/`.
3. Small change to an installed app's screen: `wam views customize <app> --mode overlay` (`references/views.md`).
4. Redesign of an installed app's screen: `wam views customize <app>`, edit, `wam views apply <app>`.
5. A new app with its own data: `wam new` (below).
6. Wrapping an existing upstream app: `references/port.md`.

## Build
1. **One sentence and 2–4 visible actions.** Check installed apps first; maybe one already does the job. The id is the app's name in kebab-case
   (`wam check` enforces it): name `To-dos` means `wam new to-dos --name 'To-dos'`.
2. **Start from the closest kind:**
   - `wam new <id> --kind list --request '<sentence>'`: groups that fold, one item per line, tap to edit, Enter adds
     the next line below, done/undo, saved order, SQLite, HTTP API, MCP and skill. To-do, shopping, reading, packing,
     gift, chore lists: most of the job is editing `web/list.json` (title, item and group nouns, two or three extra
     fields, empty-screen words). Change code only for behaviour the config cannot express.
   - `wam new <id> --request '<sentence>'` (kind `hello`, the default): the minimal reference (daily habits, API, MCP,
     vanilla view) for jobs no kind covers. Declare the app's round trip in `verify.json`.
3. **Edit with `read_file`/`patch`.** Keep the shared kit untouched: `boot.js`, `wizard-tokens.css`, `wizard-ui.css`,
   `wizard-sheet.js` and `/wizard-os-home/core/app-client.js` (the `WizardApp` client). App styles go in `styles.css`
   with kit variables only, never literal colours. The window is the page: no `max-width` on the frame, `.wz-grid` for
   columns. Every view action uses the documented API; expose the same job through narrow MCP tools and the app's
   `skill/SKILL.md`. Keep user text as data. After source edits run `wam package-inline <source>`.
4. **`wam verify <source>`.** One fixed command: builds the container, starts it on loopback, runs the API round trip
   (create, list, update, restart the container, read back; built in for `--kind list`, from `verify.json` otherwise),
   checks MCP lists tools, runs `wam check`, and saves one phone-width screenshot per skin. It cleans up after itself,
   prints elapsed time and memory, and exits non-zero on failure. Look at the screenshots. Run heavy builds on the
   configured build worker when the installation names one: copy the bundle alongside the source
   (`rsync -a "$(readlink -f ~/.wizard-os/builder/current)/" worker:wizard-builder/`) and run
   `wizard-builder/bin/wam verify <source>` there by path.
5. **If it passes, install and hand over.** Install through the installation's App Manager lifecycle with its
   authenticated same-origin route (for a local proof, `wam install-local <source> --run`), open it in Wizard at
   `?open=<app-id>`, and give the owner the link. The owner
   using it is the final check; fix what he reports in the app and ship the fix the same way.
6. **Stop rule.** Five failed `wam verify` runs in total ends the loop: stop and report what works, what fails (the
   last receipt) and the one decision or fix needed. Do not build a harness to investigate.

## Chat is the Chats thread (James, 22 Oct 2026)
Any feature where the owner talks to a model ("Ask AI", "discuss", "assistant") opens the real Chats thread over the
app with `WizardApp.nav.openThread(sessionId, {reply})`; the app supplies only context and the item → session mapping.
Never build a composer or transcript. `wam check` fails `app-chat` markup. Read `references/chat.md` before planning a
conversational feature.

## Every tap registers before I/O
Update visible state synchronously in the handler, before awaiting network, storage, a poll or a render guard; then
reconcile in the background. Keep pending state across stale refreshes, deduplicate repeat taps (one `operation_id` per
change), and on failure restore the state and show one plain error with Retry. For irreversible or external actions
(send, pay, publish) show honest pending feedback, never a false "Sent". The list kind already does this; keep its
`enqueue` pattern. This is how the code must behave, not an automatic cross-app lint or release blocker, and not a
reason to write a per-app harness (`references/system-functions.md`).

## Finishing actions leave the screen (James, 25 Oct 2026)
Dismiss, archive, reversible delete, mark done, save-and-close: one tap updates local state and returns the owner to
where he came from before awaiting the write (in a framed app feature-detect `WizardApp.nav.back()` and call it in the
same handler). In a list the item changes in place. Undo lives in a toast or in the app, never as a step before leaving.

## Back never exits; tap outside closes
Register in-app steps with `WizardApp.back.register({canGoBack, back})` or `back.push`; routed apps use
`back.useHistory`. Never listen to `popstate` or push history entries yourself. Every pop-up is
`<dialog class="wz-sheet">` opened with `WzSheet.open` (tap outside, ×, Esc, swipe and Back close it), never
`alert/confirm/prompt` or a homemade modal; `wam check` fails all three in builder-made apps. If an app must own a
`<dialog>`, close it only when both pointerdown and click land outside the panel, and swallow the release click of the
long-press that opened it.

## App changes ship in the app (James, 29 Oct 2026)
Approved workflows, styles, presets, prompt rules and validators go into the app's source (`SKILL.md`, `wizard.md`,
config, validators), then out through the normal release path, with the owner's approval wherever publication needs it.
Once installs update, check the installed app's version and every file you changed against the release. Never record
them in an agent's own skills, notes or memory, and never edit an installed copy: App Manager replaces it. An unmerged
side branch is not shipped.

## Reuse check, before writing code
One implementation per capability. Read the component inventory (`docs/COMPONENTS.md` in wizard-ux; its App section is
the `WizardApp` facade) and write one line per capability in the plan and report; delegated workers get this block and
the inventory pasted into their brief:
```
Reuse:
- <capability>: REUSE <entry point from its row>
- <capability>: EXTEND <module> because <row> lacks <thing>
- <capability>: NEW because no row covers it (lands in shared core with a row, never in the app)
```
Use shared voice (`WizardApp.capture`/`.voice`/`.voiceInput`), chat (`WizardApp.nav.openThread`), orb (`Orb.create`)
and reading position (`WizAppReadingPosition`). App PRs run `humanitylabs-org/wizard-ux/.github/workflows/reuse-check-app.yml@main`.
Shared system functions (capture, share, Back, theme, updates) come only from `WizardApp`; see
`references/system-functions.md`.

## Port from live
Porting or rebuilding an existing app starts from the source serving live today and keeps every deployed feature; pass
the release owner's `app-parity-gate <live-source> <candidate>` before cutover.

## Skins and pinned apps
The shell registry is the source of truth: Nebula (default), Grove, Tide, Ember, Signal. Paper is retired. Apps inherit
the shell's skin; never add a skin switch. Pinned apps: top bars include `var(--wos-cutout, 0px)` (the kit's `.wz-head`
already does).

## Share
`wam publish <source> --repo owner/name --output <plan.json>` prepares the exact source hash, files, GitHub settings,
catalogue request and signing handoff (`references/publish.md`). Public repos, pushes, catalogue publication and
signing require the owner's explicit approval of that prepared artifact. `--execute` refuses. Never call a prepared plan published.

## Pitfalls
- `wam check` is strict for builder-made apps: new style, Back and pop-up violations fail; older apps get warnings.
- Generated app skills are managed by the registry; the builder skill is registered separately. Repair the bundle with
  `wam builder-bundle install` only when doctor fails; it keeps a newer bundle. Never copy it into personal skills.
- A `summary` containing `: ` is fine; `wam package-inline` quotes it.
- `<source>` and `<id>` are placeholders you resolve, not text to paste.
- `install-local` is a developer proof; production install and same-origin routing belong to App Manager.
