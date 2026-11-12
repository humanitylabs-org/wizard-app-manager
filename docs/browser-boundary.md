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

This is an extra gate: the owner login, tailscaled peer, origin, `X-Wizard-Store` and from-box checks still run first.

Tests: `tests/test_browser_boundary.py` lists every listener route and every published host route and asserts an
app-page request is refused with the switch on, the shell still gets through, and nothing changes with it off.
