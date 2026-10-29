# Pinned producer wire-schema readiness repair

This is a synthetic-protocol readiness repair, **not an installed Manager pass**.
The actual pinned Compose binary must still be run by the owning parent's durable
forum job. Do not submit or activate the installed path on this document alone.

## Preserved failure and provable incompatibility

The failed source is `70079303f247877e591fd7e9ecfb699ffe1d5aa7`;
producer-only job `heavy_5743a9eac37a4ac9bd28c944e0e2836b` exited 1.
Its unchanged witness SHA is
`c4894f2b00fb4aa28733b33608974c63c8532d47a0fb84a4902eaf278cc584e0`.
The original trace has a configured-name local discovery miss, filtered network
list, then closed-shape rejection before upstream network creation. **It did not
retain the refused incoming body or producer stderr. Those bytes cannot be
reconstructed or assigned an original-request digest.** The old stage and its
receipt/ledger/output/cleanup are retained; exact copies and hashes are in
`manager-seal-network-schema-evidence/failed-70079303/failed-bytes-index.json`
in the repair workspace. The parent's job-log SHA remains separately labelled
as parent-reported, not confused with the controller-output digest.

Public-source snapshots and URL/byte/SHA manifest are committed under
`scripts/tests/fixtures/manager_seal_wire/`. Compose v2.40.0's `go.mod` selects
Docker SDK v28.5.0 and docker-image-spec v1.3.1. `create.go` supplies no
`Scope`, `ConfigOnly`, or `ConfigFrom` to `network.CreateOptions`. That SDK's
struct declares all three **without omitempty**, its client embeds the options
in `CreateRequest`, and its request encoder uses `json.Encoder.Encode`.
Thus the source serializer necessarily emits `Scope:""`, `ConfigOnly:false`,
`ConfigFrom:null`. All three names were absent from the failed guard schema.
This proves a wire incompatibility, not which original body bytes were sent.

The fixed source-derived request is
`scripts/tests/fixtures/manager_seal_wire/network-create-source-replay.json`:
203 bytes including final newline, SHA
`92e124673daa05c8db54883cf44b645a325e5c97b7e5aa64f6e306bcc9308dc9`.
The test derives its exact field order and unused values from those public
structs and replays the bytes unchanged over Unix sockets. **This is not a
captured old request, actual SDK execution, or actual Compose execution.**

## Minimal admission change and collective review

Only these additional unused forms are admitted:

| wire field | admitted when present | explicitly refused |
| --- | --- | --- |
| network Scope | exact empty string | local/swarm/whitespace/null, wrong types |
| network ConfigOnly | exact boolean false | true, null, numeric zero, other wrong types |
| network ConfigFrom | null only | even an empty object, any Network reference, aliases/types |
| container OnBuild | null or empty array | any instruction, nonempty/malformed array or wrong type |

`container.Config.OnBuild` is another unconditional field left nil by this
Compose producer; the collective review found and fixed it now, rather than
waiting for a second producer run. Unknown names and case aliases remain refused,
including zero/null-looking additions. Nonzero authority is never normalized
away and forwarded bodies are never rewritten to fabricate success.

The source audit compares every member of the relevant request structs together:
container CreateRequest/Config, HostConfig/embedded Resources, network
CreateRequest/embedded CreateOptions, EndpointSettings, IPAM, RestartPolicy,
LogConfig, NetworkingConfig, Mount, BindOptions, and HealthcheckConfig. After
this repair there are no missing unconditional members. Known omitted members
are omitempty and deliberately not delegated: container ArgsEscaped/MacAddress/
Shell; HostConfig Annotations/StorageOpt/KernelMemory/KernelMemoryTCP; and mount
VolumeOptions/ImageOptions/TmpfsOptions/ClusterOptions. The fixture does not set
them. Disabled address-pool, endpoint IPAM, and source-network-reference objects
are inventoried, not opened to arbitrary members. Connect/disconnect member sets
are also checked. This is a source/schema review, not a guarantee against a
future semantic or protocol-peer mismatch in actual producer execution.

