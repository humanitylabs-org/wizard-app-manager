# Slice 2 — read-only installed-box audit

**Draft branch only; do not merge or deploy.** Audited the real long-lived alongside installation, using its installed `apps/<store>/<id>/docker-compose.yml`, user-config overrides and current path/environment context. No live Config object was constructed, no approvals saved, no service changed and no Docker/API call made. Python bytecode writes are disabled in the harness.

## Result

- **32 installed app/store pairs** enumerated (including the same app id in different stores).
- Before these corrections, the coordinator's direct recipe/override probe flagged **22/32**.
- Corrected policy: **9/32** have remaining host-access risks; **23/32** pass without consent.
- Exact upgrade migration simulated in memory: **0/32 blocked**. All 9 existing host-access apps retain their current risk sets. This was a dry run, not a live migration.
- Default Docker capabilities, same-app read-only source pins and registry-derived same-agent home mounts no longer cause false positives.

**Provenance finding:** the 8 remaining host-network overrides are owner/local integrations, not output of any current WAM generator. They have no durable manager provenance; some have only owner-local comments. The current WAM alongside writer generates the shell/Agent Service/bundle overrides, not these apps. Treating a comment or an app id as authority would allow a restored/hand-edited recipe to bypass consent. These genuine host-network integrations therefore remain visible and are preserved by the upgrade snapshot, rather than falsely described as safe recipes. Notifications' cron folder, Mizel's meetings folder and Connectors' shared Runtipi network remain explicit host-access risks as required.

## Sanitized per-app table

No host paths, environment values or secrets are included.

| Store | App | Remaining categories | After simulated upgrade |
|---|---|---|---|
| core | browser | none | pass |
| core | transcriber | none | pass |
| core | wizard-chatgpt | none | pass |
| core | wizard-claude | none | pass |
| wizard | defleur-video | none | pass |
| wizard | hello-wizard | none | pass |
| wizardapps | agent-philosopher | none | pass |
| wizardapps | agent-private | none | pass |
| wizardapps | airlock | none | pass |
| wizardapps | cinequeue | uses the server network | existing access preserved |
| wizardapps | defleur-video | none | pass |
| wizardapps | meditate | none | pass |
| wizardapps | messenger | none | pass |
| wizardapps | mizel | reads or writes files outside its own storage, uses the server network | existing access preserved |
| wizardapps | notes | none | pass |
| wizardapps | notifications | reads or writes files outside its own storage, uses the server network | existing access preserved |
| wizardapps | outline-streaming-demo | uses the server network | existing access preserved |
| wizardapps | profiles | none | pass |
| wizardapps | reference | uses the server network | existing access preserved |
| wizardapps | search-preview | none | pass |
| wizardapps | stories | none | pass |
| wizardapps | usuals | none | pass |
| wizardapps | wisdom | uses the server network | existing access preserved |
| wizardapps | wizard-calendar | none | pass |
| wizardapps | wizard-claude | none | pass |
| wizardapps | wizard-connection-router | uses the server network | existing access preserved |
| wizardapps | wizard-connectors | joins a shared network | existing access preserved |
| wizardapps | wizard-files | none | pass |
| wizardapps | wizard-quest | none | pass |
| wizardapps | wizard-recorder | uses the server network | existing access preserved |
| wizardapps | wizard-shopping | none | pass |
| wizardapps | wizard-tinfoil | none | pass |

## Reproduce (read-only)

```sh
python qa/privsep-slice2/installed-audit.py --root "$RUNTIPI_ROOT"
python qa/privsep-slice2/installed-audit.py --root "$RUNTIPI_ROOT" --grandfather-dry-run
```

The actual run supplied the real installation root explicitly. Set `--home` if the manager registry lives somewhere other than the root's sibling `.wizard-os`. `--json` prints the same sanitized inventory and counts. Exit 1 means at least one app would need new consent without the migration; the dry run returns 0. It never records consent. The test suite also checks that running the harness leaves a fixture installation's file bytes and modification times unchanged, and denies subprocess/directory/file preparation during alongside regeneration.

The persisted migration runs once at manager Config startup, before any recipe mutation, and reads **installed** copies, never incoming store recipes. It records sorted risk strings, `source: grandfathered at upgrade` and a UTC timestamp for every installed app/store pair. A later install or restored package cannot enroll itself. New risk strings still need the verified owner route/local administrator; same/subset risks do not. `wam doctor` lists the preserved host access in ordinary words.
