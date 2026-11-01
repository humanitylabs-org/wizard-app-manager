# Explicit service connection opt-out

Reuse: **EXTEND** the existing `x-runtipi` / legacy service schema, converter,
`DockerComposeBuilder` and `mountAppGrants`. **REUSE** `WizardStorage.prepare`,
`DockerService.prepareApp` and network enforcement unchanged. No second builder,
role inference, new grant store, permission grant, or runtime/inference change.
`wizard-ux/docs/COMPONENTS.md` covers UI facade capabilities; this change has no
UI and WAM has no separate COMPONENTS.md. Existing owners are the modules above.

## Recipe declaration

```yaml
services:
  private-data-init:
    image: your-existing-pinned-image
    x-runtipi:
      wizard_grants: false
    # Explicit user, mounts, capabilities and networking remain the recipe's job.
```

Legacy dynamic JSON uses `wizardGrants: false` on the service. Both formats and
architecture overrides roundtrip the declaration. The only accepted declared
value is literal `false`; omit it to preserve historical behavior. `true`,
strings, numbers, null and structures are invalid, not requests for more access.
The existing architecture resolver runs first; the injector consumes the resolved
service declaration before build-only service metadata is discarded.

An opted-out service receives neither the read-only `/wizard` grants mount nor
the writable `/wizard-requests` request mount, and neither `WIZARD_GRANTS_FILE`
nor `WIZARD_CONNECTION_REQUESTS`. Other services retain both mounts and variables,
including root and one-shot services without the declaration. Existing app-level
grants, provider endpoints, request collection, share selection, network policy,
image, user, capabilities, security options and declared environment are unchanged.
No grant is created, broadened or revoked by this opt-out.

Contradictory explicit connection mounts (including canonical app-data sources
at another target and normalized reserved targets), connection variables (map,
list or bare inherited form), and `env_file` / `extends` / `volumes_from` are
refused for an opted-out service, rather than silently removed. Direct builder
calls also reject an invalid declared value, even if they bypass schema parsing.

This is **connection-injection attenuation, not a generic data-only sandbox**.
It does not authorize otherwise forbidden mounts, host access or networks; it does
not suppress share injection. Do not put it on a main/share-carrying service and
assume isolation. Unrelated explicit sources, image defaults and owner-supplied
Compose overrides remain independent authorities. A data-only helper claim needs
complete post-`prepareApp` and Docker Compose config/daemon mount, environment,
capability and network inventories for the actual recipe. Do not test only the
source recipe or a hand-rebuilt stripped Compose file. Owner overrides that
reintroduce access invalidate that claim; this patch does not revoke owner powers.

## Focused acceptance, not rollout

- Dependency-free `node --test tests/credential-grants-optout.test.mjs` executes the
  exact source injector body and covers default behavior and fail-closed guards.
- `wizard-grants-optout.test.ts` exercises schema, converter roundtrip, architecture
  resolution, builder preservation, literal value validation and contradictory
  declarations. Focused unchanged grant/request contracts prove engine continuity.
- The isolated forum stage drives the actual app-files manager create-only data
  initialization, builder, storage preparation and network enforcement, then runs
  `docker compose config` with a synthetic env file. Retain complete final service
  inventories, not just forbidden-path predicates. Compare unchanged normal
  services and network declarations against the exact base builder.
- Run data-init and validator from that final document only, with their exact
  data-only mounts, network-none, root CHOWN-only and non-root cap-free identities.
  Exercise install, restart and regeneration/update while preserving synthetic
  existing data bytes/mode/owner; incompatible roots and links/special files refuse.
- The existing 40-case synthetic runner stays byte-unchanged, hash-pinned and
  separately accounted. It was never executed at staging time. Its raw-Compose
  fixtures do not prove the new final-rendered isolation boundary.

No live data, private agents, Messenger, recordings, credentials, provider
accounts, publication, rollout, restart or merges are authorized by this work.
PR382/404 approvals, the draft DO-NOT-MERGE state, Miguel's acceptance and the
other previously retained gates are not waived. The forum stage is not submitted
or executed by the source repair; acceptance remains pending until its real receipt.
