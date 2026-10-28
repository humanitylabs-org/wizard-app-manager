# Slice 5 clean-guest acceptance

Run only on the admitted Linux/KVM worker, not on an owner's box. Interface:

```text
python3 qa/privsep-slice5/run.py EVIDENCE_DIR SOURCE_EXPORT CANDIDATE_SHA [--smoke|--preflight]
```

`EVIDENCE_DIR` must be a new path under `/srv/work/privsep/slice5/`.
`SOURCE_EXPORT` is the exact exported candidate tree; its parent must contain
`source-export.json` with `head_sha` equal to the full candidate SHA. `--preflight`
executes the real argument checks and loads/compiles the reused functions, without
creating evidence or booting a VM. The submitted full run omits `--smoke`.

The runner loads definitions (not entry points) from the accepted Slice 1/4
runner: clean Debian 12 cloud image, Docker mirror, pinned stock Hermes installed
as unprivileged `hermes`, root-entered protected alongside bootstrap at the pushed
candidate, source/doctor readback, and the exact finally-stop teardown. Slice 5
replaces Slice 4's post-install actions; no host mounts or real model/Tailscale
credentials are supplied. A deadline signal enters teardown, not an orphan VM.

Guest assertions use the real root-owned helper, sudoers policy, `visudo`, systemd
timer, service-uid layout, HTTP owner boundary, thin-client token and sudo log:

1. `sudo -n true` fails as unprivileged Hermes; agent token / forged owner headers
   cannot enable developer mode, apply a source or grant access; sub-switch is
   refused with developer mode off; sudo rejects helper extra arguments.
2. Box-local root owner selects developer mode, grants 120 seconds; Hermes sudo
   succeeds and appears in the actual dedicated sudo logfile; complete sudoers
   validates; actual doctor API contains the red full-access row; security-file
   changes are detected; Runtipi/manager stay wizard-system-owned.
3. Wait for the **actual root timer**, then assert the file is removed and Hermes
   sudo fails again. No manual revoke/status-triggered expiry substitutes for it.
4. Regrant as owner, then `developer off --setting-only`; source reverts to the
   release setting without replacing the acceptance candidate, access is revoked,
   and sudo fails. Unit tests exercise dispatch through the release installer.
5. Full pytest runs in the same job via Slice 4's suite method. The receipt accepts
   exactly `tests.test_chats_views::test_typescript_public_index_path_has_identical_seal`
   as the sole known baseline failure. Empty/different failure lists fail admission.

`receipt.json` requires every phase, initial doctor, expected suite result and
confirmed VM teardown. Logs and their SHA256 remain in the evidence directory.
The staging `job.json` is a submission request, **not proof the job ran**.
