# Store Get and exact release changes

The Store install listener remains loopback-only, reachable only through Tailscale Serve's root-owned peer socket. It requires the node owner's Tailscale identity, an Origin with an actual Serve mount to this listener, JSON and `X-Wizard-Store: 1`. Merely supplying identity headers on loopback is insufficient.

`POST /wizard-os-home/store-install/<app>` accepts `{source, repo, commit, operation}`. `operation` is `install`, `update`, or `switch`; commit is a full immutable SHA. These are selection identifiers, **not installation authority**. The server resolves them against the configured public index, owner-private catalog, or the owner's existing Alpha subscription. Unknown sources, extra payload fields, stale pins and mismatched repositories fail before preparation. Old `{}` Standard Get requests remain supported only for an unambiguous public-index identity; they cannot select private sources or updates.

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
