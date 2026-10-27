# Build an app (candidate)

**Separated layout:** when `~/.wizard-os/client.json` exists, use only the thin client:
`wam builder check <source>`, `wam builder install-local <source> --stage-only --run --port 18799`,
`wam builder update-local <source> --run`. The source is yours; the service snapshots and builds protected
bytes, never your writable directory. No symlinks; 25 MiB total, 4096 files, 10 MiB/file. A host-access refusal
needs owner approval. Never follow the Docker or local builder-bundle commands below in this layout; do not
change terminal backend or copy a full CLI to bypass this boundary. Scaffolding/packaging, staged lifecycle
and view-customization verbs below are not provided by the separated transport; report that precisely.
The result is a loopback candidate, not a production registration or complete Wizard browser preview.

**Older full-access layout only:**

Source in `~/.wizard-os/dev/<id>/`; data in isolated app-data; owner files in media. `wizard.md` points at API docs, MCP, shipped skill, runtime and view source without requiring an upstream restructure. The reference template uses stdlib Python/SQLite and vanilla web source with a dependency-free validation build.

Run through `terminal`: `wam new <id> --request '<sentence>'`, adapt source, `wam check <source>` and `wam check <source> --proposal`, then `wam install-local <source> --stage-only --run`. All IDs, paths, runtime ports and source metadata must match. Build context is repository root, Dockerfile `app/Dockerfile`. After edits refresh the portable inline Runtipi payload with `wam package-inline <source>`; this uses the installed bundle's Python and assets, without a checkout or Python module setup.

The API and MCP share a service layer; view reads/writes only documented relative API routes. Render owner text using textContent; write errors leave saved state unchanged. Default direct app ports are loopback, the Wizard gateway owns production authorization. The template refuses any Host or browser Origin it was not configured for (`WIZARD_TRUSTED_HOSTS` / `WIZARD_TRUSTED_ORIGINS`; `install-local --trust-origin https://preview.example` adds one), which stops DNS-rebinding reach into an unauthenticated developer API. Installed through Runtipi, the installer or gateway must set the same two variables to the hosts/origins it proxies from (e.g. the owner's Tailscale name); until then the app answers loopback only. Expose idempotent write operations where practical and confirm each via readback. Local execution has no gateway auth and is single-owner developer proof only.

Read `system-functions.md` before editing the view. `wam check` always enforces the shared-function source gate: domain callbacks/routes are app code, root Back, capture, sharing, Appearance transport/storage, SW and installation logic are core-only. Load `/wizard-os-home/core/app-client.js` before app code; a standalone developer preview must route that exact shared resource through its gateway, not duplicate a client locally. A bare app port without shared resources is only an API proof, not a complete safe preview.

A fresh staged-app directory refuses overwrite. Stop/restart the exact developer project with `docker compose -p <project> -f <compose> stop/start`, using the `project` and `compose` values the install returned (each developer home gets its own project and data volume); remove only disposable candidate containers with `down`. Keep source/data when ending QA unless the named artifact is disposable.
