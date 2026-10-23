# Private-access release acceptance

Passing unit tests or an already-joined demo is not a new-owner onboarding receipt.
Do not log out a demo/production node or change its routes/Serve to test onboarding.
Do not use copied auth keys, a shared team login, or implicit sudoers grants.

## Evidence levels

- `tests/test_private_access_setup.py`: command doubles only. Covers absent binary
  (official-install URL, not an actual package installation), connected node (only
  status calls; no reauthentication or Serve 443 mutation), stopped node, service
  failure, unavailable status, login link/wait/resume, custom flags, DNS/HTTPS off,
  and unsafe fallback output. Simulated login completion is not a human login.
- Opt-in `WIZARD_TEST_OFFICIAL_TAILSCALE=1 python -m pytest
  tests/test_private_access_official.py -v`: actual official host binaries mounted
  read-only into an existing disposable Python container. `--network none`, private
  tmpfs state/socket, userspace networking, no host socket or privileged networking.
  Real daemon-down error and fresh `NeedsLogin` state; complete preference equality
  before/after retry with DNS acceptance off, route acceptance on, custom hostname.
  `systemctl` is a no-op here: this does not validate distro package/service setup.
  No control-plane access means no login URL or owner authentication is exercised.
- Fresh authorized disposable guest + real owner browser: required before claiming
  onboarding complete. Capture public os.sh hash, installer commit, stable manifest,
  binary version, status transitions and resulting private URL; never credentials.

## Bug contract

Official Tailscale 1.102.3 reproduces a destructive old fallback: after an initial
`up`, set non-default preferences, then `up --timeout=1s` refuses and prints the
preservation command. `login --timeout=1s` switches to an empty profile and resets
DNS/route acceptance and hostname even without network connectivity. Therefore
never use `login` as an alleged settings-preserving fallback. Retry the CLI's
explicit preservation command as validated argv, not evaluated shell text. If
its output changes, fail closed rather than reset preferences.

## Outstanding release gates (human owner required)

On a disposable guest, exercise each separately:

1. No Tailscale binary: official package download/install and real systemd daemon;
   detached `start` returns promptly, `status` exposes one owner sign-in link.
2. Existing logged-out node and expired node key: both should reach `NeedsLogin`;
   prove real key-expiry/reauthentication, rather than assigning that state in a
   fixture. Retain custom DNS/routes/SSH/hostname preferences and Serve 443.
3. Open the one link on the owner's phone/laptop, including a person without a
   Tailscale account/client. Provider sign-up, client install and any device
   approval/admin policy are real owner gates; do not claim the installer does
   those steps. Verify same-account device can open the final Wizard URL.
4. While waiting, poll `status`, complete browser login, observe the same detached
   runner resume; no duplicate install or gateway interruption. Test link expiry,
   owner cancellation and 1800-second deadline separately. Current timeout path
   clears the owner-action marker and finishes box-only with a rerun note: it is
   not a successful private-access receipt and does not retain a pending link.
5. Already joined with custom flags and an occupied Serve 443: read back flags,
   Serve mappings, original service health and Wizard's distinct private URL.
   Setup's status-only behavior does not prove subsequent app publication ports.
6. Root and named Hermes owner **without passwordless sudo**: detached installs
   cannot answer a sudo password prompt. Existing root-to-named-owner preflight
   refuses absent administrator-provisioned sudo; no automatic NOPASSWD grant is
   allowed for alongside installs. Test the named-user entry path separately and
   require a clear administrator handoff before mutations. Do not treat the fresh
   dedicated wizard user's existing NOPASSWD bootstrap as this acceptance case.

Release source merge and public-script delivery are distinct: `get.sh` fetches the
private-access helper from the stable release's WAM commit. A source fix is not
customer-delivered until a new immutable release pins it and public hash/pins are
read back. Coordinate that release with the release owner; never rewrite 0.3.1.
