# Phone release mirror

`wam phone-sync` fetches
`https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases/phone/latest.json`
(the public mirror of the canonical wizard-os `releases/phone/latest.json`, beside the box snapshots; wizard-os itself is private, so boxes cannot read it) and and the
`phone.json` of the newest published wizard-ux release
(`https://github.com/humanitylabs-org/wizard-ux/releases/latest/download/phone.json`, written by the wizard-ux
Phone release workflow, which cannot edit the pointer above), and mirrors the newest valid one this box admits
(highest `versionCode` whose `min_box_snapshot` the box meets; either source may be missing or invalid, then the
other is used). So a CI phone release reaches boxes within the hour with no pointer PR (wizard-ux#1399). It mirrors
the verified public Android release onto this box. It never installs anything, restarts a container, or changes a
maintenance/update setting. This is independent of DECISIONS #91's `check`/apply policy.

## Layout and timer

- Host directory: `${RUNTIPI_ROOT}/app-data/wizard-os/phone/`
  (stock: `$HOME/runtipi/app-data/wizard-os/phone/`). This is the existing public app-data projection, not the
  manager's private credentials directory.
- Service container: `/wizard-os/phone/` under the existing `/wizard-os:ro` mount.
- `latest.json` contains `app`, `version`, `versionName`, `versionCode`, relative `url`, `bytes`, `sha256`,
  `signer`, and `notes`. APK name: `wizard-os-<versionName>.apk`.
- `wam phone-sync --ensure-timer` installs user units `wizard-phone-sync.service` and **`wizard-phone-sync.timer`**.
  Every installer run calls it unconditionally. The timer runs hourly (up to 5 minutes jitter), with a first
  boot/activation tick after 2 minutes and Persistent=true. It captures this installation's WIZARD_OS_HOME and
  RUNTIPI_ROOT. It does not consult the OS update mode, channel, or maintain mode.
- `wam maintain` is not on current main at this change's base (`8bda1e49`); its proposed implementation is a
  separate PR. No synthetic maintain command or second maintenance loop is added here. The independent timer
  works with either future maintain policy.

## Validation and atomic publication

Pointer cap: 64 KiB; APK cap: 200 MiB. Pointer/app/version/size/hash/signer/source-commit fields are validated.
Only the canonical wizard-ux GitHub release asset URL for that version is accepted. GitHub asset redirects are
followed over HTTPS (never downgraded to HTTP). No GitHub credentials are sent. Requests have socket timeouts;
APK reads also have a five-minute total deadline. A loopback HTTP fixture mode exists only as a Python test
argument, not as a CLI option or environment override.

The minimum-box check uses `wam.updates.installed()`: the successfully installed snapshot in
`$WIZARD_OS_HOME/release.json` (status pinned or drift), the same receipt used by box updates. Unknown or older
snapshots skip the mirror. SemVer prereleases sort before the corresponding stable snapshot.

A nonblocking flock serializes timer/manual runs. Never downgrade an existing served versionCode. An identical
versionCode + digest is a no-op only after checking the APK size and hashing the actual local file. Versioned
APK URLs are immutable: a conflicting digest under an already published URL is rejected.

Download to a unique temporary file in the feed directory, verify declared bytes and SHA-256, chmod 0644 and
fsync, then check the APK's real signer (`wam/apk_sig.py`, wizard-app-manager#789), then rename the APK.

**Signer check.** A release's `signer` field is only a hint. The APK's own v2/v3/v3.1 signing block decides: every
signer must carry the certificate whose sha256 is `SIGNER`, its public key must be that certificate's key, its
RSA PKCS#1 v1.5 (SHA-256/512) signature over the signed data must verify, and the signed content digests must match
the APK's bytes. An APK with no v2/v3 block, another key, a copied certificate with a forged signature, or edited
contents is refused, and an already mirrored APK that fails the check is never reported `unchanged`. Pure Python,
no extra dependency.

**CI lead cap.** A wizard-ux release whose `versionCode` is more than `MAX_CI_LEAD` (20) above the reviewed pointer's
is ignored, so bigger jumps need a pointer PR and one odd release can't freeze the feed with `skipped:no_downgrade`.
If the pointer is unreachable the cap is measured from what the box already mirrors (outcome
`skipped:ci_release_too_far_ahead`). A feed already above the cap (mirrored before this check) is replaced by the
newest admitted release while the reviewed pointer is reachable. The same cap and signer check must apply to any
future Mac (or other) app mirror before it ships. Write/fsync/rename the JSON pointer only after the APK is ready. Superseded
`wizard-os-*.apk` files are deleted only after publication. Failure never changes the old feed or removes its
APK. Temporary files are cleaned up. The directory is 0755 for the non-owner container uid.

One CLI outcome line is printed. Missing pointer (404), offline, invalid pointer/APK, snapshot gate, busy mirror,
no-downgrade and unchanged releases all exit 0, leaving retries to the next tick. Timer installation errors exit
nonzero so the installer can report the missing timer.

## Owner feeds and the feed watchdog (wizard-ux#1768)

Some boxes serve `/reports/wizard-os/` from a private owner feed in a Hermes home
(`$HERMES_HOME/data/apps/private-reports/wizard-os/`, also `~/.hermes/...` and the alongside agent's Hermes home)
instead of the mirror folder above. Before this, only wizard-ux `tools/publish_owner_phone.py`, run by hand, moved
that folder, so James's feed sat on 0.10.58 while 0.10.59 and 0.10.60 shipped. `wam phone-sync` (the hourly timer
runs `phone_sync.run`) now, after the mirror:

- brings every such owner feed (one with a `latest.json`) up to the mirror's release: the same bytes, checked again
  for size, sha256 and the real signer; the old `latest.json` kept as `latest.json.bak-<versionCode>`; never a
  downgrade; `latest.json` written last; under the `.owner-publish.lock` that `publish_owner_phone.py` takes too; old
  APKs there are never deleted. An owner feed still on a build older than versionCode 82 (0.10.18, the first build
  that saves the Wizard's address on the phone) is left alone (`owner:skipped:pre_preference`) and reported, because
  the shared build has no address built in;
- remembers in `$WIZARD_OS_HOME/phone-feed.json` the newest admitted release and what each feed serves. A feed that
  stays behind it for 3 hours, or is 2 or more releases behind for an hour, sends one high-priority note to the
  owner's Notifications inbox (once per release, retried while the inbox doesn't take it). No release source readable
  for 24 hours sends one note too;
- `wam doctor` shows `Phone update feed is current`: it fails when a feed is behind for an hour or more, when the
  hourly mirror hasn't run for 3 hours (or never finished a run since its timer was installed), when no release could
  be read for 24 hours, and when the box serves an owner feed but the timer isn't installed.

The CLI line gains ` owner:<outcome>` per owner feed and ` behind:<feeds>` when the watchdog sees a stuck feed.

## Rollout boundary

This change is not a deployment. Publish a box snapshot pinning this App Manager and the Service feed-route
image together. A report-only box needs one explicit update to install the new code/timer. An unpublished
pointer is a logged no-op, not an install failure. The stock shell's catch-all nginx proxy already forwards
`/reports/wizard-os/` to the Service, and its existing read-only app-data mount covers this directory.

Test: `python -m pytest -q tests/test_phone_sync.py tests/test_updates.py`.

Pre-updater manual install belongs in wizard-ux `web/setup/setup.js` step 3 (`step3`, QR/connect URL). That
view should resolve the feed's relative APK URL; neither this manager's installer nor the Service owns that UI.
