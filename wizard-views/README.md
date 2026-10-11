# Wizard App Manager views for Wizard UX

For James: four small pieces built in your own launcher, home and Settings markup. They are vanilla ES modules with no build step.

| File | What |
|---|---|
| `wizard-apps.js` | `renderMyApps(el, ctx)`, `renderStore(el, ctx)`, `renderResources(el, ctx)`, `renderResourceCard(el, ctx)`, `renderCard(el, app, ctx)` |
| `wizard-graph.js` | The graph layout, pure (no DOM, tested in `tests/wizard-graph.test.mjs`): which tile sits in which cell, what folds into "+N" |
| `wizard-apps.css` | Only the added pieces (`.wam-*`): graph lines, anchor ring, dimming, "+N", status dot, badges, chip row, card, meters, home card. Everything else uses your `.lgroup .lgrid .lapp .st-group .st-row .now-card` |
| `overlay.js` | Box-local wiring into an unmodified Wizard UX (spec/SHELL.md layer 4), described below |
| `index.html`, `standalone.*` | The same views full screen for desktop/web without the shell, with a small Admin menu. `wux.css` holds your classes scoped under `.wux`, for this page and for the desktop palette |

## Where they go

- **My Apps is the swipe-up launcher, drawn as a graph.** With an empty query, your "Views" and "Wizard apps" groups become four steady rows: Views, Agents, Apps, System (System = the apps filling a parking spot: agent, files, transcription...). Your own tiles (Inbox, Chats) stay in those rows and still open as before. On the phone the Views row is at the bottom, next to the thumb; on the desktop it is at the top, next to the input.
  - One tile is the **anchor** (violet ring and glow, the only selection look; the first view by default). What it connects to (what it `uses`/`needs`, what uses it) comes first in its row, joined to the anchor by thin lines from tile edge to tile edge. Lines share a short trunk, run along the empty band between rows and down the gaps between columns, never through a tile; a row title that would sit on a line slides aside. Everything else dims (icon ~35%, name still readable) but stays tappable. If the anchor fills a parking spot, its row title says which ("Agent spot").
  - **Tap a tile** to make it the anchor. The anchor shows a pill: **Open** (it has a screen and is running: opens it like the card's Open) or **Details** (opens the card: Open, Update, Start/Stop; "No screen: ask your agent to use it"). Tapping the anchor anywhere else also opens the card.
  - Rows are left-aligned with no gaps and never shrink or scroll sideways: when a row has more than fits, the rest folds into a "+N" tile at its end that opens the row in place.
  - Marks, one meaning each: small dot = running (red/amber when not), quiet ↑ = update available, faint solid ring = no screen. Long names use a short form on the tile ("Hermes Desktop"; `short_name` in apps.json wins); the card shows the full name.
- **The App Store is the same sheet and the same graph** over the index. "Get apps" swaps the sheet body; one chip row goes above `#searchForm` (All and the top tags scrolling sideways, then one sort button that cycles Popular / New / A–Z), and `#search` searches the index until the sheet closes. Tap an app to see what it needs: requirements already installed get ✓, missing ones get an amber + and their own amber dashed branch ("Adds 1" in its row title). Tap Details for the card; Install runs the Runtipi install (the app's `uses` first), then `POST /v1/installs`.
- **Works with vs allowed to use.** The lines and the card's "Works with" list are compatibility (declared `uses`/`needs`/`provides`). Permission is a different thing and isn't built yet: the card keeps a dashed "Allowed to use" slot for it.
- **Resources is a Settings group** after "Wizard": Storage, Processor and Memory with small meters in your home cards' colours (mint, violet, pink; amber from 80%). Once it has a few readings, Processor also shows a small line of its last minutes beside its number. Then Start all / Stop all / Restart all. Stop all needs a second tap.
- **Home card**: under your Unread / Working / Yesterday cards, one card with the same three meters, refreshed every 10 s while it is visible; a calm amber tint when anything is at 80% or more. Tapping it opens Settings at Resources.
- **Desktop shell**: the ⌘K palette shows the My Apps graph above your list while the input is empty; "Get apps" turns it into the store (Esc goes back). Your Settings card gets the Resources group.

To merge natively, call the render functions from `renderLauncher`, `renderHome` and `renderSettings` instead of loading `overlay.js`.

## Data (same origin)

- **Runtipi:**
  - `GET /api/apps/installed` and `GET /api/system/load`
  - `POST /api/app-lifecycle/{urn}/install|start|stop`, `PATCH .../update`, and `POST /api/app-lifecycle/start-all|stop-all|restart-all`
  - Logos at `/api/marketplace/apps/{urn}/image`. The urn is `<id>:wizardapps`.
- **Wizard OS front:**
  - `GET /wizard/v1/wizard-apps` serves apps.json (type, category, uses, provides, view).
  - `/wizard/v1/index/*` proxies the public index `https://apps.aiwizards.com/v1`, so your CSP's `connect-src 'self'` still holds.
  - `/wizard/v1/apps` lists only installed apps whose screen the browser can reach.

`ctx` options are listed at the top of `wizard-apps.js` (`api`, `wizard` and `index` change the base paths).

## App opening and routing

Keep stock Runtipi's Open actions and Traefik routing as the source of truth for app
access. Wizard cards should reuse the configured Runtipi endpoint/action, not
introduce per-app Tailscale Serve routes or a bespoke subpath proxy. Runtipi supports
the current hostname plus app port, an app domain, and a local domain. Those endpoints
still need a working private network/domain configuration; a URL alone is not proof
of remote reachability.

`runtipi-open.js` reads the live app's explicit domain or local subdomain plus
`GET /api/user-context` (`localDomain`, `sslPort`), preserving `url_suffix`.
Stopped, headless, or restart-pending apps do not get an Open action. External
domain screens open in a new top-level tab, as in stock Runtipi; this lets private
access sign-in work without a cross-origin iframe.

New installs appear automatically from Runtipi's installed-app API. Runtipi generates
Traefik routes when its normal domain settings are enabled. This adapter does not
provision DNS, Cloudflare Access policies, tunnel entries, or Tailscale routing.
Those provider prerequisites must already be configured; two manually configured
app hosts do not make all future apps remotely reachable automatically.

Verification: `node --test tests/runtipi-open.test.mjs`. Live pilot checks exercised
an existing Hermes screen and a fresh Files install through stock Runtipi TLS;
the real launcher Open button reached Cloudflare sign-in. An owner-authenticated
public session is a separate check, not implied by local browser/origin tests.
