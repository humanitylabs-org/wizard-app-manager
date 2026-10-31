# Narrow guard/producer repair: independent c92d review

Scope: the three reproduced guard/producer blockers only. No Manager product or
runtime change; all edits are to synthetic acceptance tooling/tests/docs.

## Closed typed wire schema

The forwarding guard closes each admitted Go-struct object before semantics:
container config, HostConfig, RestartPolicy, LogConfig, Mount, BindOptions,
Healthcheck, NetworkingConfig, EndpointSettings, network-create, network IPAM,
and connect/disconnect. Unknown members and case aliases are rejected even when
zero/null-looking. Disabled device/ulimit/blkio/portbinding/IPAM objects remain
entirely disallowed when nonempty, including nested aliases. Map keys such as
labels, exact network names and bind paths are not case-folded. Primitive types
are exact (not Python's bool-is-an-int equality); numeric float/string/bool
substitutes cannot change resources, flags, prefixes, restart retries or timing.

For the pinned SDK's endpoint operational fields, admit only typed zero/null:
NetworkID, EndpointID, Gateway, IPAddress, IPPrefixLen, IPv6Gateway,
GlobalIPv6Address, GlobalIPv6PrefixLen, DNSNames. The same zero-state restriction
covers the other non-alias endpoint settings. Populated addresses/identities,
DNS names, link-local/IPAM state, driver options, MACs and gateway priority are
not delegated. Aliases remain only a string array. The admitted payload is
forwarded rather than rewritten into a fabricated success response.

## Typed network discovery, not ignored denials

Only GET of an exact configured, currently uncreated network name, after
activation and without query/body, receives local 404. No upstream connection
opens. Its ledger operation is `discovery-miss`, not `deny`/`forward`. Unknown
names/IDs/prefixes still get refusal; owned names continue to rewrite to exact
returned IDs. Creation still requires the existing labels/names/policy.

Accounting pairs each miss once with the next same-name network creation intent
and returned ID. Duplicate/unpaired/unknown discoveries, missing configured
network discoveries, and all unexpected refusal counts fail. Producer readiness
requires exactly one cycle per four configured networks. Installed start/restart
requires exactly two, separately from the existing exact explicit-probe denial
count, ten returned container IDs, real event reconciliation and cleanup.

## Actual producer witness before Manager bootstrap

`manager_seal_producer_witness.py` runs the SHA-pinned Compose v2.40.0 binary
(SDK v28.5.0) against the same five-service `fixture()` document and unchanged
forwarding handler. A counting Unix protocol peer has no Docker client or real
daemon path. Only synthetic image metadata and admitted create/inspect/list
protocol operations are implemented. The producer gets an empty synthetic HOME,
explicit guard DOCKER_HOST, no inherited Docker configuration/environment, and
no pull/build/start flags. The output preserves the real forwarded request body
bytes/digests, guard ledger and actual producer exit. A positive witness requires
all five ordinary create bodies with SDK zero endpoint fields, exactly four
local discoveries/creations, no refusal and exact upstream/forward counts.
Unsupported protocol or guard rejection is failure, never installed proof.

The installed controller runs this witness before dependency bootstrap, build,
infrastructure, Nest or Manager admission. A separate
`--producer-readiness-only` stage exits after that witness, uses the original
owned cleanup path and cannot set the installed-pass field. Its stage has a
`-producer` suffix so it cannot consume the full installed stage's one-use
controller/run paths. The owning parent first reviews the raw readiness trace,
then may separately admit the full installed job. Both jobs verify the entire
source manifest before importing the runner. The full job repeats the required
prebootstrap witness rather than relying on an unbound older trace.

## Local executed regressions

The focused run executed **44 tests**, zero failures/errors/skips, including
**1,166 real Unix-socket refusal assertions** that also require zero upstream
requests. It covers the existing guard/controller/capacity invariants plus the
new closed-schema and witness-harness regressions; no broad product suite was
rerun. A separate before/after replay of the independent review cases observed:

| case | reviewed baseline (status / upstream requests) | repaired (status / upstream requests) |
| --- | --- | --- |
| restart case alias | 201 / 1 | 403 / 0 |
| ordinary pinned endpoint shape | 403 / 0 | 201 / 1 |
| configured fresh-name discovery | 403 / 0, denial | 404 / 0, typed discovery miss |

AST comparison to the reviewed baseline confirms `lookup`, `response`, `event`,
`cleanup`, `reconcile_creation_intents` and `emergency_cleanup` are unchanged.
These local results do not assert the actual pinned Compose binary passed.

## Evidence boundary

Local tests use real counting Unix sockets, not Docker. The producer-witness
unit tests verify its pinned-binary refusal, real protocol-peer transport,
request bytes/counts, and ordering of the early-return admission fence; they
are **not execution of Compose**. The ordinary producer-shaped JSON positive
regression is a public-source-linked replay, not the actual binary witness.
The actual pinned binary witness and full installed run remain for the owning
parent's durable worker admission; no such run/submission occurred in this
repair. Previous synthetic 19/22/60 proofs remain unchanged and are not new
installed evidence.

Resource and authority bounds remain: cached image pins; 200 GiB free reserve,
1 GiB next additional allocation, 4 GiB retained-plus-pending peak (James-approved task-only expansion; see manager-seal-installed-recovery.md); 12 GiB
aggregate declared RAM, 8 GiB aggregate tmpfs, 64 MiB fixture, no swap. No raw
socket is added to Manager; unrelated IDs/resources remain refused. No changes
to owned cleanup authority, activation, private data, live services, core,
models/providers, migrations, key retirement, pulls or merges.

Public producer references:
- https://raw.githubusercontent.com/docker/compose/v2.40.0/pkg/compose/create.go
- https://raw.githubusercontent.com/docker/compose/v2.40.0/go.mod
- https://raw.githubusercontent.com/moby/moby/v28.5.0/api/types/network/endpoint.go
- https://pkg.go.dev/encoding/json#Unmarshal
