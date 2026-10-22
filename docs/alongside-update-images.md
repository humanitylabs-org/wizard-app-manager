# Alongside update image reconciliation (#113)

An alongside `user-config/wizardapps/<app>/docker-compose.yml` is merged **after** the generated recipe. Updating only the recipe/source leaves the saved `image:` in control. The existing `wam app update` path did not refresh that override. Re-running `wam alongside` without transient image environment variables also retained its old saved pin.

## Change

- Reconcile selected `WIZARD_RELEASE_PINS` images before writing a core alongside override and before queuing a core `wam app update` (short IDs and `:wizardapps` URNs).
- Reconcile alongside-enable from the manifest even when transient image variables are absent.
- Precedence: explicit developer image environment override, selected release digest, then saved image when no release is selected. A prior receipt never selects policy.
- Keep `previous_images[app]` on actual transitions, including an explicit downgrade; no-op repeats do not overwrite it. This is a retained reference, **not an automatic rollback implementation**.
- Preserve host Hermes, host networking, loopback ports and sharing settings; other stores are untouched. Existing update API/backup/restart semantics are unchanged.

## Verification

At commit `1c5d2ba` on `fix/alongside-update-image-113`:

- New regression against unchanged `main` `23e4090`: **9 failed, 1 passed**. Failures include stale pins reaching the queued API call and the actual Docker Compose merged configuration retaining the old image.
- Same focused tests on Thales after the fix: **10 passed in 0.36s**, including two **real Docker Compose `config --format json`** merges (no daemon/container startup required).
- Full WAM Python suite in a disposable Linux container on Thales: **501 passed, 9 skipped in 69.19s**. The container runs tests as a non-root user with system Python + PyYAML installed, matching installer fixture assumptions. Skips: two Compose CLI tests (covered by the native Thales focused run), five optional ruamel.yaml plugin tests, opt-in Tailscale contract check, private upstream provenance check. Existing short-HMAC fixture warning only.
- Node `tests/runtipi-open.test.mjs`: **4 passed**. Existing package module-type warning only.
- `git diff --check`: passed.

Native macOS whole-suite execution was unsuitable (70 failures from Linux installer fixture assumptions); a first root Linux run and an incomplete-system-Python non-root run also exposed harness assumptions. The corrected non-root Linux full run above is the acceptance result; none of those environment failures were hidden or patched into product code.

## Remaining acceptance boundary

No disposable-VM release upgrade, running Agent Service revision check, image-delivery UI request or rollback execution was performed by this lane. That remains with launch lane L4's upgrade run / release owner. This lane proves the old saved override is replaced before the queued update and that Compose selects the release digest; it does not claim container or end-to-end VM acceptance. No release files, tags, production installs or other contributors' branches were changed.