## Failure evidence retained before authorization

Only the synthetic witness installs `SyntheticPolicy`/`SyntheticHandler`.
The general guard gains **no request/body/header logging**. The producer still
has a synthetic empty HOME/config, explicit synthetic socket, clean environment,
and unchanged no-pull/no-build/no-start command. The protocol peer has no real
Docker daemon path. No private home/files/env/history/prompts/media are inputs.

Before each policy authorization the synthetic wrapper durably appends request
method, exact target, HTTP headers and exact body (base64, optional valid UTF-8,
byte count and SHA) to `producer-incoming.jsonl`. The witness JSON additionally
contains each decision/refusal's source frames, all incoming requests, forwarded
upstream trace, guard ledger, and trusted failure source frames. Discovery misses
and refused requests are retained, not just requests that reached the peer.
Unsupported peer operations return failure with method/target/body and error
class retained. SHA of the exact fixture and pinned binary bind the evidence.

Stdout and stderr are drained separately and retained as exact
`producer-stdout.bin` / `producer-stderr.bin` bytes, with size/hash, replacement
UTF-8 for reading, exit/timeout/overflow flags in the witness. Each stream retains
at most 64 KiB; overflow kills/reaps the producer and fails readiness. There are
at most 192 incoming and 192 upstream requests, each trace capped at 4 MiB and
the combined JSON envelope at 24 MiB. Trace caps fail closed and mark an
incomplete trace; they never manufacture completeness or a pass. Normal failure,
output overflow and timeout persist the bounded evidence before propagating
failure. The incoming JSONL is written before authorization, even if subsequent
producer checks fail. It contains requests that reach the authorizer, not a raw
capture of malformed HTTP rejected earlier by the HTTP framing checks.

If the next actual producer fails, inspect `producer-stderr.bin`, then the
refused row's `incoming_trace` and `refusal_frames`, or the last `upstream_trace`
row with `peer_error`. Use exact base64 bytes for replay, not a manually guessed
shape. Any protocol failure still occurs before Manager build/bootstrap/start.

## Executed local evidence and remaining gate

The final focused run passed **54 tests**, zero failures/errors/skips, with
**1,262 real Unix refusal assertions** requiring zero upstream calls. It includes
populated/casing/type negatives for all new fields; all ordinary unconditional
container fields together; retained denied-body/headers/stderr/trace digests;
output overflow/timeout/caps; and the existing guard/controller/capacity checks.

Exact source replay control results, using the same bytes on each side:

| case | failed guard | repaired guard |
| --- | --- | --- |
| network-create source replay | 403 / zero upstream | 201 / one upstream, identical bytes |
| container OnBuild null | 403 / zero upstream | 201 / one upstream, identical bytes |

`local-tests-r5/{focused-tests.json,focused-tests.log,source-replay-before-after.json}`
under `manager-seal-network-schema-evidence/` contains the executed evidence.
AST comparison retains lookup/response/event unchanged; the complete installed
controller file is byte-identical to the failed baseline, preserving cleanup,
creation-intent reconciliation, emergency cleanup and prebootstrap fencing.

Stage only the new exact-source **producer-readiness-only** spec. Do not reuse
or overwrite the failed stage. The owner must review the next actual trace before
any separately admitted installed run. The pinned Compose SHA remains
`bd5835ccbbf06a42dcb5294c65e34a4634b34447afb9ed6fc7adf18a000e0f99`.
No actual Compose/Docker/Manager run or durable job submission was performed in
this repair. Old synthetic 19/22/60 evidence is unchanged. All resource/authority
bounds remain: 200 GiB reserve floor, 1 GiB next additional allocation, 3 GiB
retained-plus-pending peak, 12 GiB memory, 8 GiB tmpfs, 64 MiB fixture, no swap.
No installed submission, activation/restart/migration, private data, live changes,
pulls, key retirement, provider/model/core changes, or merge is authorized here.
