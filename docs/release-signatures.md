# Signed releases (warn-only, off by default)

wizard-app-manager#565, step A of wizard-os#416. Code: `wam/release_sig.py`; trusted keys: `wam/release_signers`;
tests: `tests/test_release_signatures.py`.

## What it checks

Before `wam updates --apply` / Automatic uses a release's pins (full installer rerun or quick update):

1. **The release file**: `releases/<version>.json` against its detached signature `releases/<version>.json.sig`.
2. **The promotion record**, when the release has one: `releases/promotions/<version>.json` against `<...>.json.sig`.
3. **The installer**: `wam` itself downloads `https://aiwizards.com/os.sh`, hashes it (never trusting anything the
   installer prints) and compares it with the hashes the release file names, `"installer": {"os.sh": "<sha256>", "get.sh": "<sha256>"}` (aiwizards.com/os.sh serves get.sh, so
   either name matches). With the check on, the installer then runs from that checked file (`bash <file> start`), so
   the detached runner is a copy of exactly those bytes instead of a second download of get.sh.

States: `signature` ok | missing | bad (with a `reason`: changed file, a key not on the list, no trusted key yet);
`installer` ok | mismatch | unnamed (the release names no hash) | unchecked.

## Scheme: `ssh-keygen -Y` (Ed25519)

OpenSSH 8.0+ is already on every box, so the check adds no system dependency (minisign would). Signatures are made
with

    ssh-keygen -Y sign -f <ed25519 key> -n wizardos-release releases/<version>.json

and verified with `ssh-keygen -Y verify -f wam/release_signers -I wizardos-release -n wizardos-release`.

`wam/release_signers` is the OpenSSH allowed-signers list, shipped inside the App Manager checkout and never fetched
from where the releases come from. One line per key, so the key can change: add the new key, release, sign with it,
then drop the old line. It holds **no key yet**: creating the real key and signing published releases (wizard-os
`tools/release_train.py`) is a later step that needs a person.

## The switch

`WIZARD_VERIFY_RELEASES` (environment, wins) or `verify_releases` in `~/.wizard-os/updates.json`, set with
`wam updates --verify-releases off|warn`:

- `off` (default): nothing is fetched, checked or written; the update runs exactly as before.
- `warn`: checks, logs a missing or bad result to `~/.wizard-os/release-signature.log` (`[signature] ...`; its own
  file because get.sh empties install.log when it starts), records it in `~/.wizard-os/release-signature.json`, in
  `wam updates` (key `signature`, also as a dry run: `wam updates`, or `--apply-if-auto --check` on an Automatic box)
  and, once the installer has finished, in `release.json` (`signature`) and `install-status.json` (the summary).
  A quick update writes those two itself. The update always carries on. Today's mirror is unsigned, so a box on
  `warn` records `signature: missing`.
- `enforce`: reserved for the next step (refuse and change nothing). Until `ENFORCE_AVAILABLE` is turned on in
  `wam/release_sig.py` it behaves like `warn`, and `wam updates` says so.

The plain `releases/` mirror and fresh installs (`curl ... | bash`, which has no checkout and so no key list yet) are
unchanged.
