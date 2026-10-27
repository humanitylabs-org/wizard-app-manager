# Host lifecycle bridge

Staged integration entry point (no Hermes core or old WAM server registration changes):

```sh
WIZARD_OS_HOME=/path/to/isolated/wizard-home RUNTIPI_ROOT=/path/to/runtipi \
  python3 -m wam.lifecycle serve --bind 127.0.0.1 --port 7791
```

Only an IPv4 loopback bind is permitted, except the one declared fresh-mode container listener (`--container`, see "Fresh mode"). Use a service supervisor outside this module. Existing WAM/YAML dependencies are unchanged; HTTP server, locking, journal, SSE client and authentication use Python's standard library.

## Authentication and proxy boundary

The bridge uses `WIZARD_APP_MANAGER_TOKEN` if set, otherwise creates `$WIZARD_OS_HOME/lifecycle-token` atomically with permissions 0600. `--token-file PATH` explicitly selects an owner-only file instead. The dedicated token must have 32–256 header-safe characters from `[A-Za-z0-9._~-]`. Provision the same value into the Agent Service's **server-side** `WIZARD_APP_MANAGER_TOKEN` and configure its bridge origin as `http://127.0.0.1:7791` (no `/api` suffix; a containerised fresh-mode Service uses `http://wizard-app-lifecycle:7791`, generated below). Do not put it in UX settings, bundles, browser requests, app manifests, or public registry files.

Every request, including reads, images and `/healthz`, requires `Authorization: Bearer <dedicated token>`. Any `Origin` header is rejected: the browser uses the existing same-origin session/CSRF-protected service routes, which reconstruct the allowlisted host request without forwarding browser Origin, cookies, auth or arbitrary headers. No CORS. The internal upstream-resolution endpoint below is **not** a browser proxy route.

Runtipi authentication uses the existing `wam.runtipi.cli_token` HS256 `sub=cli` mechanism and inherited installed/app client helpers through an isolated no-redirect subclass, reading `JWT_SECRET` from the installed `.env`, not a harness-only dashboard login. The bridge's production Runtipi origin is `http://127.0.0.1:<NGINX_PORT>/api`. The existing Runtipi must already have an operator and the Wizard store configured; this module does not register users or modify host Runtipi config. Secrets, raw upstream errors and JWTs are not returned or logged.

## Read routes

- `GET /healthz`: `{ok: true}` (bridge liveness, not a Runtipi readiness assertion).
- `GET /api/apps`: the public registry (`schema, updated_at, store, apps, slots`, and existing `external, parking` projections when configured).
- `GET /api/apps/installed`: `{installed: [...], registry: <public registry>}`. Installed entries retain the existing Runtipi shape `{app, info, metadata}` plus `wizard: <registry entry>`. Only Wizard-store apps with valid `wizard.md` are included; system-owned skills are not installed-app rows. `app` is explicitly limited to `id, appName, appStoreSlug, status, port, version, pendingRestart, createdAt, updatedAt, isVisibleOnGuestDashboard` if present. `info` retains public app/catalog details and form descriptions but not deployment form defaults/values. `metadata` is sanitized recursively. No `app.config`, env contents, credentials, JWTs or copy/upstream internals.
- `GET /api/apps/<id[:wizardapps]>`: catalog/installed detail `{app, info, metadata}` plus `wizard` when installed/projected.
- `GET /api/slots`: `{spots: [...]}` using existing WAM slot rules.
- `GET /api/system/load`, `/api/marketplace/all`, `/api/marketplace/enabled`: sanitized existing response shapes.
- `GET /api/marketplace/apps/search`: existing sanitized response; query allowlist `search, pageSize, cursor, category, storeId`; store always constrained to `wizardapps`.
- `GET /api/marketplace/apps/<id[:wizardapps]>/image`: authenticated server-side image fetch; raster MIME allowlist, 4MiB limit, redirects refused, no forwarded upstream headers.
- **Server-only** `GET /api/internal/apps/<id>/view`: `{upstream: "http://<service_app_host>:<app-port>/<manifest-path>", path: "/apps/<id>/"}`. Agent Service consumes this to implement authenticated same-origin `/apps/<id>/` proxying. Do not expose this endpoint verbatim to browsers. Resolve only the app id, never an arbitrary client-supplied origin/path. WebSocket/non-HTTP app transports need a separate proxy decision.

