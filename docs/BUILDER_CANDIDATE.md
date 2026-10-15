# Portable builder candidate

Status: unmerged candidate adapted from Wizard OS PR24. Approved checks are unchanged; API/view-source metadata checks are opt-in proposal mode. This is source-bundle distribution (install with `python3 -m pip install -e .`), not wheel-only packaging.

`wam new`, `wam check [--proposal]`, `wam install <local-folder> --stage-only [--run]`, `wam publish --repo ... --output ...` and `wam builder-skill [--hermes-home ...]` dispatch through isolated builder modules. Public execution refuses. Local install never edits apps.json/registry.py or restarts Hermes. System skill lives outside generated app skills.

Integration seams: registry/lifecycle owner projects API and source metadata, exposes production app routes and preserves system skills. Packaging mounts `system-skills` read-only. Exact minimal shared registration is the early builder dispatch in wam.cli.main; production registry/lifecycle has no dependency on builder internals.

The complete source bundle includes template, skill/references and checker helper, plus repository GPL-3.0-only license. Exact source receipts and independent review live in the lane evidence, separately from user source.
