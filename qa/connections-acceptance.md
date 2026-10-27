# App walls: independent acceptance

James approved app walls on 27 October 2026. Spec: wizard-os#274. Implementation: #271, held for the Connections preview; this acceptance does not authorize merging or deploying it.

Oscar Henry Collins (@oscarhenrycollins), for Miguel, authored the implementation and pushed the eight review fixes before this acceptance began. Build on `f7e44a3e`, do not recreate his changes.

## Reuse plan

- REUSE: the existing Settings group/rows and `renderConnections`; shared `WizardApp` from `/wizard-os-home/core/app-client.js` for Appearance; the real shared `appearance-host.js` and `skins.js` in the synthetic harness.
- EXTEND: the existing standalone `.wux` CSS aliases with `--wz-*` roles (embedded `.wam` keeps inheriting the shell's aliases), readable light semantic colours, wrapping badges and 44px connection rows; existing network tests with fail-closed unavailable preflight and immediate CLI grant/revoke ordering.
- NEW: no production capability, private Appearance receiver or theme storage. `qa/connections-appearance.cjs` is an isolated synthetic acceptance harness, not a second UI implementation.

## Review finding → regression

All Python names below live in `tests/test_network.py`.

| Finding | Verified behaviour / regression |
|---|---|
| Grant changes reconcile immediately | `test_a_revoked_grant_takes_the_link_away_and_automatic_grants_follow_their_declaration`; `test_grant_and_revoke_reconcile_live_networks_before_registry` covers both CLI actions |
| Host access changes require approval before update | `test_update_that_adds_host_access_needs_the_owner` |
| Exposed core containers retain app gateway networks | `test_exposed_core_components_stay_on_the_gateway_networks` |
| UI installs use isolation preflight | backend `wizard-networks.test.ts`: “never puts an unexposed Wizard app on the shared network, with no policy file (fail closed)”; `test_reconcile_writes_the_app_managers_policy_first` |
| UI updates reconcile their network changes | `test_serve_watcher_reconciles_after_every_wizard_app_start`; backend “drops wam's managed entries for services an update renamed, keeping everything else” |
| Failed preflight stops update | `test_update_stops_when_its_network_preflight_fails`; strengthened with `test_update_stops_when_network_preflight_is_unavailable` (Docker unavailable is a failure, not approval) |
| Arbitrary external network attachments are refused | `test_external_and_foreign_named_networks_are_escapes_and_never_isolated`; backend “refuses networks the app does not own unless the owner allowed it” |
| Served views only advertise backed APIs | `test_wam_serve_page_offers_only_the_views_its_api_backs`; loopback Connections-only page, rather than advertising unimplemented Store/My Apps/Resources APIs |

The release-image tests explicitly fake a successful network preflight: they test image selection, not Docker. The fail-closed network tests still exercise refusal and prove no update is queued.

## Appearance acceptance

Run on a Linux worker, with an actual Playwright installation and the real Wizard shared core directory mounted:

```sh
WIZARD_CORE_DIR=/path/to/wizard-ux/web/core \
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
node qa/connections-appearance.cjs /path/to/output
```

The harness reads no box state. It serves only the candidate views, real shared Appearance code/palettes and synthetic JSON. It asserts Nebula and Obsidian Light at 390×844 and 1440×900; normal, drift, unavailable and empty states (16 cases/JPEGs), row expansion, revoked-grant copy, minimum touch height, no horizontal clipping, computed text colour, no page errors, JPEGs below 1 MB, and live skin changes retaining the expanded row. It records evidence in `results.json`.

Verified focused checks: 51 Python tests (network + release-image selection), four Node view tests, and 16 browser/Appearance cases. Full Python, backend, TypeScript and image checks are additionally required on the exact proposed head in GitHub CI. This synthetic preview does not claim a physical-phone test, a live Wizard test, or fresh Docker VM acceptance.
