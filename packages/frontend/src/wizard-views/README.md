# Wizard App Manager views for Wizard UX

Three views, built from Wizard UX's own pieces so they can move into the shell with little change.
`wux.css` copies Wizard UX class names and values from `wizard-ux/web/styles.css` (`.search`, `.lgroup`, `.lgrid`,
`.lapp`, `.lrow`, `.st-group`, `.st-row`). Anything new is marked "added" there.

| View | Where it goes in Wizard UX | What it shows |
|---|---|---|
| `MyAppsView` | The launcher sheet (swipe up). It replaces the "Wizard apps" group | Installed apps as tiles. Each interface sits next to the agent or app it opens, joined by a short wire. Apps with no screen have a dashed ring. Status dot and update badge. Search at the bottom covers your apps first, then the App Store |
| `AppStoreView` | A second launcher page, opened from "Get apps" or from search | The same tiles and groups for everything in the Wizard App Index. Installed apps have a tick |
| `ResourcesView` | A group in the Settings sheet | Storage, processor and memory rows with a small meter, plus Start / Stop / Restart all |

Groups are Agents, Apps and System. Interfaces don't get their own group: they appear inside the pair with the thing they open.

## Data

- **Installed apps, status, updates:** App Manager API `GET /api/apps/installed`, `GET /api/system/load`, and lifecycle `POST /api/app-lifecycle/:urn/(start|stop|restart)`.
- **Names, logos, type, `uses`:** the public index `GET https://apps.aiwizards.com/v1/search` (`?q=` or `?id=`). Pairing comes from `uses` in `wizard.md`. Screen versus headless is `type != service`.

In the shell these calls would go through the Agent Service (`/wizard/...`), per `spec/SHELL.md`.
