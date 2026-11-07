# Build an app (candidate)

Source in `~/.wizard-os/dev/<id>/`; data in isolated app-data; owner files in media. `wizard.md` points at API docs, MCP, shipped skill, runtime and view source without requiring an upstream restructure. The reference template uses stdlib Python/SQLite and vanilla web source with a dependency-free validation build.

Run through `terminal`: `wam new <id> [--kind list] --request '<sentence>'`, adapt source, `wam package-inline <source>`, `wam verify <source>`, then install (`wam install-local <source> --run` for a local proof). All IDs, paths, runtime ports and source metadata must match. Build context is repository root, Dockerfile `app/Dockerfile`. After edits refresh the portable inline Runtipi payload with `wam package-inline <source>`; this uses the installed bundle's Python and assets, without a checkout or Python module setup.

The API and MCP share a service layer; view reads/writes only documented relative API routes. Render owner text using textContent; write errors leave saved state unchanged. Default direct app ports are loopback, the Wizard gateway owns production authorization. The template refuses any Host or browser Origin it was not configured for (`WIZARD_TRUSTED_HOSTS` / `WIZARD_TRUSTED_ORIGINS`; `install-local --trust-origin https://preview.example` adds one), which stops DNS-rebinding reach into an unauthenticated developer API. Installed through Runtipi, the installer or gateway must set the same two variables to the hosts/origins it proxies from (e.g. the owner's Tailscale name); until then the app answers loopback only. Expose idempotent write operations where practical and confirm each via readback. Local execution has no gateway auth and is single-owner developer proof only.

Read `system-functions.md` before editing the view. `wam check` always enforces the shared-function source gate: domain callbacks/routes are app code, root Back, capture, sharing, Appearance transport/storage, SW and installation logic are core-only. Load `/wizard-os-home/core/app-client.js` before app code; a standalone developer preview must route that exact shared resource through its gateway, not duplicate a client locally. A bare app port without shared resources is only an API proof, not a complete safe preview.

A fresh staged-app directory refuses overwrite. Stop/restart the exact developer project with `docker compose -p <project> -f <compose> stop/start`, using the `project` and `compose` values the install returned (each developer home gets its own project and data volume); remove only disposable candidate containers with `down`. Keep source/data when ending QA unless the named artifact is disposable.

## `wam verify` round trip

`--kind list` apps get the round trip built in (create, idempotent replay, list, update, restart, read back). Other
apps declare it in `verify.json` at the source root; the hello template ships one:

```json
{"round_trip": [
  {"do": "POST /api/habits", "body": {"name": "Verify {{run}}"}, "status": 201, "save": {"hid": "habits.-1.id"}},
  {"do": "GET /api/habits", "expect": {"habits.-1.name": "Verify {{run}}"}},
  {"do": "POST /api/habits/{{hid}}/today", "body": {"done": true}},
  {"restart": true},
  {"do": "GET /api/habits", "expect": {"habits.-1.done": true}}
]}
```

`{{run}}` is unique per run, `{{uuid:name}}` a fresh UUID reused by name, `save` stores a response value by dotted path
(negative list indexes count from the end). A restart step is required. Screenshots need a headless Chromium
(`WAM_CHROMIUM=<path>` or one on PATH / in the Playwright cache); without one they are skipped with a note.
