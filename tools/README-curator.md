# Default-app curator copies

Contract, locations, commissioning and revocation:
[`wizard-os/releases/README.md`, Curator custody](https://github.com/humanitylabs-org/wizard-os/blob/main/releases/README.md#curator-custody-decisions-83).

`mirror_defaults.py --root <WAM-checkout-with-release-mirror> --work <external-disk-cache> --report <receipt.json>`
reads active release snapshots and the index-pinned publications of WAM defaults.
Run on Thales only. It requires PyYAML, Git and crane v0.20.6. `GH_TOKEN`
authenticates release uploads (sources are refused if GitHub marks
them private). `crane auth login` supplies the job-scoped GHCR token. The
scheduled WAM workflow uses its own token for contents/package writes; no new
secret or deploy key is needed. No app code, build, submodule hook or install script runs.

Source mirrors are append-only Git bundle assets in this repository's public
`curator-source-mirrors` prerelease, named `<owner>--<repo>--<commit>.bundle`.
Do not delete or replace existing assets. Each bundle contains an advertised
`refs/tags/curator/<owner>/<repo>/<commit>` ref, but builder commits are never
pushed into WAM's Git history. Anonymous download into a fresh empty repository,
Git fsck and full commit comparison verify every source asset after publication.
Images use the already-public WAM package's `curator-sha256-<digest>` tags;
these are preserved upstream images, not App Manager releases. Production
WAM image tags are never overwritten by custody maintenance.

Regression proof (local fixture repositories; no network):

```
uv run --with PyYAML --with pytest --with pyjwt python -m pytest -q tests/test_curator_mirrors.py tests/test_packaging.py
bun test tests/curator-mirrors.test.ts
```

Both install implementations fetch a real Git mirror after a simulated missing
origin, download a real bundle, compare the full HEAD SHA, and reject an
intentionally mispointed bundle ref. Size-limit and corrupt-bundle cases fail closed;
maintenance tests prove existing copies work without builder access, readback
uses fresh Git objects, private source is refused and asset lists are paginated. Python tests also package the recovered bytes as both source and build.
