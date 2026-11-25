# Signed releases (warn-only, on for everyone)

wizard-app-manager#565, step A of wizard-os#416. Code: `wam/release_sig.py`; trusted keys: `wam/release_signers`;
tests: `tests/test_release_signatures.py`.

## What it checks

Before `wam updates --apply` / Automatic uses a release's pins (full installer rerun or quick update):

1. **The release file**: `releases/<version>.json` against its detached signature `releases/<version>.json.sig`.
2. **The promotion record**, when the release has one: `releases/promotions/<version>.json` against `<...>.json.sig`.
3. **The installer**: `wam` itself downloads `https://aiwizards.com/os.sh`, hashes it (never trusting anything the
   installer prints) and compares it with the hashes the release file names, `"installer": {"os.sh": "<sha256>", "get.sh": "<sha256>"}` (aiwizards.com/os.sh serves get.sh, so
   either name matches). The installer then runs from that checked file (`bash <file> start`), so
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

## Warn-only, on for everyone

The check always runs (it shipped behind `WIZARD_VERIFY_RELEASES` and was turned on for everyone after show-and-tell;
the switch, `wam updates --verify-releases` and the `verify_releases` key in `~/.wizard-os/updates.json` are gone, and
a stored `verify_releases` value is dropped the next time `wam updates` runs). It:

- logs a missing or bad result to `~/.wizard-os/release-signature.log` (`[signature] ...`; its own file because
  get.sh empties install.log when it starts),
- records it in `~/.wizard-os/release-signature.json`, in `wam updates` (key `signature`, also as a dry run:
  `wam updates`) and, once the installer has finished, in `release.json` (`signature`) and `install-status.json` (the
  summary). A quick update writes those two itself.
- never refuses: the update always carries on. Today's mirror is unsigned, so boxes record `signature: missing`.

Refusing an unsigned or mismatched update (`enforce`) is the next step, once real releases are signed.

The plain `releases/` mirror and fresh installs (`curl ... | bash`, which has no checkout and so no key list yet) are
unchanged.
