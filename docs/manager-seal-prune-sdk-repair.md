# Installed guard: actual dockerode prune bytes

QA-only repair; product and saved backend build inputs remain unchanged. This is not installed acceptance.

## Wire provenance and refusal

Captured from the actual public dockerode 4.0.9 and docker-modem 5.0.6 packages with `/home/telos/.local/bin/node` v22.22.3, through a local raw Unix socket server, not Docker. Both tarball SHA-512 integrities match `bun.lock`; the 64-package dependency closure was fetched from npm using the exact lockfile versions and every tarball's SHA-512 checked before extraction. Durable capture scripts, dependency pins and archives live under the continuation's proof directory, outside the repository.

`manager-seal-prune-sdk.http` is the complete 277-byte request, SHA-256 `32fb827e053c0c056d7ad151e432ba3bb9e8618f8ff51abcfed96897dbaaf197`:

```http
POST /containers/prune?filters=%7B%22label%22%3A%5B%22runtipi.appurn%3Dagent-synthetic%3Amigrated%22%5D%7D HTTP/1.1
Content-Type: application/json
Content-Length: 65
Host: localhost
Connection: keep-alive

{"filters":{"label":["runtipi.appurn=agent-synthetic:migrated"]}}
```

The raw fixture retains CRLF bytes and the original header order. The JSON fixture records bytes/base64, SDK integrity pins, Node pin, and the exact previous prune branch extracted from commit `18e32355313b1ceb62a3cc86bb2c88ed91016dde` (guard SHA-256 `448ebd7da086a3dc722ba068042e6d2fac2920ff55ddc9b2abd7c09632f0aed1`).

`dockerode/lib/docker.js:954-983` sets path `/containers/prune?`, POST, and options to the caller's opts. `docker-modem/lib/modem.js:160-165` serializes object-valued query options as JSON, while `:207-221` also serializes nonempty POST options into the JSON body and emits content-type/length. This is duplication by the real SDK, not a hypothetical request shape.

The guard still NEVER forwards prune. Query keys must be exactly `filters`, the selector must canonicalize to the exact synthetic app-URN label true-set. With no body this remains typed. With a body, it must be at most 4096 bytes of strict UTF-8 JSON with unique keys and no NaN/Infinity, exactly `{"filters": X}`, with the same canonical selector as the query. Both legacy arrays and bool true-set maps are valid independently; mixtures inside a selector, false/non-bool entries, extras and mismatches fail closed. Typed results return no caller body; only canonical filters enter the ledger. All other shapes yield generic 403 with no caller data.

## Regressions

The raw fixture is sent unchanged through real `Server`/`Handler`/`Policy` with an instrumented local upstream peer: current code gives typed `prune_refused` 403, zero upstream calls. The exact historical prune branch restored in-memory refuses these same bytes at historical line 358 as `rule_refused`. The historical parser is restored too; unrelated policy is retained.

Negative raw-wire cases cover differing query/body filters, extra body/query keys, foreign labels, malformed JSON, body over 4096 bytes, duplicate keys/query parameters, invalid bool sets, mixed array/map selector contents, nonfinite constants, non-UTF-8 JSON, BOM and trailing data. They assert generic 403, zero forwards and no caller content in the ledger. This does not waive strict accounting.

Retained `18e32355313b` ledger bytes are SHA-256 `a69edf1f4bf3b7f38463f52d13620824f2f59754e825d13e5971c902aceb3b4f` (403 rows). Replay fails exactly `unexpected guard refusal`: nine probe refusals plus seq 19, rule line 358. Replacing ONLY seq 19 with the typed canonical row passes full reconciliation: 9 exact probes, 1 install prune in [18,44], activation seq 16, 8 paired network discovery/creations over 2 cycles. Probe identities, sequences, labels, install window and discovery pairing remain required.

## Other POST audit and limits

The production Dockerode instance originates in `modules/docker/docker.module.ts` and is passed via `AppLifecycleCommandFactory` into commands; `SubnetManagerService` and `AppStatusSyncService` also receive it. Exhaustive non-test backend TypeScript occurrence audit found the install `pruneContainers` call as the only product Dockerode POST. Subnet allocation uses `listNetworks` (GET); status synchronization uses `listContainers` and container `inspect` (GET). Start and restart explicitly call `ensureAppDir(..., {pruneContainers:false})`. `DockerService.composeApp` invokes the Compose CLI, not Dockerode; bootstrap has no daemon-wide network prune. No additional product Dockerode body/query guard repairs are justified.

Unproven/future-call suspects: if code begins invoking Dockerode container `stop({t:...})`, `kill({signal:...})`, or `wait({condition:...})`, docker-modem would duplicate nonempty opts in query/body and the unchanged owned-mutate guard (`not data`) would refuse. Dockerode `start` with nonempty options also requires a separately justified shape. Network connect/disconnect and create calls have body-based APIs and existing closed guards; no product callsite was found for them. Do not loosen them on speculation. Compose/Go HTTP shapes are not proven by this JavaScript capture; a fresh installed lifecycle run remains pending. Full official-image acceptance and the existing update/rollback contract gaps remain unproven.
