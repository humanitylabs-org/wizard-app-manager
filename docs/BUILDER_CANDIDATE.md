# Portable builder candidate

Status: unmerged candidate adapted from Wizard OS PR24. Approved manifest checks are unchanged; API/view-source metadata checks are opt-in proposal mode. The James-approved shared-system-function source gate is mandatory in every builder check/scaffold/stage/update/publication-preparation path. This is source-bundle distribution (install with `python3 -m pip install -e .`), not wheel-only packaging.

`wam new`, `wam check [--proposal]`, `wam install-local <local-folder> --stage-only [--run]`, `wam publish --repo ... --output ...` and `wam builder-skill [--hermes-home ...]` dispatch through isolated builder modules. Public execution refuses. Local install never edits apps.json/registry.py or restarts Hermes. System skill lives outside generated app skills.

Integration seams: registry/lifecycle owner projects API and source metadata, exposes production app routes and preserves system skills. Packaging mounts `system-skills` read-only. Exact minimal shared registration is the early builder dispatch in wam.cli.main; production registry/lifecycle has no dependency on builder internals.

The complete source bundle includes template, skill/references and checker helper, plus repository GPL-3.0-only license. Exact source receipts and independent review live in the lane evidence, separately from user source. The installed skill requires `references/system-functions.md`; `wam/system_functions.py` enforces it via `builder.check_app`, not a doc-only optional checklist. Template loads the central client and removes app-local Back/theme transports. Standalone preview must mount the same approved core assets. No wizard.md schema change, public execution or production registry mutation is introduced.

## Development harness scope

The system-function gate scans runtime source and known inline Compose payloads.
Top-level `tests/`, `test/`, `qa/` and `ops/` harnesses are omitted only when both
`view.source.path` and `view.source.dist` name existing, confined directories
outside those trees. This uses existing view metadata, not an app-local ignore
file. Missing, ambiguous or escaping metadata keeps the full scan. A runtime
import, URL or server-path reference to a development tree brings the whole tree
back into scope; nested paths such as `web/tests/` are never omitted. Symlinks
remain errors, including in development trees. The canonical style policy and
its strict/new-line ratchet are unchanged. This lint is not a JavaScript sandbox;
it does not execute a server to infer dynamic routes.

