# Default-app curator copies

Contract, locations, commissioning and revocation:
[`wizard-os/releases/README.md`, Curator custody](https://github.com/humanitylabs-org/wizard-os/blob/main/releases/README.md#curator-custody-decisions-83).

`mirror_defaults.py --root <wizard-os-checkout> --work <external-disk-cache> --report <receipt.json>`
reads active release snapshots and the index-pinned publications of WAM defaults.
Run on Thales only. It requires PyYAML, Git and crane v0.20.6. `GH_TOKEN`
also authenticates source pushes (public sources are refused if GitHub marks
them private). `crane auth login` supplies the job-scoped GHCR token. The
scheduled WAM workflow uses its own token for contents/package writes; no new
secret or deploy key is needed. No app code, build, submodule hook or install script runs.

The mirror source repo is this repository, under append-only lightweight
`curator/<owner>/<repo>/<commit>` tags. Do not force-push, prune, or run WAM
CI/release jobs on these tags: the referenced trees are app source, not WAM.

Regression proof (local fixture repositories; no network):

```
uv run --with PyYAML --with pytest --with pyjwt python -m pytest -q tests/test_curator_mirrors.py tests/test_packaging.py
bun test tests/curator-mirrors.test.ts
```

Both install implementations fetch a real Git mirror after a simulated missing
origin, compare the full HEAD SHA, and reject an intentionally mispointed mirror
ref. Python tests also package the recovered bytes as both source and build.
