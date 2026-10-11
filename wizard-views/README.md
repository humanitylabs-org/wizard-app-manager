# Wizard App Manager views for Wizard UX

For James: three small views built in your own launcher and Settings markup. They are vanilla ES modules with no build step.

| File | What |
|---|---|
| `wizard-apps.js` | `renderMyApps(el, ctx)`, `renderStore(el, ctx)`, `renderResources(el, ctx)`, `renderCard(el, app, ctx)` |
| `wizard-apps.css` | Only the added pieces (`.wam-*`): status dot, update badge, dashed ring for apps with no screen, pair wire, chip row, card, meters. Everything else uses your `.lgroup .lgrid .lapp .lrow .st-group .st-row` |
| `overlay.js` | Box-local wiring into an unmodified Wizard UX (spec/SHELL.md layer 4), described below |
| `index.html`, `standalone.*` | The same views full screen for desktop/web without the shell, with a small Admin menu. `wux.css` holds your classes scoped under `.wux`, for this page only |

## Where they go

- **My Apps is the swipe-up launcher.** With an empty query, your "Wizard apps" group becomes Agents / Apps / System, and your other groups stay. An interface sits next to the app it `uses`, joined by a short wire. Apps with no screen get a dashed ring. Tapping a tile opens a short card with only the buttons that apply (Open, Update, Start/Stop). An app with no screen says "No screen: ask your agent to use it".
- **The App Store is the same sheet.** "Get apps" swaps the sheet body. One chip row goes above `#searchForm` (All, the top tags, then Popular / New / A–Z), and `#search` searches the index until the sheet closes. Install runs the Runtipi install (the app's `uses` first), then `POST /v1/installs`.
- **Resources is a Settings group** after "Wizard": Storage, Processor and Memory with small meters, then Start all / Stop all / Restart all. Stop all needs a second tap.

To merge natively, call the render functions from `renderLauncher` and `renderSettings` instead of loading `overlay.js`.

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
