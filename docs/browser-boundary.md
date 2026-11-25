# Browser owner boundary (App Manager side)

Issue: humanitylabs-org/wizard-os#133. Agent Service side: wizard-agent-service `server/browser-boundary.mjs`.

## Why

App screens load on the same origin as the shell. A script on an app page therefore sends the owner's
Tailscale login, the shell's `Origin` and `Sec-Fetch-Site: same-origin`, exactly like the shell. Those prove
the device, not that the shell asked. The shell-only token proves the shell asked.

## Switch

Off unless `WIZARD_BROWSER_BOUNDARY=1` (environment of the `wam serve` process). Off means no change at all.
Do not turn it on alone: the shell must first send the token (wizard-ux) and the box must provision the token file.
`wam browser-boundary on|off` (below) does both.

## New boxes start with it on

Miguel, 23 Nov 2026 (#133, option A), after the automated hostile-app check on a real box (wizard-ux
`tools/boundary-box.sh`, CI job "Hostile app on a real box"): a new box starts with the boundary on, a box installed
before keeps it off for one more release (apps that keep data in the browser move it over first).

`wam bootstrap` decides it once, before it installs anything (`browser_boundary.settle_new_box`): when no choice is
recorded (`<wam home>/browser-boundary/state.json` is missing), no shell / Agent Service / pre-split bundle is
installed and nothing but Wizard's own defaults is, it records `on` (`"why": "new box"`) and mints the token, so the
shell and the Agent Service get their block as bootstrap installs them. `wam bootstrap` then restarts `wam serve`
(`systemctl --user try-restart wizard-app-manager-api`) so its own routes follow; install.sh reports a failed restart. A box that already runs Wizard, or whose owner chose, is left as it is. `WIZARD_BROWSER_BOUNDARY=0`
in the installer's environment (get.sh forwards it) keeps a new box off; the Mac guest build uses that, and its guest
then follows like a box installed before (next section). `wam browser-boundary off` is the way back on any box.

## Boxes that already ran Wizard follow one release later

A box installed before keeps the boundary off through the release that switched new boxes on, then follows. At the
end of every installer run (`wam bootstrap`, after the release moved the apps; `browser_boundary.existing_box_reason`)
it is switched on, like `wam browser-boundary on` (token, blocks, the shell and the Agent Service recreated, then
`wam serve` restarted), when all of these hold:

- the owner never chose (no `state.json`) and the installer was not given `WIZARD_BROWSER_BOUNDARY=0`;
- the split shell or Agent Service was installed before this run (a new box is decided above; a pre-split box that
  runs only the `wizard-shell` bundle stays off, because the bundle's shell never sends the token, while a split box
  that keeps the stopped bundle as rollback material follows);
- the shell and the Agent Service are on the release's own source commit now (an older shell sends no token; a box
  whose update did not move them is tried again on the next installer run);
- it is not a Mac guest whose Mac app does not send the token yet. try-wizard's VM is switched on only once
  `/opt/wizard/host-capabilities.json` says `"shell_token": true` (`browser_boundary.mac_host_sends_token`). Only the
  code-only guest update of a WizardOS for Mac release whose own owner calls (the update lease, dictation) carry the
  token installs that file (wizard-os#133), so the guest of an older Mac app stays as it is.

It records `{"on": true, "why": "existing box"}`, so it is decided once. An app's saved settings come along: the shell
copies each key an app wrote while it was same-origin into the app's own copy on its first sandboxed start (wizard-ux
`core/browser-boundary.js` storeCarry). `wam browser-boundary off` is the way back.

The box's own programs that call the owner API on its loopback send the token from the same file
(`browser_boundary.owner_headers`): `wam` agent access, feature switches and agent restarts, and `wizard contact`.
The boundary walls off app pages in the browser, not programs on the box (they are the owner on every box today,
wizard-os `spec/OWNER_PROOF.md`).

With it on:

- `WIZARD_SHELL_TOKEN_FILE` must hold 32 random bytes as lowercase hex, the same file the Agent Service reads.
  Missing or malformed: every gated request is refused (fails closed).
- Every Store/Settings listener route (`StoreHandler._go`, both the Serve listener and the gateway listener) and
  every host API route published beside the shell (`resources.PUBLISHED`: `/api/system/load`,
  `/api/system/devices`) also needs header `X-Wizard-Shell-Token: <token>`.
- Even with the token, a request is refused when `Origin: null` (sandboxed frame), `Sec-Fetch-Site` is not
  `same-origin`, or `Sec-Fetch-Dest` is a frame (`iframe`, `frame`, `embed`, `object`).
- Only the Connections view's own page files (GET `_connections/…` outside `api/`) skip it, because they load as a
  frame and hold no owner data. Its `api/` calls are gated.

## Only while every installed app opens inside it (WAM#1043)

