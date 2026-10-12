# Host lifecycle bridge

Staged integration entry point (no Hermes core or old WAM server registration changes):

```sh
WIZARD_OS_HOME=/path/to/isolated/wizard-home RUNTIPI_ROOT=/path/to/runtipi \
  python3 -m wam.lifecycle serve --bind 127.0.0.1 --port 7791
```

Only an IPv4 loopback bind is permitted. Use a service supervisor outside this module. Existing WAM/YAML dependencies are unchanged; HTTP server, locking, journal, SSE client and authentication use Python's standard library.

## Authentication and proxy boundary

The bridge uses `WIZARD_APP_MANAGER_TOKEN` if set, otherwise creates `$WIZARD_OS_HOME/lifecycle-token` atomically with permissions 0600. `--token-file PATH` explicitly selects an owner-only file instead. The dedicated token must have 32–256 header-safe characters from `[A-Za-z0-9._~-]`. Provision the same value into the Agent Service's **server-side** `WIZARD_APP_MANAGER_TOKEN` and configure its bridge origin as `http://127.0.0.1:7791` (no `/api` suffix). Do not put it in UX settings, bundles, browser requests, app manifests, or public registry files.

Every request, including reads, images and `/healthz`, requires `Authorization: Bearer <dedicated token>`. Any `Origin` header is rejected: the browser uses the existing same-origin session/CSRF-protected service routes, which reconstruct the allowlisted host request without forwarding browser Origin, cookies, auth or arbitrary headers. No CORS. The internal upstream-resolution endpoint below is **not** a browser proxy route.

Runtipi authentication uses the existing `wam.runtipi.cli_token` HS256 `sub=cli` mechanism and `JWT_SECRET` from the installed `.env`, not a harness-only dashboard login. The bridge's production Runtipi origin is `http://127.0.0.1:<NGINX_PORT>/api`. The existing Runtipi must already have an operator and the Wizard store configured; this module does not register users or modify host Runtipi config. Secrets, raw upstream errors and JWTs are not returned or logged.

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

An IPv4 address or DNS hostname is accepted verbatim; URL schemes, ports, paths, userinfo, whitespace, empty labels and invalid IPv4 are rejected. This setting maps registry `mcp.url`, `health`, and the private view upstream to that Service-reachable host, leaving browser `view.url` same-origin and Runtipi/JWT control traffic on host loopback. `mcp.internal` keeps existing app-network DNS metadata. This is declared addressing, **not connectivity proof**: a host port bound only to loopback may need packaging's explicit network forwarding before a container can reach it. No compose mutation, automatic host discovery, per-app MCP configuration or gateway restart occurs here. The parent must verify one Wizard gateway plus skill/orientation discovery and the actual application endpoint from inside the fresh Service/Hermes container. Old `wam doctor` remains unchanged; its seam is `wam.cli.doctor`, whose config/file checks cannot certify that container route.

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

Terminal failure response includes `{success: false, error: <bounded symbolic code>, urn, action, replayed, registry}`. Validation/auth/refresh errors before a complete envelope return only `{success: false, error}`. Codes include `unauthorized`, `invalid_app`, `invalid_path`, `invalid_body`, `invalid_json`, `invalid_query`, `invalid_form`, `unexpected_fields`, `required_fields_missing`, `idempotency_key_required`, `idempotency_conflict`, `operation_journal_invalid`, `operation_unconfirmed`, `dependencies_required`, `required_slot`, `app_busy`, `app_not_installed`, `app_not_found`, `view_not_found`, `not_found`, `method_not_allowed`, `body_too_large`, `lifecycle_failed`, `completion_stream_unavailable`, `invalid_completion_event`, `completion_unconfirmed`, `untrackable_operation`, `terminal_status_unconfirmed`, `projection_refresh_failed`, `upstream_unavailable`, `unsupported_image`, `image_too_large`. No arbitrary exception message or upstream body is exposed.

Thread + `fcntl.flock` serialization covers reads, mutations, polling, refresh, and durable journal decisions, including multiple bridge processes sharing one home. A key is reserved atomically before upstream calls. Journal request fingerprints are keyed HMACs, never raw submitted credentials; generated required passwords are atomically saved 0600 and reused. Completed duplicate/failure deliveries replay their recorded outcome, but still refresh from Runtipi. All operation outcomes including failures and conflicting retries refresh **both** local and shared `apps.json` and app skills. A refresh failure returns 502, never stale success. Required-slot uninstall rules are preserved. Installs with missing dependencies fail `dependencies_required`; the integration owner installs dependencies individually (no hidden unjournaled recursive installs or Hermes/per-app MCP restarts).

### Deliberate crash/timeout policy

Missing SSE receipt, lost upstream response or a process crash after journal reservation is **uncertain**, never success and never blindly reissued. Both the same key and new mutations for that app return `operation_unconfirmed`; other app operations are independent. Operator reconciliation is required before clearing that specific journal reservation. A genuinely completed `lifecycle_failed` receipt may be retried with a new key after inspecting refreshed app state. Keep the journal across restarts; no TTL expiry silently repeats an uninstall/update/restart. The stock SSE app events have no request id, so the bridge serializes its own submissions and assumes other administrators do not operate the same app concurrently through the Runtipi dashboard. This assumption is a real integration gate, not a claimed cross-dashboard exactly-once guarantee.

## Files and scope

`registry.write` updates individual files atomically using fsync + rename; skill ownership manifests protect non-app/system directories, user-modified skills and `system-skills/wizard-builder`. Obsolete **unmodified generated** app SKILL.md files alone are removed. Registry and skill trees are not a multi-file transactional filesystem snapshot. Bridge refresh uses this writer for both destinations and never the old CLI shared-copy directory deletion.

Alongside host substitution now recognizes the modular `wizard-agent-service` as well as legacy `wizard-shell`, not separate `wizard-ux`; the lifecycle install path supplies the existing override mechanism with the correct service id. No compose edits, VM mutations, gateway restart or per-app MCP changes. Legacy `cli.py` bootstrap/alongside entry points are unchanged and retain their existing slot-default selection; packaging/parent must select the modular service in its integration path.

Tests are HTTP fixtures using the real `Runtipi.req` + CLI JWT and stock-shaped SSE/contracts, executed on Thales under `/Volumes/ThalesMedia/AndroidBuilds/worker.lock`. They do not establish a live Runtipi install, phone journey or gateway/tool reload. Those remain the parent's isolated `wos-apps` VM acceptance gate.
