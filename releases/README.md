# Public Wizard OS release metadata

This directory publishes only the installer’s release metadata. The canonical Wizard OS specification repository remains private; no source checkout, credentials or owner configuration is mirrored here.

`get.sh` reads `stable`, then `<version>.json`. `WIZARD_RELEASE_BASE` still supports an explicitly chosen alternate mirror or local release directory.

## 0.2.2 provenance

- Canonical source: `humanitylabs-org/wizard-os`, merge `8f54a471535a66afbdda3520dd6b6374541bb143`, `releases/0.2.2.json`.
- Byte-identical SHA-256: `8a0d3394c92e01984069647c8e6f2f7f21ddd5ef7a26c8e84daa18429d747c84`.
- Every image is pinned by immutable digest and verified for amd64 and arm64. Component commits and publication evidence are in the manifest.
- This snapshot includes split UX/Agent Service images and the App Manager installer fixes. It does not include later UI commits simply because they are newer.
- Alongside installation consumes exact split-image pins. Fresh installation still uses catalog defaults and reports drift where those differ. This metadata publication is not a claim that full fresh-machine acceptance has passed.

The canonical manifest’s private-URL caveat records its original publication boundary. This public mirror closes that metadata-availability gap; it does not expose the private repository or alter the selected image identities.

Future releases copy only reviewed release JSONs and advance `stable` through a pull request. Preserve old versioned manifests byte-for-byte.