On 25 Nov WizardOS 0.3.436 switched an existing box on by itself and TG style and Files opened blank: their page
loads a module script that never answers `Origin: null`. So the installer now asks the installed apps first
(`wam/boundary_apps.py`). For every app with a window (not the shell or the Agent Service) it loads the page on the
app's loopback port, with the host the app trusts (its `WIZARD_TRUSTED_HOSTS`) and the owner-proof signature the
owner edge would add, then asks for each of the page's own files twice: the way it loads today, and the way a
sandboxed window asks (`Sec-Fetch-Site: cross-site`, no Referer, `Origin: null` for module scripts, `modulepreload`
and `crossorigin` files, which then need `Access-Control-Allow-Origin: *` or `null`). An app **would open blank** when
a file that loads today would not load in the sandbox. An app this box-local request can't read at all (stopped, or a
page that refuses it) is listed as not checked and never holds the box. Only GETs; nothing is written.

- **A box that already ran Wizard** (above): when any app would open blank it is not switched on;
  `<wam home>/browser-boundary/held.json` names the apps, the installer output carries `browser_boundary.held`,
  `wam doctor` shows a `Browser boundary (sealed app windows)` row, and nothing is recorded as a choice, so the next
  installer run checks again.
- **A box the installer switched on by itself** (`"why": "new box"` this run, or `new box` / `existing box` from an
  earlier release that never checked): once the run's apps are in, the same check runs once (`"checked"` in
  `state.json`). When an app would open blank the boundary is switched back off (blocks removed, the shell and the
  Agent Service recreated, `wam serve` restarted), the choice is forgotten and the reason held as above; a box that
  was already on before the run also gets one Notifications line saying apps open the old way again.
- **Switched on:** the owner gets one Notifications line saying apps now open in a sealed window and that
  `wam browser-boundary off` is the way back (`"told": true` in `state.json`; retried on later runs until taken).
- **An app installed or updated on a box with the boundary on** is checked; one that would open blank is named in
  the install/update result (`browser_boundary.opens_blank`) and in one Notifications line with the way back. This
  never switches the boundary off by itself: an app must not be able to lower the wall by failing on purpose.
- **The owner's own `wam browser-boundary on|off`** is never undone; `on` lists any app that would open blank
  (`would_open_blank`). Either clears `held.json`.

Tests: `tests/test_browser_boundary_apps.py` (real HTTP servers standing in for TG style, Notes, Tinfoil-style and
login-only apps).

## Turning it on for a box: `wam browser-boundary on|off|status`

`wam browser-boundary on` mints the token once (32 random bytes as lowercase hex, `<wam home>/browser-boundary/shell-token`,
the folder 0700) and hands it read-only to the Agent Service and the shell (wizard-ux, or the pre-split wizard-shell)
as `/run/wizard-shell/token`, with `WIZARD_BROWSER_BOUNDARY=1` and `WIZARD_SHELL_TOKEN_FILE`, through a block of their
stock user-config override marked `x-wizard-browser-boundary` (only that block is ever rewritten or removed; alongside
mode rewrites keep it, and an app installed later gets it). It restarts those apps (`--no-restart` to skip). `wam serve`
picks the same file up when it next starts (an explicit `WIZARD_BROWSER_BOUNDARY` in its environment always wins).
`wam browser-boundary off` removes the blocks and the token. `--dry-run` and `--json` work as elsewhere.

The native Troubleshooter (`wam troubleshooter`) gets `WIZARD_SHELL_TOKEN_FILE=<wam home>/browser-boundary/shell-token`
in its `host.env` whether the box is on or off. It reads the file on every call and sends the token only when the file
holds one, for its one box-local Agent Service read (the repair page's sign-in recovery card), so `on` and `off` need
no Troubleshooter restart. It never sets `WIZARD_BROWSER_BOUNDARY` and never hands the token to its page.

The shell sends the token (wizard-ux `core/shell-token.js`). With the box off there is no token file and no block,
so nothing changes.

This is an extra gate: the owner login, tailscaled peer, origin, `X-Wizard-Store` and from-box checks still run first.

Tests: `tests/test_browser_boundary.py` lists every listener route and every published host route and asserts an
app-page request is refused with the switch on, the shell still gets through, and nothing changes with it off.

## App pages: `wam check` warns about a blank sandboxed window

With the boundary on, every app window is a sandboxed frame (an opaque origin). The browser then asks for a module
script (`<script type="module">`, which every Vite build writes) or any file marked `crossorigin` with `Origin: null`
and needs a CORS answer; without one the page stays blank. `wam check` warns (`browser boundary: page files load in a
sandboxed window`, `wam/builder.py` `boundary_rows`, never an error) when an app page loads such a file and none of the
app's own server code sends `Access-Control-Allow-Origin`. The fix is the one wizard-notes, wizard-music and the
Recorder's live view use: answer GET/HEAD of the app's own public files (scripts, styles, fonts, pictures; never the
page, the API or a write) with `Access-Control-Allow-Origin: *` and no credentials. `templates/hello-wizard` loads
classic `<script defer>` files and needs nothing (checked in real Chromium in a sandboxed frame: the habit list loads
and Add saves through the shell). Tests: `tests/test_builder_boundary.py`.