View projection uses `view.url: /apps/<id>/`; `_view_upstream` remains internal while building and is persisted separately in owner-only `view-upstreams.json`. Existing `api`, `view`, `source`, `backend`, `replaces_view` are retained as sanitized **data**, not newly proposed routing/tool semantics. Credential-bearing keys and URL userinfo/query/fragment are omitted, form defaults/values removed, JWT/Bearer strings and known server-secret values redacted. Path metadata cannot choose an origin, traversal, query credentials or percent-encoded escapes.

## Declared service app network

Alongside defaults to `127.0.0.1`: the Agent Service is on the same host. A fresh/container Service can declare its reachable host gateway in `$WIZARD_OS_HOME/app-manager.json`:

```json
{"service_app_host": "host.docker.internal"}
```

An IPv4 address or DNS hostname is accepted verbatim; URL schemes, ports, paths, userinfo, whitespace, empty labels and invalid IPv4 are rejected. This setting maps registry `mcp.url`, `health`, and the private view upstream to that Service-reachable host, leaving browser `view.url` same-origin and Runtipi/JWT control traffic on host loopback. `mcp.internal` keeps existing app-network DNS metadata. This is declared addressing, **not connectivity proof**: app ports are published on host loopback only, so `host.docker.internal` does not reach them from a container (real-box run B3). **Fresh mode does not use it**; it uses the app network instead:

```json
{"service_app_network": "internal"}
```

`internal` (written by `wam fresh-agent prepare`) makes the private view upstream `http://<app id>:<view.port or app port><path>`, the app's Runtipi network name, which the Agent Service container on that network resolves (B3 proved `mcp.internal` the same way). `mcp.url`/`health` stay on `service_app_host` for host-side tools; the Service recipes already set `WIZARD_APPS_MCP_ADDRESS: internal`. Both settings are re-read at every refresh, so a running bridge follows them without a restart.

## Pointer skills (`wizard-job-*`) are rewritten with every result

Every `registry.write` (each bridge refresh, so every lifecycle result including failures and replays, and every `wam` CLI registry write) also renders the `wizard-job-<job>` pointer skills into that root's `skills/` and refreshes the `.wizard-jobs` stamp. The renderer is the wizard-os plugin's own `kernel.py` (`pointer_skills(bridge_apps(...))`), vendored byte-identical at a pinned wizard-agent-service commit in `wam/vendor/` (`SOURCE.json` holds repo, commit and sha256; `tests/test_fresh_agent.py` fails on drift). It is the same text the Agent Service and the plugin write, so an install appears and an uninstall disappears in the agent's skills folder immediately, without a Hermes session start, an Agent Service write or a restart:

- `$WIZARD_OS_HOME/skills` (what an alongside host Hermes reads through `skills.external_dirs`) with `mcp.url` addresses;
- the shared `app-data/wizard-os/skills` (what a fresh container Hermes reads) with `mcp.internal` addresses.

