# Privilege separation — Slice 2 receipt

Status: implemented on the draft `feature/agent-privilege-separation` branch only. **Do not merge or deploy.** No VM was started and no stock Hermes code was changed.

## Scope and seams

- REUSE: `network.network_escapes`, Runtipi path/env expansion rules, Store listener/root-peer owner identity and CSRF checks, backup filesystem adapter and per-machine exclusion contract.
- EXTEND: all install/update paths, final builder output before Compose execution, backup preflight and restored user-config before any Runtipi start; structured consent failures; existing lifecycle tests and asynchronous Store status.
- NEW: one `wam.host_policy` admission allowlist, recipe-bound `approval-registry.json`, a manager-owned Approve page and `wam approve APP SHA256` local administrator fallback. No Wizard card or shell adapter is implemented here.

Unknown executable Compose settings fail closed. Host namespaces, runtime sockets, devices, elevated capabilities/options, sysctls, host file-backed env/secrets/configs, external binds, host build contexts/Dockerfiles, build secrets/SSH/entitlements and custom host network/volume driver settings require owner consent. The exact reviewed metadata mounts/Files capabilities and privilege-reducing `no-new-privileges` option are documented in [STORE-AUDIT.md](STORE-AUDIT.md). There is no blanket system-app or host-network bypass.

## Consent and owner routes

A refused bearer API install/update returns HTTP 409 with `error: needs_owner_approval`, `app`, `risks`, `approve_url` and the terminal `command`. The CLI prints the same JSON and exits **23**. Async Store installs retain the structured refusal; their status endpoint returns 409 once refusal is known. `--allow-host-access` is accepted for compatibility but never approves.

The Approve page is served by the existing manager Store listener, through `/wizard-os-home/store-install/_approve/APP/SHA256`. GET never approves. POST requires an actual root-owned loopback peer, the node owner's Tailscale login, and the same Origin/custom-header checks as Store writes. Tagged nodes refuse. Owner identity is pinned on service startup or first verified pairing; drift refuses and is reported by `wam doctor`.

Owner-only API mutations: `/api/grants`, `/api/network`, `/api/updates`, `/api/updates/apply`, `/api/backup/import`; the strict owner Store listener exposes the same finite actions under `_owner/`. No token/header-only approval route exists. Local CLI commands remain available to a human administrator. Backup repository restore (`--from-repo`) and undo remain agent actions; backup **file** import requires owner identity on the API.

**No usable owner login:** the refusal includes the exact command `wam approve APP SHA256`. A human administrator runs it as the service user, then retries the refused action. No password fallback was added. Approval binds the recipe and executable user override, plus path-affecting runtime values; changing them requires new consent. Approvals/pins do not travel in backup bundles.

**Interim boundary:** until Slice 3, the agent still shares the service uid and can invoke the local administrator CLI or alter uid-owned files. Slice 2 removes flag/API self-approval, not that uid-level capability. The WAM paths are guarded; direct Docker/Runtipi reach is not claimed to be isolated until the later identity/token migration.

## Full suite — real forum output

Toolchain: `forum`, Python **3.13.5**, Node **v20.19.2**. Both copies have a Git index so portable-path tests execute normally. Python comes from the already-installed isolated test environment; no VM or durable-heavy job was used.

Command in each copy:

```sh
PATH=/srv/work/privsep/slice1/20261027T161948Z/venv/bin:$PATH python -m pytest -q tests
```

| Copy | Tested source | Result | Exit |
|---|---|---|---|
| `/srv/work/privsep/slice2/baseline-0ff7c879` | `0ff7c879` | **1400 passed, 1 failed, 21 skipped, 15 subtests passed**, 291.42s | 1 |
| `/srv/work/privsep/slice2/work-20261027` | `ff329ed42dd38817c69aff6d7e9e9b71a629217a` | **1444 passed, 1 failed, 21 skipped, 15 subtests passed**, 295.40s | 1 |

The **same sole failure** in both is `tests/test_chats_views.py::test_typescript_public_index_path_has_identical_seal`. Forum's Node rejects `--experimental-strip-types` with exit 9. It was not changed, skipped or marked expected. This is baseline parity, not an all-green suite. Full outputs: [forum-baseline.log](forum-baseline.log), [forum-candidate.log](forum-candidate.log).

There are 44 new audit regression cases. They exercise host root, privileged mode, sockets, build secrets/SSH/host contexts, host env files, namespace/capability/device options, mount normalization and symlinks, changed-recipe/value invalidation, obsolete flag refusal, backup override preflight, forged owner headers, wrong root-peer login, tagged nodes, owner pin drift, HTTP 409/readback, all owner-only API verbs, asynchronous status, and final builder-output admission. Existing legacy alongside tests now explicitly simulate human approval instead of grandfathering host access.

## Store and UI evidence

- All **8/8** current store recipes pass; **0** need consent at their default settings.
- All **14/14** default Compose recipes pass; **0** need consent. Troubleshooter is a native host service with no Compose recipe, explicitly recorded as N/A rather than invented coverage.
- Every default id and store recipe is counted by `qa/privsep-slice2/audit.py`; `sources.json` pins each recipe to its source commit. `python qa/privsep-slice2/audit.py --check` prints PASS.
- Actual isolated Approve page inspected at **390 × 844**, dark and light token palettes: no horizontal overflow, readable risks, visible **54.78px** approval button. Images: [dark](screenshots/approval-dark.png), [light](screenshots/approval-light.png). The layout preview used a throwaway mocked owner identity, not live tailscaled; its temporary server was stopped.

## Remaining release gates

No live installation, real tailscaled identity path or disposable-VM acceptance is claimed. The whole release still needs the later service/agent uid separation and coordinator-owned end-to-end fresh/alongside acceptance. The existing Slice 1 VM runner remains staged; this task intentionally ran no VMs. PR stays draft with `do-not-merge`.
