# Store Get and exact release changes

The Store install listener remains loopback-only, reachable only through Tailscale Serve's root-owned peer socket. It requires the node owner's Tailscale identity, an Origin with an actual Serve mount to this listener, JSON and `X-Wizard-Store: 1`. Merely supplying identity headers on loopback is insufficient.

`POST /wizard-os-home/store-install/<app>` accepts `{source, repo, commit, operation}`. `operation` is `install`, `update`, or `switch`; commit is a full immutable SHA. These are selection identifiers, **not installation authority**. The server resolves them against the configured public index, owner-private catalog, the owner's existing Alpha subscription, or (for `source: 'link'`) the checked manifest fetched from GitHub at the selected commit. Unknown sources, extra payload fields, stale pins and mismatched repositories fail before preparation. Old `{}` Standard Get requests remain supported only for an unambiguous public-index identity; they cannot select private sources, links or updates.

## Remove app (`wam/store_remove.py`)

The same listener and gate also serve the Store's **Remove app**. App ids can't start with `_`, so these routes never shadow an app:

- `GET store-install/_remove/<app>/plan` is read-only. It returns `{app, name, installed, removable, protected, reason, identity, dependents, empties, backups}`:
  - `identity` is the installed recipe's `.wizard-source` repository and source plus its `.wizard-commit`;
  - `dependents` are installed apps that list it in `uses` or hold a grant to it;
  - `empties` are capabilities that nothing else installed provides.

  Wizard's own apps are not removable: release defaults, `wizard-ux`/`wizard-shell`/Agent Service/Hermes, `system`, shell or interface apps. Neither is the only installed provider of a required spot (`slots.blocks_uninstall` over apps that are installed now), or an app being installed. Removal fails closed unless the identity is exact: `apps.json` must be readable and hold a record under the app's own id (no `app_id` alias, no Runtipi-only install), and that record and the installed recipe must name the same repository and full 40-character commit.
- `POST store-install/_remove/<app>` takes exactly `{repo, commit, dependents, empties}`, copied from the plan the owner reviewed. Before anything runs, the plan is computed again. If the identity, the dependents or the emptied spots differ, the request gets 409 and nothing is removed.
  - A second tap with the same identity joins the running job.
  - A different identity gets 409. A Store Get or Update for that app gets 409 while its removal runs or its outcome is `unknown`.
  - A repeat after `removed` returns the same receipt only while the app is still gone.
  - Removals run one at a time. Each one plans again right before the lifecycle call, counting only apps that are still installed, so two removals can never empty a required spot between them. If that second plan differs, the job is `failed` and nothing is removed.
- `GET store-install/_remove/<app>` returns the job state. The job runs `cli.uninstall`, the App Manager's lifecycle uninstall. It deletes the app's own folder and app data, and keeps earlier backups (`removeBackups: false`) and any pre-migration Docker volume (`docs/APP_STATE_AND_SHARES.md`). Each outcome is decided by reading the installed list back, for up to 30 seconds after the lifecycle call returns:
  - `removed`: the app is no longer listed;
  - `failed`: the app is still listed and Runtipi reports it `running` or `stopped`, and the job reason names the error;
  - `unknown`: the app is still listed in any other state (still uninstalling), or the readback fails.

  The Store never treats `unknown` as success. An `unknown` job keeps the barrier: a later read of the job, a repeat request or a Store Get/Update reads the lists back first and settles it as `removed` or `failed`.

## Share-link selections

A link request supplies exactly `{source: 'link', repo: 'OWNER/REPO', commit: '<40 lowercase hex>', operation: 'install'}`. The commit is mandatory; branches, tags, URLs and browser-supplied manifest/policy fields are refused. The repository must match `[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*`. No catalog membership or Alpha subscription is required. WAM fetches the exact commit with the existing SHA-verified `private.fetch` path, runs the shipped `wizardmd.check` rules, and requires the manifest id to equal the requested app id. The resolved entry is built only from server-fetched source. Preparation repeats the checker before packaging or lifecycle side effects. Public links need no GitHub token; private links use the server's GitHub login, including `GITHUB_TOKEN` or `~/.wizard-os/github-token`. Git authentication is passed per command as an environment-configured HTTP header, never saved in Git config, a URL, a receipt or logs.

Link `update` and `switch` require an installed `.wizard-source` with the same repository **and** `source: 'link'`; a link cannot silently take over a Standard, owner-private, Alpha or legacy unlabelled install. Existing non-link selection policy is unchanged. Installs and release changes retain native manager install/update/backup handling.

## Recorded install source

Every newly prepared recipe records `source` in `.wizard-source`: `standard`, `owner-private`, the subscribed Alpha source id, or `link`. Standard manager/index installs also record `standard`. The Alpha adapter's existing invocation/activation policy is unchanged; WAM passes its resolved source id in the owner-local `WAM_INSTALL_SOURCE` environment for the child WAM preparation. Registry generation copies the installed identity's source to `apps.json`; a prospective store recipe cannot relabel a Link install. A local-folder identity (`{"local": "<folder>"}`), `repo: 'local/…'`, or explicit `source: 'local'` records `local`. Legacy identities without a source remain `standard` unless local. Local paths are not copied into the registry.

GitHub sign-in UI remains a follow-up: **Apps → Sources → Sign in to GitHub**.

The server never executes a browser-supplied URL, image, ref, executable or adapter. Private releases use the existing credential, verified release archive/image and Compose safety checks. Public releases are prepared from the resolved repository at the exact approved commit. The prepared recipe carries `.wizard-selection`; the maintained Runtipi store honors it even during a fresh installation/update copy instead of silently replacing it with the latest public lookup. Explicitly selecting a later release prepares a replacement pin. A selected recipe stays pinned for ordinary manager updates until explicitly replaced.

Updates/switches require an existing installed identity with the same repository, read from the **installed** files rather than the prospective store recipe. They use native manager update and its default backup; there is no uninstall, volume removal or implicit conversion of Get into Update. The completed operation checks the installed-copy repository and commit before reporting success.

Alpha uses the installed shell's bundled `wizard-alpha.mjs` and its existing owner-local subscription/install policy, asset verification, pinned lifecycle gate and authenticated activation receipt. A missing subscription, policy or activation adapter remains an explicit error; Store Get does not invent or grant one. Catalog refresh never installs anything.

Jobs retain a public selection receipt. Repeated taps join only the same source/repository/commit/operation. A conflicting operation returns 409. Receipts contain no credentials, policy commands, filesystem paths or raw catalog entries. The UI checks the receipt and registered installed commit before saying ready.

Deploy the manager image containing `.wizard-selection` support **before** the companion Python API and shell UI. This changes no stable release pointer; normal Wizard OS releases remain independently pinned snapshots.

Verification:
- `python -m pytest -q`
- `node --test tests/runtipi-open.test.mjs`
- `cd packages/common && bun run build`
- `cd packages/backend && bun x vitest run && bun run tsc`
- Companion `wizard-ux/tools/qa-store-installs.cjs` exercises negative-control prompts, exact source installs, updates/switches, wrong receipts, denied owners and missing manager routes. Every write is intercepted and service workers are disabled.