Only folders that are generated pointers are replaced or removed: a real directory holding only `SKILL.md` (and the renderers' `SKILL.md.tmp`) whose text carries the generated marker. App skills, `system-skills/wizard-builder`, owner skills (even one named `wizard-job-*` without the marker), symlinks and `wizard link`/`unlink` state are left alone. The Agent Service's richer pointers (with tool names) still land in its own `/state` folder; the plugin's session-start refresh sees a current stamp and skips.

## Fresh mode: the container Hermes and the containerised Agent Service

`wam fresh-agent prepare|connect|status|disconnect` (`wam/fresh_agent.py`) is the installation helper for a box without its own Hermes. `install.sh` runs `prepare` before `wam bootstrap` (so the apps start already wired) and `connect` after it. It writes only generated stock Runtipi user-config overlays and Hermes' own config; no recipe, image, Hermes core or host Hermes change. It refuses to run in alongside mode.

**`prepare`** creates `mcp-token` and `lifecycle-token` (0600) if missing, reads the Runtipi Docker network's gateway address (`docker network inspect runtipi_tipi_main_network`, private IPv4 only) and records in `app-manager.json`:

```json
{"container_bridge_bind": "172.18.0.1", "service_app_network": "internal"}
```

plus `$WIZARD_OS_HOME/fresh-agent.json` (0600). `wizard-app-lifecycle-containers.service` then runs a second bridge process, `python3 -m wam.lifecycle serve --container --port 7791`, bound to that address only. It is the same bridge (same home, token, journal and `flock` serialization as the loopback one). `Server` accepts exactly that declared private, non-loopback IPv4 and nothing else. The address is reachable from containers on Docker networks of this box, not from the public interface. Every request still needs the dedicated bearer token, and `Origin` is still refused. A host firewall that drops Docker-to-host traffic (for example UFW with default deny incoming) must allow `7791/tcp` from that network.

**Owner.** The Service trusts the Runtipi gateway peer (Tailscale Serve to the Service's `127.0.0.1`-published port arrives from it) only for a named `Tailscale-User-Login`. `prepare` takes the owner from `$WIZARD_APP_OWNER_LOGINS` (and `$WIZARD_ALLOWED_HOSTS`), else from this box's own `tailscale status` (Self's login and DNS name), records it in `fresh-agent.json` and refuses (`owner_login_required`) when there is none; it never trusts every tailnet user. The Service overlay carries `WIZARD_APP_OWNER_LOGINS` and `WIZARD_ALLOWED_HOSTS`. Packaging gate: the Service must be published on `127.0.0.1` only (a wildcard publication lets an app container hairpin through the gateway and forge the header); `status` reports it as a failing check. `disconnect` also disables `wizard-app-lifecycle-containers.service`, which has nothing to bind any more.

**Generated overlays** (`<runtipi>/user-config/wizardapps/<app>/`, header `# Written by wam (fresh mode)`; a compose file wam did not write is never replaced; other `app.env` keys such as image pins are kept):

| app | `docker-compose.yml` | `app.env` (0600) |
|---|---|---|
| `wizard-agent-service` and/or legacy `wizard-shell` | `extra_hosts: wizard-app-lifecycle:<gateway ip>`; env `WIZARD_MCP_TOKEN`, `WIZARD_APP_MANAGER_URL: http://wizard-app-lifecycle:7791`, `WIZARD_APP_MANAGER_TOKEN` | `WIZARD_MCP_TOKEN`, `WIZARD_APP_MANAGER_TOKEN` |
| `hermes-agent` (service `hermes-agent-engine`) | volume `${ROOT_FOLDER_HOST}/app-data/wizard-os:/wizard-os:ro`; env `WIZARD_OS_HOME: /wizard-os`, `WIZARD_APPS_MCP_ADDRESS: internal` | none |
| `wizard-ux` (the modular shell, beside `wizard-agent-service`) | `network_mode: host`, `networks`/`ports` reset; env `WIZARD_SHELL_LISTEN: 127.0.0.1:<its APP_PORT>`, `WIZARD_AGENT_SERVICE_ORIGIN: http://127.0.0.1:<the Service's APP_PORT>`; entrypoint = the generated `shell-adapter.sh` (mounted read-only from this folder) | none (no token) |

**Split shell (`wizard-ux` + `wizard-agent-service`).** The recipe's shell would sit on the Runtipi network and proxy to the Service from its own container address, which the Service never trusts (403), and any app container could reach that proxy and hand it a forged `Tailscale-User-Login`. The fresh overlay instead runs the shell the way the packaging owner's alongside mode does: on the host network, listening on `127.0.0.1` only (Tailscale Serve points there; no container can connect), reaching the Service's `127.0.0.1`-published port, so the Service sees its Runtipi gateway peer, which it trusts only with the named owner login. A stable peer: shell or Service restarts change nothing. `shell-adapter.sh` runs the recipe's own entrypoint on copies of the image's own nginx files with three checked changes: `allow 127.0.0.1; deny all;` (refuses to start unless the listen address is `127.0.0.1:<port>`), `/wizard-os-home/store-data/` (GET/HEAD, the Store's data) to the Service before static files, and `frame-ancestors 'self'`/`X-Frame-Options SAMEORIGIN` so the shell can frame its own Store page. All other routes (API, voice, streaming, health, the box-only gateway rule) stay the recipe's; if the recipe's anchors change, the shell refuses to start rather than run unadapted. `connect` writes it for an installed shell (restarting it), `status` checks it, `disconnect` removes overlay and adapter.

The Service stays on the Runtipi network (not host networking), so `wizard-ux` keeps reaching `http://wizard-agent-service:8787`, the container Hermes reaches the gateway by DNS, and app MCP/view upstreams resolve by app name. Overlays are written at install (by `wam install`/`bootstrap` and the bridge's install action) once `prepare` ran, and by `connect` for apps already installed.

**`connect`** picks the gateway: `http://wizard-agent-service:8787/wizard/mcp` when the modular Service is installed, else `http://wizard-shell:8787/wizard/mcp` (legacy bundle). It never installs a second agent or Service. Inside the running `hermes-agent-engine` container (`docker exec` as its `HERMES_UID:HERMES_GID`, `HERMES_HOME=/opt/data`, Hermes' own interpreter found via `hermes --print-runtime-command` or `/opt/hermes/.venv/bin/python`) it:

1. refuses if any MCP server other than `wizard` already points at a `/wizard/mcp` gateway, or if `wizard` names something else (exactly one gateway);
2. backs up `config.yaml` and `.env` to `/opt/data/backups/wizard-os/<UTC stamp>/` (0700) before the first change;
3. installs the plugin with stock `hermes plugins install humanitylabs-org/wizard-agent-service#hermes-plugin/wizard-os --ref <pinned commit> --no-enable --no-deps`, checks the installed files' sha256 against `wam/vendor/SOURCE.json` (on mismatch it stops before enabling), then runs `hermes plugins enable wizard-os`. Only a Hermes without that subcommand falls back to the `plugins.enabled` allow-list edit `wizard link` makes. `WIZARD_PLUGIN_SOURCE`/`WIZARD_PLUGIN_REF` override the source and ref;
4. sets `mcp_servers.wizard = {url: <gateway>, headers: {Authorization: "Bearer ${WIZARD_MCP_TOKEN}"}}` and appends `/wizard-os/skills` to `skills.external_dirs`. These are minimal-diff, atomic edits (the `wizard link` splice: unchanged lines keep their bytes). An empty `mcp_servers: []` becomes a map;
5. writes `WIZARD_MCP_TOKEN` into `/opt/data/.env` (0600). The token reaches the container over stdin only, never argv or output.

The edits' previous values are recorded in `fresh-agent.json`. Then a stock Runtipi restart (`POST /app-lifecycle/<urn>/restart`) runs, only for apps whose overlay or config changed. A rerun with nothing to change makes no edit and no restart. `connect` ends with `status`.

**`status`** is read-only. It checks: exactly one Wizard gateway with the expected URL, `/wizard-os/skills` linked and mounted (lists the visible `wizard-job-*` pointers), the pinned plugin enabled, the token present in the Hermes `.env` (value not shown), the Service overlay, the declared bridge listener, and one live `initialize` + `tools/list` through the gateway **from inside the hermes-agent container**, using the token Hermes uses. It does not prove a model turn or the Service→bridge route; `opus-qa/fresh-agent-qa.sh` probes the latter from the Service container.

**`disconnect`** reverses the recorded edits (removing the token and the plugin only if `connect` added them; it backs up first), removes the generated overlays and the `app-manager.json` keys, deletes `fresh-agent.json`, and restarts the affected apps.

**After `connect`, app lifecycle needs no agent restart**: the bridge rewrites the shared `apps.json`, app skills and pointers (bind-mounted read-only into the container Hermes), the plugin's kernel reads `/wizard-os/apps.json`, and the Agent Service gateway sends `tools/list_changed`. Owner identity for a containerised Service (real-box gate 6e: only loopback peers or verified tailnet/Access principals pass owner routes) is the Service's decision and is not changed here.

## Mutations (synchronous terminal response)

Only these stock routes/methods exist:

| Method | Route | Body |
|---|---|---|
| POST | `/api/app-lifecycle/<id[:wizardapps]>/install` | flat known app form-field variables only; exposure flags/host ports are not accepted from client |
| POST | `/api/app-lifecycle/<id[:wizardapps]>/start` | `{}` |
| POST | `/api/app-lifecycle/<id[:wizardapps]>/stop` | `{}` |
| POST | `/api/app-lifecycle/<id[:wizardapps]>/restart` | `{}` |
| DELETE | `/api/app-lifecycle/<id[:wizardapps]>/uninstall` | `{removeBackups: false}` (optional, explicit boolean) |
| PATCH | `/api/app-lifecycle/<id[:wizardapps]>/update` | `{performBackup: true}` (optional, explicit boolean) |

Required `Idempotency-Key`: 16–128 characters from `[A-Za-z0-9_-]`; same logical click/retry must reuse the same key **and payload**. The service proxy should generate/preserve one key, not mint a fresh key after timeout. Successful response: `{success: true, noop: boolean, status: running|stopped|missing, requestId?: <stock UUID>, urn, action, replayed: boolean, registry}`. A returned request id alone is never success: subscription to stock `/sse/app` begins before submitting, requires the matching app/action success event (error events fail even if status rolled back to running), then verifies installed status. No-op installs/start/stop/uninstall verify authoritative state and refresh projections without resubmission.

Terminal failure response includes `{success: false, error: <bounded symbolic code>, urn, action, replayed, registry}`. Validation/auth/refresh errors before a complete envelope return only `{success: false, error}`. Codes include `unauthorized`, `invalid_app`, `invalid_path`, `invalid_body`, `invalid_json`, `invalid_query`, `invalid_form`, `unexpected_fields`, `store_unavailable`, `registry_not_ready`, `required_fields_missing`, `idempotency_key_required`, `idempotency_conflict`, `operation_journal_invalid`, `operation_unconfirmed`, `dependencies_required`, `required_slot`, `app_busy`, `app_not_installed`, `app_not_found`, `view_not_found`, `not_found`, `method_not_allowed`, `body_too_large`, `lifecycle_failed`, `completion_stream_unavailable`, `invalid_completion_event`, `completion_unconfirmed`, `untrackable_operation`, `terminal_status_unconfirmed`, `projection_refresh_failed`, `upstream_rejected`, `upstream_unavailable`, `unsupported_image`, `image_too_large`. No arbitrary exception message or upstream body is exposed.

Thread + `fcntl.flock` serialization covers mutations, polling, refresh and durable journal decisions, including multiple bridge processes sharing one home. Catalog/system passthrough reads remain available during mutations; projection reads attempt a nonblocking lock and return the last atomic public snapshot with `snapshot: true` while busy (or symbolic `registry_not_ready` if none exists). Snapshot installed rows have the same `{app, info, metadata, wizard}` envelope, but `app` only supplies `status`, `info` supplies `id, urn, name`, and `metadata` is `{}`; the full registry entry remains in `wizard`. Snapshots are not terminal mutation evidence. A key is reserved atomically before upstream calls, then a durable submission-phase marker is written immediately before the lifecycle request. Pre-submission failures are retryable with a new key after recovery and do not quarantine an app as potentially mutated. Journal request fingerprints are keyed HMACs, never raw submitted credentials; generated required passwords are atomically saved 0600 and reused. Completed duplicate/failure deliveries replay their recorded outcome, but still refresh from Runtipi. All operation outcomes including failures and conflicting retries refresh **both** local and shared `apps.json` and app skills. A refresh failure returns 502, never stale success. Required-slot uninstall rules are preserved. Installs with missing dependencies fail `dependencies_required`; the integration owner installs dependencies individually (no hidden unjournaled recursive installs or Hermes/per-app MCP restarts).

### Deliberate crash/timeout policy

Missing SSE receipt, lost upstream response or a process crash after journal reservation is **uncertain**, never success and never blindly reissued. Both the same key and new mutations for that app return `operation_unconfirmed`; other app operations are independent. Operator reconciliation is required before clearing that specific journal reservation. A genuinely completed `lifecycle_failed` receipt may be retried with a new key after inspecting refreshed app state. Keep the journal across restarts; no TTL expiry silently repeats an uninstall/update/restart. Received definitive upstream 4xx rejections (excluding 408/429) or connection-refused non-delivery become symbolic `upstream_rejected`, not uncertain mutation; a new key may retry after correcting the request or auth. Redirects and proxy environment routing are disabled for every CLI-JWT request. A rejection of a verification/read GET **after** lifecycle acceptance is not proof that the mutation was rejected: it remains uncertain. The stock SSE app events have no request id, so the bridge serializes its own submissions and assumes other administrators do not operate the same app concurrently through the Runtipi dashboard. This assumption is a real integration gate, not a claimed cross-dashboard exactly-once guarantee.

## Files and scope

`registry.write` updates individual files atomically using fsync + rename; write-ahead skill ownership records accept old/new generated digests across interrupted writes, and protect non-app/system directories, user-modified skills and `system-skills/wizard-builder`. Obsolete **unmodified generated** app SKILL.md files alone are removed. Registry and skill trees are not a multi-file transactional filesystem snapshot. Malformed manifest paths/YAML/capabilities/non-JSON values or source skill symlinks quarantine only that installed app: it remains listed with `metadata_error: metadata_invalid`, null `mcp/view/health/skill` and empty capabilities, so uninstall remains possible and other apps still refresh. No secret-bearing raw metadata exception is exposed. The shared registry mount must remain read-only to app containers; symlinked registry/skills roots are rejected and non-dict JSON shapes cannot poison refresh. Host-private slot choices are authoritative for the shared copy. Confirm the actual mount mode in packaging acceptance; this module does not authorize container writers to shared state. Bridge refresh uses this writer for both destinations and never the old CLI shared-copy directory deletion.

Alongside host substitution now recognizes the modular `wizard-agent-service` as well as legacy `wizard-shell`, not separate `wizard-ux`; the lifecycle install path supplies the existing override mechanism with the correct service id. Fresh mode recognizes the same two ids (above). No recipe edits, VM mutations, gateway restart for app lifecycle, or per-app MCP changes. Legacy `cli.py` bootstrap/alongside entry points keep their existing slot-default selection. Alongside shell/Service split overrides for `wizard-ux` belong to the modular-shell WAM branch; packaging/parent must select the modular service in its integration path.

Tests are HTTP fixtures using the real `Runtipi.req` + CLI JWT and stock-shaped SSE/contracts, executed on the Mac worker under its shared worker lease. They do not establish a live Runtipi install, phone journey or gateway/tool reload. Those remain the parent's isolated `wos-apps` VM acceptance gate.
