# Slice 3 acceptance: alongside install with an unprivileged agent

## Clean two-guest acceptance at 1e3a3f4c (forum KVM, 27 Oct 2026)

Overall: PASS. Evidence: forum `/srv/work/privsep/slice3/20261027T220832Z-staging/evidence` (receipt.json).

- Candidate (unprivileged `hermes`, no sudo): `id hermes` = hermes + wizard-files only; real kernel denials for
  sudo, Docker API, protected files and Tailscale mutation; `wam serve` and Runtipi run as `wizard-system`;
  thin client installs, restarts, reads logs and uninstalls an app; agent token refused on approval even with
  forged owner headers; doctor 10/10 ok; Agent Service at the agent uid.
- Baseline (main 67141228, same clean image): cannot complete — `named_host_ready` requires
  `/run/user/1000/bus`, absent after successful administrator host preparation (Debian 12 cloud image).
  This is a bug on main for exactly this setup; see BASELINE-REPAIR.md.
- Full pytest: 1555, only the pre-existing `test_chats_views::test_typescript_public_index_path_has_identical_seal` failure.
- Both VMs stopped.
