# Browser owner boundary (App Manager side)

Issue: humanitylabs-org/wizard-os#133. Agent Service side: wizard-agent-service `server/browser-boundary.mjs`.

## Why

App screens load on the same origin as the shell. A script on an app page therefore sends the owner's
Tailscale login, the shell's `Origin` and `Sec-Fetch-Site: same-origin`, exactly like the shell. Those prove
the device, not that the shell asked. The shell-only token proves the shell asked.

## Switch

Off unless `WIZARD_BROWSER_BOUNDARY=1` (environment of the `wam serve` process). Off means no change at all.
Do not turn it on alone: the shell must first send the token (wizard-ux) and the box must provision the token file.
Turning it on for anyone is Miguel's call after the hostile-app browser test (release stop on #133).

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

Never run `on` before the shell sends the token (wizard-ux, next slice): every Store, Settings and chat call from the
shell would be refused too. With the box off (the default) there is no token file and no block, so nothing changes.

This is an extra gate: the owner login, tailscaled peer, origin, `X-Wizard-Store` and from-box checks still run first.

Tests: `tests/test_browser_boundary.py` lists every listener route and every published host route and asserts an
app-page request is refused with the switch on, the shell still gets through, and nothing changes with it off.
