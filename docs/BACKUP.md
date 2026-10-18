# Wizard backup and move

**Status:** v1 contract; engine `wam backup` in this repo (`wam/backup/`). Tests: `python3 -m pytest tests/test_backup_*.py`.

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
- **Scheduled backups**: off-machine into an encrypted restic repository on any S3-compatible storage (Cloudflare R2,
  AWS S3, Backblaze B2, MinIO), an SFTP server or a local/external folder; daily by default; only changes upload
  after the first run; keeps 7 daily, 4 weekly and 6 monthly snapshots; checked after every run and read back
  weekly.

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
| `agents/<id>/agent.json`, `agents/<id>/hermes-backup.zip`, `agents/<id>/folder/…` | Every agent. The home is captured with stock `hermes backup` (SQLite backup API, Hermes' own excludes). The main agent is `hermes` whether it is the box's Runtipi `hermes-agent` app (full) or the host Hermes (alongside). Extra agents also carry their small Wizard files (`runtime.key`, `gateway.key`, `service-state.json`, `package/`; not `home/` or `upload-staging/`) so a new agent's keys match its imported `.env`. Credentials stay inside the encrypted file. |
| `agents-root/…` | The Agent Service's own files beside the agent folders: agent-data grants (`.agent-grants.json`), the main agent's app choices (`.main-agent.json`), the apps already offered to agents (`.apps-seen.json`). Plain top-level files only; temporary and lock files stay. Import sets this box's aside and undo puts them back. |
| `apps/<id>/app.json`, `apps/<id>/package/…`, `apps/<id>/data.tar.gz` | Every installed app: its `apps.json` row (repo, commit, version, urn), Runtipi's status/version and install form, the exact Runtipi package installed on the box (`runtipi/apps/<store>/<id>`), and its data (`app-data/` + `app/` + `user-config/`, plus `volumes/<name>/` for each named Docker volume its compose file declares, such as an AI account login), archived as root by a one-off container of the App Manager's own image with the app stopped through Runtipi and started again after. Owners, modes, hard links and symlinks that stay inside their folder are kept; sockets and links leaving the folder are skipped and listed in the export result. (Not Runtipi's per-app backup: on a real box it silently drops data after the first socket, skips paths containing `backups`, and its restore refuses archives with symlinks, so an app with a model cache could never come back.) The Agent Service's archive leaves out its `agents/` folder (each agent travels as an agent) and its MCP token (this box's). Agent apps (`hermes-agent`, `agent-<id>`) carry no data archive: their home is the agent's `hermes-backup.zip`. |
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
   moved. Record which apps were installed before. App data belongs to the app containers (root), so each app the
   restore replaces is stopped and archived as root into `before/apps/` (a copy, not a rename), and so are named
   volumes an app the restore installs would overwrite (`before/volumes/`). The
   owner's files are set aside inside the files folder itself (`media/.wizard-restore/<stamp>/`): on a Mac it is a
   mounted Mac folder that cannot be renamed. Every step is journaled, so undo also works after a crash half way.
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

## Moving between a split and a bundled Wizard shell

Some Wizards use two system apps (`wizard-ux` for the shell and `wizard-agent-service` for agents). Older Mac
Wizards use `wizard-shell`, which does both jobs. Import keeps the destination's providers: it does not install the
other layout beside them or replace the Mac shell that its native bridge needs. The plan lists these decisions in
`kept_here` and explains them in plain-language warnings. Matching providers are restored normally; empty slots still
receive the backup's apps. An app is omitted only when all the slots it provides are already kept here. If a partial
layout would leave two Agent Services installed, import stops rather than start a second one.

Agent Service state travels between layouts: only the archive's `app-data/data/` folder is placed in the destination
provider's `data/` folder. Its package, app environment and user-config stay here, as do its agent homes and MCP token.
The separate shell's data is not copied across. The destination provider is stopped and snapshotted first, so Undo
restores its previous data. It stays stopped after import; `wam backup activate --old-is-off` starts it after checking
there is only one Agent Service provider. Import also checks before installing apps and again before starting them.

Undo records its notes in `restores/<stamp>/journal.json` as `undo_notes`. If a failed install has disappeared from
Runtipi, Undo skips that uninstall and removes only that app's leftover Compose containers and install folders. It
reports the cleanup, continues restoring the snapshots, and still checks the before/after digest.

## Scheduled backups

Scheduled runs store the capture in a **restic repository** on the target, encrypted with this box's recovery key, so
after the first run only changed data is uploaded and every kept day is a full, restorable snapshot. The
`.wizardbackup` file stays the format for export, move and hand-carried copies; both are opened by the same key.

`~/.wizard-os/backup.json` (no secrets) + `~/.wizard-os/secrets/backup-target.json` (0600, S3 keys only):

```json
{"schema": 2, "engine": "restic", "enabled": true, "every": "daily", "at": "03:30",
 "retention": {"daily": 7, "weekly": 4, "monthly": 6}, "verify": {"read_data_subset": "10%", "every_days": 7},
 "prune_by": "box", "box_id": "<12 hex>",
 "target": {"kind": "s3", "endpoint": "https://<account>.r2.cloudflarestorage.com", "bucket": "…",
            "prefix": "wizard/<box-id>/", "region": "auto", "path_style": false, "object_lock": false}}
```

**Targets** (`target.kind`): `s3` (any S3-compatible store: R2, AWS, B2, MinIO; restic `s3:<endpoint>/<bucket>/<prefix>`,
`--region`, `--path-style` for MinIO), `path` (a local or mounted external folder) and `sftp` (`user@host:/path`,
using the box's own ssh config and keys). S3 keys reach restic only as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in
restic's own process environment.

**The key.** The repository password is the recovery key's canonical text (`WZRK-…`). restic gets it only through
`RESTIC_PASSWORD_FILE`: a 0600 file in a 0700 folder made for that one command and deleted afterwards; never argv,
never the environment, never a log or the status file. A passphrase cannot open a repository; the recovery key opens
both a repository and a `.wizardbackup` file. Nothing readable leaves the box: restic encrypts and authenticates every
pack, index and snapshot (AES-256-CTR + Poly1305) before upload.

**One run** (`wam backup run`, what the systemd `--user` timer runs with `--scheduled`): lock → restic present →
open (or create) the repository → **capture**: the same entries as a file export (manifest first, agents via stock
`hermes backup`, apps' packages + data + pins (each app's data archived as for an export), the Wizard home, the files tree,
`checksums.json` last) written as one plain tar straight into `restic backup --stdin` as `/wizard.tar`, restic
host `wizard-<box-id>` → **retention** `restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune`
(`--keep-daily/--keep-weekly/--keep-monthly`; the old `keep: N` / `--keep N` maps onto keep-daily) → `restic check`
→ weekly `restic check --read-data-subset=10%` (time of the last one kept in the status file) → `restic stats --mode
raw-data` → `backup-status.json`. "Verified" means restic checked the repository structure that run and actually read
back a share of the data within the last week. One run at a time; a second run exits at once.

`backup-status.json`: `ok`, `snapshot`, `data_added` (new bytes this run, before restic's compression),
`data_added_packed`, `capture_bytes`, `repository_bytes`, `snapshots`, `retention`, `check`, `read_data`,
`peak_extra_disk_bytes`, `restic_cache_bytes`, `seconds`, `error`, `last_success_at`.

**Disk.** No copy of the capture is staged: the tar is streamed into restic. What a run writes on the box: each
agent's `hermes backup` archive (it snapshots `state.db` consistently) plus its uncompressed rewrite, and each app's
data archive (made with the app stopped, as for an export), one app at a time, deleted right after it is read. So the
peak is about the largest single app archive plus the agents' archives. On a test box whose transcriber app holds
~0.97 GB of model files the peak was 0.99 GB for a 1.08 GB capture: that one app dominates. The run samples this and
records the peak as `peak_extra_disk_bytes`; restic's local cache (`~/.cache/wizard-backup/restic`,
`restic_cache_bytes`, tens of KB on that box) comes on top. Next step if this matters: stream each app's tar straight
into the capture (one counting pass for the tar header's size, one streaming pass, app still stopped), which brings
the peak down to the agents' archives.

**Dedupe-friendly capture.** restic splits the stream into content-defined chunks (512 KiB–8 MiB, about 1 MiB on
average) and stores each chunk once, so compressed members would defeat it. The stream therefore holds agent archives
rewritten with **stored** (uncompressed) zip members (stock `hermes backup` has no stored mode; the rewrite keeps names,
dates and modes, so stock `hermes import` reads it unchanged) and app data archives **decompressed** to
`apps/<id>/data.tar` (gzip'd back into `data.tar.gz` on restore, then the file import path). restic compresses chunks
itself (zstd). Measured on the test box (1.08 GB capture, two agents, ten apps): run 1 added 573 MB (529 MB packed);
after a new owner file and a new agent session, run 2 added 12.5 MB (6.3 MB packed); bucket 529.1 MB → 535.4 MB.
Tradeoff: restic sees one big file, so each changed spot (a new file, a changed database page, the manifest) costs
about one chunk (~1 MiB) rather than only the changed bytes.

**restic itself** is the official static release (github.com/restic/restic, BSD-2-Clause, see
`THIRD_PARTY_NOTICES.md`), pinned by version and sha256 in `wam/backup/restic.py` (0.19.1, linux amd64/arm64).
`install.sh` installs it (`wam backup restic-install`); `schedule set` and `run` install it themselves when it is
missing (boxes made before), into `~/.local/bin/restic` (or `~/.local/share/wizard-backup/bin/` when the owner has
another restic there). A download whose sha256 differs is refused and nothing is installed.

**Timer.** `~/.config/systemd/user/wizard-backup.{service,timer}` (`Persistent=true`, a randomised delay). A user timer
only fires while the user has a systemd instance, so `schedule set` runs `loginctl enable-linger` for the box user
(skipped under tests).

**Restore from the repository.** `wam backup import --from-repo [SNAPSHOT|latest]` (target from `backup.json`, or
`--s3/--path/--sftp` plus `--s3-credentials-file` / `WIZARD_S3_*` / a prompt on a new box; key by the usual rules)
reads the snapshot with `restic dump` straight into the **same** verify-and-stage pass as a file import (member
rules, every checksum, free space) and then follows exactly the file path: plan, set aside, apply, paused start, Undo,
`activate`. A new box needs only the recovery key and the target's credentials. `wam backup snapshots` lists them.
After a restore the schedule is copied but **off**, with a new box id (a new S3 prefix; a shared folder repository
keeps boxes apart by restic host).

**Old snapshots a compromised box cannot destroy.** Protection must live in the storage, not on the box:

- *S3 with Object Lock* (preferred). A bucket with versioning + Object Lock and a default retention (GOVERNANCE or
  COMPLIANCE, e.g. 30 days), and a box credential that may read, write and delete objects but may **not**
  `s3:DeleteObjectVersion`, `s3:PutObjectRetention`, `s3:PutObjectLegalHold`, `s3:BypassGovernanceRetention`,
  `s3:PutBucketVersioning`, `s3:PutBucketObjectLockConfiguration`, `s3:PutLifecycleConfiguration`,
  `s3:PutBucketPolicy` or `s3:DeleteBucket`. A delete from the box then only adds a delete marker, an overwrite only
  adds a newer version, and every locked version stays recoverable by the owner. Set `--object-lock` on the schedule:
  the box's prune deletes only completely unused packs (`--max-unused unlimited`, so nothing is rewritten) and a
  delete the storage refuses is recorded as deferred, not as a failed run. Optionally also deny `s3:DeleteObject`
  except on `<prefix>/locks/*` (restic must clear its own lock files): then the box cannot even hide snapshots behind
  delete markers, but it cannot forget any either; use `--prune-by owner` and run `wam backup prune
  --s3-credentials-file <owner credential>` on a slower cadence (only data older than the lock window can go). The
  owner adds a lifecycle rule that expires noncurrent versions some days after the lock window, so deleted data
  eventually leaves the bucket.
- *What was proven* (MinIO, versioned bucket with Object Lock, box policy as above incl. the `DeleteObject` deny): with
  the box's own credential `restic forget <id> --prune`, plain and versioned deletes of snapshot, pack and key
  objects, `--bypass` deletes, recursive deletes, bucket lock/versioning/lifecycle changes were all refused.
  Overwrites are accepted as **new versions** (S3 cannot refuse a PUT the box needs for new backups), which makes
  the current repository unreadable, so the owner restores it by copying the original, still-locked versions back
  (`mc cp --version-id <v> <obj> <obj>`, or the provider's "restore previous version"); after that `restic check
  --read-data` passed and an old snapshot restored a marker file intact. With `s3:DeleteObject` allowed, restic's
  own `forget --prune` succeeds but only writes delete markers; every version stays recoverable.
- *GOVERNANCE vs COMPLIANCE*: on MinIO the box's credential could **clear** a GOVERNANCE retention even with
  `s3:PutObjectRetention` / `s3:BypassGovernanceRetention` denied (shortening was refused); the data stayed safe only
  because `s3:DeleteObjectVersion` was denied too. COMPLIANCE retention could not be cleared, shortened or
  bypassed by anyone, root included. Use COMPLIANCE where the provider offers it (and keep the IAM denies either way);
  pick its window knowing the owner cannot shorten it either.
- *Which prune route restic tolerates* (tested): **A**, box may `DeleteObject` (not versions): `restic forget
  --prune` runs normally and only writes delete markers; a compromised box can hide snapshots but every version stays
  recoverable within the lock window. **B**, box may not `DeleteObject` outside `locks/`: the box's `forget` is
  refused (`wam backup run` records it as deferred and still succeeds; restic's own lock files keep working), and
  `wam backup prune --s3-credentials-file <owner credential>` (`--prune-by owner` to stop the box trying) forgets and
  prunes; the forgotten objects get delete markers while their locked versions stay until the lock and a
  noncurrent-version lifecycle rule let them go. Recommended: B for S3 with versioning (the box cannot even hide
  snapshots), A where the owner wants the box to keep pruning itself.
- *Cloudflare R2*: R2 has no object versioning; use an R2 **bucket lock rule** (retention by age) on the repository
  prefix's `data/`, `index/`, `snapshots/`, `keys/` and `config` (not `locks/`), and an R2 API token scoped to that
  bucket with Object Read & Write. A locked object's delete or overwrite is refused outright, so set `--object-lock`
  (refused deletes are deferred) or `--prune-by owner`.
- *AWS S3*: create the bucket with Object Lock (versioning is then on), set a default retention, and give the box an
  IAM user/role whose policy allows `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetObject`, `s3:PutObject`,
  `s3:DeleteObject` on the bucket/prefix and explicitly denies the actions listed above.
- *Home disk / SFTP*: run `restic rest-server --append-only` on the storage machine and point the box at it (`rest:`
  repositories are a small addition to `target.kind`); the box can add snapshots but not delete or rewrite them.
- *A second copy*: `restic copy` into another repository (another provider or the owner's disk) is the planned way to
  keep two targets; not built yet.

## CLI
## CLI

```
wam backup export   [-o FILE] [--passphrase] [--no-files]      # also prints the size and what went in
wam backup verify   FILE                                        # decrypt + check everything, change nothing
wam backup import   FILE [--yes]                                # restore / move here (verify first, undo kept)
wam backup import   --from-repo [SNAPSHOT|latest] [--s3 URL | --path DIR | --sftp T] [--yes]   # same, from restic
wam backup snapshots [--s3 URL | --path DIR | --sftp T]           # the repository's Wizard snapshots
wam backup undo                                                 # put back what was here before the last import
wam backup activate --old-is-off                                # resume what was active at export
wam backup key      new|show-id|set [--out FILE] [--key-file F] # recovery key (shown once)
wam backup schedule set|show|off  [--s3 https://<endpoint>/<bucket>[/<prefix>] | --path DIR | --sftp user@host:/path]
                    [--keep-daily 7 --keep-weekly 4 --keep-monthly 6] [--object-lock] [--prune-by box|owner]
                    [--at HH:MM] [--every hourly|daily|weekly] [--region R] [--path-style] [--s3-credentials-file F]
wam backup run      [--scheduled]                               # one run now (--scheduled: skip when off)
wam backup prune    [--s3-credentials-file F]                    # forget + prune with an owner credential
wam backup restic-install                                       # the pinned, checksum-checked restic
wam backup status   [--json]
```

Every command prints one JSON object with `--json` (human text otherwise; progress on stderr). Keys come from
`--key-file`, `WIZARD_BACKUP_KEY` / `WIZARD_BACKUP_PASSPHRASE`, this box's stored key, or a TTY prompt; S3 keys
from `WIZARD_S3_ACCESS_KEY_ID` / `WIZARD_S3_SECRET_ACCESS_KEY`, `--s3-credentials-file` or a TTY prompt.

## Engine notes (v1)

Choices the contract left open, as built in `wam/backup/`:

- **Recovery slot**: wrap key = HKDF-SHA256(recovery key, salt = slot salt, info = "wizard-backup slot").
- **Per-machine** (never captured, never replaced): `api-token`, `mcp-token`, `runtipi-operator.json`,
  `agent-runtime/`, `agent-ssh/` (the agent's terminal key for this box on a Mac), plus `alongside.json`, `linked.json`,
  `app-manager.json`, `github-token`, `host-dashboard.env`
  (this box's mode, Hermes links, App Manager config and logins). Regenerated: `apps.json`, `skills/`,
  `system-skills/`, `cache/`, `dev/`, `staged-apps/`. Agent homes keep their own `bin/`, `logs/`, `runtime/`,
  caches, locks, the Hermes code checkout and Wizard's plugins (rules adapted from try-wizard
  `wizard-import-hermes`).
- **Travels in `wizard-os/`**: `secrets/` (per-app passwords, `backup-key.json`, `backup-target.json`) and
  `backup.json`. Entries of `secrets/` and `app-tokens/` for apps the backup doesn't carry stay on the box.
- **Apps** stopped at export stay stopped after import; apps the backup doesn't carry are left alone. An app this
  box already has gets the bundle's `app-data/` and package but keeps **this box's** `user-config/` (wam writes the
  box's wiring there: alongside overrides, trusted origins, ports); it is left in place when app data is put back. An app the restore installs gets the bundle's `user-config/`. Runtipi's uninstall keeps
  `user-config/<store>/<id>`, so undo removes the one the restore brought.
- **App archives**: safe relative names under `app-data/`, `app/`, `user-config/` and `volumes/<name>/`; files,
  folders, hard links to earlier files, and relative symlinks that resolve inside the root they live in (a model
  cache's `snapshots/<rev>/x -> ../../blobs/<sha>`), nothing below a symlink. Devices, FIFOs and anything else are
  refused at verify. Putting back extracts as root into a staging folder beside the app's data, carries over what
  this box keeps (the Agent Service's `agents/` and MCP token), then swaps folders with renames.
- **Agent wiring kept** on import: `terminal`, `mcp_servers.wizard`, the wizard `skills.external_dirs`, Wizard's
  plugins, `platforms.api_server` and the `.env` keys `API_SERVER_*`, `WIZARD_*`, `HERMES_DASHBOARD*` of this box's
  agent. A new extra agent keeps its imported ones (they match its `runtime.key`).
- **Undo diagnostics**: each new import keeps private `digest-before.json` / `digest-after.json` fingerprints
  at the stopped comparison points. `undo --json` reports `differences.paths` (first 50 relative paths with
  `missing`, `extra`, `content`, `mode`, or `owner` kinds), `total` and `truncated`; never file bodies.
  New digests include uid/gid. Journals from older versions retain their original owner-insensitive comparison.
- **What undo promises to keep**: everything the digest covers, which leaves out each agent home's per-machine
  entries (`logs/`, `backups/`, `runtime/`, caches, `*.pid`, SQLite `-wal`/`-shm`, `gateway_state.json`; the same
  list import never captures, moves or overwrites), for container agents in app-data as for host agents. The agent
  rewrites them whenever it runs, the import's own run included. Undo stops every agent app in any live state
  (Runtipi says `starting` until a health check passes) and removes a new agent's folder only after its app is
  uninstalled, so a container's restart policy cannot re-create the home.
- **Lock**: a `flock` on the Wizard home folder, so taking it creates nothing.
- **Files root**: Runtipi's `media/` (or `RUNTIPI_MEDIA_PATH/media`; `WIZARD_FILES_ROOT` overrides).

Real-box checks that the fakes in `tests/test_backup_*.py` cannot prove: archiving and putting back a running
app's data as root (files owned by container users), the agent image's paths
(`/opt/hermes/.venv/bin/{hermes,python}`), `docker run -u <home owner>` writing into the agent home, stock
`hermes backup`/`import --force` on a live home, user-level systemd timers (needs `loginctl enable-linger` to run
while logged out), a real S3/R2 bucket, and a Mac VM where `media/` is a mounted Mac folder.
