# Slice 2 store and default-app admission audit

Reproduce with `python qa/privsep-slice2/audit.py --check` (PyYAML required).

The fixtures are byte-for-byte recipes at the commits in `sources.json`. Every current recipe in the store checkout is included, plus the default-app source recipes not in that checkout. No app was executed or deployed.

## Store recipes

| App | Result | Reason | Source |
|---|---|---|---|
| browser | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| defleur-video | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| files | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| hermes-agent | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| hermes-frontend-3 | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| secret-drop | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| transcriber | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| wizard-ai-accounts | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |

## System/default apps

| App | Result | Reason | Source |
|---|---|---|---|
| browser | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| files | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| hermes-agent | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| secret-drop | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| transcriber | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-app-store@9a6c5553f8c2](https://github.com/humanitylabs-org/wizard-app-store/tree/9a6c5553f8c26d3be3a52012c73c39fdf37c996c) |
| wizard-ux | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-ux@3781ae1cd55a](https://github.com/humanitylabs-org/wizard-ux/tree/3781ae1cd55ae5a7fa05d52dfe131c96886d8c6b) |
| wizard-agent-service | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-agent-service@73c4138aab38](https://github.com/humanitylabs-org/wizard-agent-service/tree/73c4138aab38534ac5e7df4f5d3a55631bcc9740) |
| wizard-chatgpt | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [oscarhenrycollins/chatgpt-connection@cdc92a755fe0](https://github.com/oscarhenrycollins/chatgpt-connection/tree/cdc92a755fe0d54bf3bc3c89a1fbf1368b8c5074) |
| wizard-claude | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [oscarhenrycollins/claude-connection@caa507980866](https://github.com/oscarhenrycollins/claude-connection/tree/caa507980866e259db34771142e5f1dfa69d4f2d) |
| wizard-troubleshooter | N/A (native host component) | No Compose recipe; installed as a host service by `wam.troubleshooter`, not admitted through app install. No host-access bypass added. | [humanitylabs-org/wizard-troubleshooter@48a99e6ae5f5](https://github.com/humanitylabs-org/wizard-troubleshooter/tree/48a99e6ae5f5a39f5a9c260ed6e50299122ba53b) |
| wizard-connection-router | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-connection-router@ec8ca140494b](https://github.com/humanitylabs-org/wizard-connection-router/tree/ec8ca140494b302a9d4e651417162b89f1d9e282) |
| wizard-voice-service | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-voice-service@14d990e5bc64](https://github.com/humanitylabs-org/wizard-voice-service/tree/14d990e5bc64b69604a7308641be4f1f0cf40c7f) |
| wizard-cloudflare | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [oscarhenrycollins/wizard-cloudflare@63ee34aa1c4d](https://github.com/oscarhenrycollins/wizard-cloudflare/tree/63ee34aa1c4d1a86e88dfdd52212a361d3ccc188) |
| wizard-connectors | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [humanitylabs-org/wizard-connectors@8808e72b7a33](https://github.com/humanitylabs-org/wizard-connectors/tree/8808e72b7a3375cef0f47a19a736dfd8b95afe39) |
| notifications | pass | Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable. | [oscarhenrycollins/notifications@cdce9f18d10f](https://github.com/oscarhenrycollins/notifications/tree/cdce9f18d10f74e1714ee2284a1c55497b49eef1) |

## Totals

- Store: 8 recipes; 8 pass; 0 need owner approval.
- Defaults: 14 Compose recipes; 14 pass; 0 need owner approval; 1 native component without a Compose recipe.

## Reviewed exceptions (finite, in `wam/host_policy.py`)

- All apps: `cap_add` may restore capabilities within Docker's default set: `AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD NET_BIND_SERVICE NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_CHROOT`. Adding these after `cap_drop` gives no more power than a default container. Other capabilities (including `SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE` and `ALL`) still require consent. This replaces the Files-only exception.
- Agent Service: exact read-only `${ROOT_FOLDER_HOST}/app-data/wizard-os/hermes-view:/hermes:ro` and `${ROOT_FOLDER_HOST}/app-data/wizard-os:/wizard-os:ro` metadata projections.
- Connection Router: exact read-only `${ROOT_FOLDER_HOST}/app-data/wizard-os:/run/wizard:ro` metadata and `${ROOT_FOLDER_HOST}/repos:/run/recipes:ro` recipe catalog.
- `no-new-privileges:true` (or `=true`) is an allowlisted privilege-reducing option, not a seccomp/AppArmor exemption. Every other `security_opt` requires consent.
- Same-app pinned source: read-only `${APP_SOURCE_DIR}`, `<root>/repos/<store>/apps/<id>/releases/<rev>` and `<app-data>/.wizard-source/<store>/<id>@<rev>` projections pass, in short or long mount syntax. Check exact prefixes, same app/store, traversal and physical resolution; writable binds, another app's pins and symlink escapes still require consent.
- Manager wiring: regenerate the alongside shell/service/bundle and compare before exempting it. Builder and Claude-login shares exempt only their exact current generated fragment, derived from installed bundle/alongside state, not the override marker. Extra-agent home binds are derived from validated same-agent registry metadata, with finite source/service/target tuples. No broad host-network/app-id bypass exists.
- Named volumes are allowed as required, including explicitly named/external Docker volumes; bind-style `driver_opts` and non-local volume drivers require approval.

## Expansion and limits

`APP_DATA_DIR` resolves to this app in Runtipi app-data (including configured appDataPath / RUNTIPI_APP_DATA_PATH); `ROOT_FOLDER_HOST/media` is the files root. Relative bind paths resolve against the app Compose project directory. Long-form mounts, `..`, existing filesystem symlinks and runtime socket sources are checked. Unknown path variables fail closed unless a default expands into an allowed root. Current app/user environment and the final install form are checked. Risk strings include resolved host-path/device/network identity and mount access mode, so changing these can introduce a new risk. Consent binds the sorted risk set, not recipe bytes; same/subset risks pass after image/comment-only updates.

The Notifications default HERMES_CRON_DIR fallback is app-local and passes; setting it to another app/home requires owner approval. These default-recipe results are supplemented by [the real installed-box audit](INSTALLED-AUDIT.md). The one-time upgrade snapshot preserves each installed app/store pair's current risk set with a timestamp and `grandfathered at upgrade` source. Doctor lists existing host-access apps in plain words. New or restored access outside that set still needs owner consent; safe new apps cannot grandfather themselves. Owner-local host-network overrides without reproducible manager provenance stay visible as host access, rather than being silently exempted.

This is source/policy admission evidence, not a VM or live-box conformance claim. The agent still shares the service uid until Slice 3; a human administrator can use the local CLI. No live box, stock Hermes, host privileges or release were changed.
