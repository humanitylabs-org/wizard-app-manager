# Build an app (candidate)

Source in `~/.wizard-os/dev/<id>/`; data in isolated app-data; owner files in media. `wizard.md` points at API docs, MCP, shipped skill, runtime and view source without requiring an upstream restructure. The reference template uses stdlib Python/SQLite and vanilla web source with a dependency-free validation build.

Run through `terminal`: `wam new <id> --request '<sentence>'`, adapt source, `wam check <source>` and `wam check <source> --proposal`, then `wam install-local <source> --stage-only --run`. All IDs, paths, runtime ports and source metadata must match. Build context is repository root, Dockerfile `app/Dockerfile`. After edits refresh portable inline Runtipi payload with `python3 -c 'from wam.builder_assets import package_inline; from pathlib import Path; package_inline(Path("<source>"))'`.

The API and MCP share a service layer; view reads/writes only documented relative API routes. Render owner text using textContent; write errors leave saved state unchanged. Default direct app ports are loopback, the Wizard gateway owns production authorization. Expose idempotent write operations where practical and confirm each via readback. Local execution has no gateway auth and is single-owner developer proof only.

A fresh staged-app directory refuses overwrite. Stop/restart the exact developer project with `docker compose -p wizard-dev-<id> -f <returned-compose> stop/start`; remove only disposable candidate containers with `down`. Keep source/data when ending QA unless the named artifact is disposable.
