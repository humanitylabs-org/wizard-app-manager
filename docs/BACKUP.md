# Wizard backup and move

**Status:** v1 contract, being built (try-wizard issues #3 and #7). Engine: `wam backup` in this repo.

One encrypted file holds a whole Wizard. The same file backs a Wizard up, restores it onto a fresh install, and moves
it to another machine (VPS ↔ Mac app ↔ home machine). Every Wizard host runs the same Linux stack (the Mac app's VM is
provisioned by the same `os.sh`), so one engine serves all of them; the Mac app only carries the file in and out.

## What the owner sees

- **Back up now / Move this Wizard**: produces `<name>-<YYYYMMDD-HHMMSS>.wizardbackup`. On a Mac: menu
  **Wizard → Back Up This Wizard…** (save panel). On Linux/VPS: `wam backup export` (the Settings panel calls the same).
- **Restore or move a Wizard here**: pick the file, enter the key. The whole file is checked first; nothing changes if
  it is damaged, cut short or the key is wrong. The current Wizard is set aside so **Undo last restore** puts it back.
- **Paused until you switch over**: a restored Wizard arrives with every agent's scheduled jobs paused, messaging
  (Telegram, Slack, ...) off and scheduled backups off, so it never runs alongside the original.
  **Make this my active Wizard** turns back on exactly what was on at export, after the owner confirms the old one
  is off.
- **Scheduled backups**: off-machine to any S3-compatible storage (Cloudflare R2, AWS S3, Backblaze B2, MinIO) or a
  local/external folder; daily by default; keeps the newest N (default 7); every upload is read back and verified.

## The key

The owner holds the key; the box never sends anything readable off the machine.

- **Recovery key** (default): 32 random bytes shown once as `WZRK-XXXX-XXXX-…` (base32, with a checksum so a typo is
  reported as a typo, not as a wrong key). The owner saves it (password manager / printed). Scheduled backups need it
  on the box: it is stored 0600 in `~/.wizard-os/secrets/backup-key.json` (the box already holds the data in clear).
- **Passphrase** (optional, e.g. for a one-off move): scrypt-derived.
- A file can carry both slots; either opens it. Keys and passphrases are never printed, logged or put on a command
  line; the CLI reads them from a TTY prompt, `--key-file`, or the `WIZARD_BACKUP_KEY` / `WIZARD_BACKUP_PASSPHRASE`
  environment for automation.

## File format v1

```
magic        b"WIZBAK\x00\x01"                       8 bytes
header_len   uint32 big-endian
header       UTF-8 JSON (below)
header_mac   32 bytes HMAC-SHA256(HKDF(file_key, "wizard-backup header"), magic|header_len|header)
chunks       AES-256-GCM, 1 MiB plaintext each; nonce = nonce_prefix(7) | counter uint32 BE | last(1)
             key = HKDF(file_key, salt=nonce_prefix, info="wizard-backup payload")
             the final chunk has last=1 (it may be empty); no bytes may follow it
```

Header (deliberately says nothing about the contents):

```json
{"format": 1, "cipher": "aes-256-gcm-stream", "chunk": 1048576, "nonce_prefix": "<b64>",
 "slots": [{"kind": "recovery", "key_id": "<first 8 hex of sha256(HKDF(key,'id'))>", "salt": "<b64>", "nonce": "<b64>", "wrapped": "<b64>"},
           {"kind": "passphrase", "kdf": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": "<b64>", "nonce": "<b64>", "wrapped": "<b64>"}]}
```

`file_key` is 32 random bytes per file, wrapped per slot with AES-256-GCM (AAD = magic). A wrong key fails the unwrap;
a changed header fails the MAC; a changed, reordered, dropped or appended chunk fails GCM or the last-chunk rule.

Crypto: AES-256-GCM from the system OpenSSL `libcrypto` (always present: Python's `ssl` links it) through `ctypes`, or
`cryptography` when importable; scrypt from `hashlib`. No new dependency.

## Inside: a tar stream

| Entry | What |
|---|---|
| `manifest.json` (first) | format, created_at, source (`arch`, `os`, `mode`: `full` / `alongside`, Wizard versions: wam commit, Runtipi version, Agent Service/shell version), `agents[]`, `apps[]`, `files` totals, `active` (which cron jobs, messaging platforms and backup schedule were on). No secrets. |
| `wizard-os/…` | The Wizard home (`~/.wizard-os`): settings, launcher, subjects, routes, app bindings, grants, devices, links/aliases, app secrets. Excluded: caches, `*.bak*`, regenerated `skills/` and `apps.json`, agent homes (captured below), and per-machine identity (`api-token`, `mcp-token`, `runtipi-operator.json`, `agent-runtime/`). |
| `agents/<id>/agent.json`, `agents/<id>/hermes-backup.zip` | Every agent. The home is captured with stock `hermes backup` (SQLite backup API, Hermes' own excludes). The main agent is `hermes` whether it is the box's Runtipi `hermes-agent` app (full) or the host Hermes (alongside). Credentials stay inside the encrypted file. |
| `apps/<id>/app.json`, `apps/<id>/package/…`, `apps/<id>/data.tar` | Every installed app: its `apps.json` row (repo, commit, version, urn), the exact Runtipi package installed on the box (`runtipi/apps/<store>/<id>`), and its data (`runtipi/app-data/<store>/<id>` + `user-config/<store>/<id>`), captured with the app stopped. |
| `files/…` | The owner's files (Runtipi `media/`), one opaque tree. No layout is assumed. |
| `checksums.json` (last) | sha256 and size of every entry, entry count, total bytes. |

Tar members must be regular files or directories with relative, `..`-free names; links, devices and FIFOs are
refused.

## Import (restore / move)

1. **Unlock**: read header, unwrap with the key. Wrong key or bad MAC → refused; nothing touched.
2. **Verify and stage**: decrypt the whole stream into a staging folder on the same filesystem as each destination,
   checking members, hashing every file and comparing with `checksums.json`; require the final chunk. Check free space
   from the manifest first. Any failure → staging deleted, refused; the live Wizard was never touched.
3. **Plan**: compare manifest with this box (mode, installed apps, image architectures). Report what will change.
4. **Set aside** (undo snapshot): stop the affected apps and agents and move their current data aside
   (`rename`, same filesystem, exact and instant) into `restores/<stamp>/before/`, with a digest of everything
   moved. Record which apps were installed before.
5. **Apply**: install missing apps from the bundled packages at the same versions; swap app data in; import each agent
   (main agent into this box's agent home, keeping this box's Wizard wiring, as "Bring My Hermes into Wizard" does);
   swap the Wizard home items and files in. Keep this box's own machine identity (tokens, tailnet name).
6. **One active copy**: every agent's messaging off (tokens kept) and cron paused; scheduled backups off. The manifest's
   `active` record is kept so **Make this my active Wizard** (`wam backup activate`) restores exactly that.
7. **Start and check**: regenerate `apps.json` and skills, republish private links on this box's tailnet, start apps
   and agents, health-check. Write `restores/<stamp>/receipt.json`.

**Undo** (`wam backup undo`): stop, move the restored data aside, move `before/` back, uninstall the apps the restore
installed, start, and compare the digest with the one taken before the restore.

The old machine is never changed by export: it stays the rollback copy. The new one runs nothing on its own until
the owner switches over.

## Scheduled backups

`~/.wizard-os/backup.json` (no secrets) + `~/.wizard-os/secrets/backup-target.json` (0600, S3 keys):

```json
{"schema": 1, "enabled": true, "every": "daily", "at": "03:30", "keep": 7,
 "target": {"kind": "s3", "endpoint": "https://<account>.r2.cloudflarestorage.com", "bucket": "…", "prefix": "wizard/<box-id>/", "region": "auto"}}
```

`target.kind` is `s3` or `path`. A systemd timer runs `wam backup run`: export → upload (S3 multipart, SigV4, stdlib
only) → read back and verify the uploaded object's header and size → prune to the newest `keep` → write
`backup-status.json` (last success, size, duration, error). One run at a time (lock). After a restore the schedule is
copied but **off**, and the box id is new, so two boxes never write to the same prefix.

## CLI

```
wam backup export   [-o FILE] [--passphrase] [--no-files]      # also prints the size and what went in
wam backup verify   FILE                                        # decrypt + check everything, change nothing
wam backup import   FILE [--yes]                                # restore / move here (verify first, undo kept)
wam backup undo                                                 # put back what was here before the last import
wam backup activate --old-is-off                                # resume what was active at export
wam backup key      new|show-id|set                             # recovery key (shown once)
wam backup schedule set|show|off  [--s3 … | --path DIR] [--keep N] [--at HH:MM]
wam backup run                                                  # one scheduled run now
wam backup status   [--json]
```

Every command prints one JSON object with `--json`.
