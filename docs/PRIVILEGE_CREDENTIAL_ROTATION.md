# Owner-run legacy manager credential invalidation

**GitHub-only candidate. No real credentials have been retrieved or rotated.**
Docker/Postgres/RabbitMQ runtime acceptance remains a separate, admitted forum job.
This is not a declaration that M6 is proven on a real installation.

## Supported source and scope

- WAM `docker-compose.prod.yml` is a **development/local image** recipe with literal
  `postgres` / `tipi` environment overrides. It is deliberately **not** supported by
  this transaction: changing `.env` cannot override those literals.
- The installed upstream CLI recipe is
  [`runtipi/cli@1e508fec8c30acda7db67613c2b4e893952c8cf9/src/assets/docker-compose.yml`](https://github.com/runtipi/cli/blob/1e508fec8c30acda7db67613c2b4e893952c8cf9/src/assets/docker-compose.yml).
  It uses service `runtipi-db`, Postgres 14 role/database `tipi`,
  `${POSTGRES_PASSWORD}`; service `runtipi-queue`, RabbitMQ 4 user `tipi`,
  `${RABBITMQ_PASSWORD}`; and backend `runtipi` with `.env` as its env file and
  `/data/.env` bind. WAM `install.sh` adds `user-config/tipi-compose.yml` in project
  `runtipi`. The adapter validates that exact installed invocation, actual
  container labels, protected bind, fixed usernames, effective environment, and
  pins all effective noncredential Compose config before the first mutation.
- Backend `packages/backend/src/common/helpers/env-helpers.ts:79,111,155-157`
  preserves a present JWT key, writes `.env`, then loads it with override=true.
  `wam/runtipi.py:cli_token` uses the supported HS256 `sub=cli` bearer path.
- PostgreSQL `POSTGRES_PASSWORD` only initializes a fresh database. This code
  executes **ALTER ROLE** against the existing database over its local admin
  socket. The generated SQL carries a salted SCRAM verifier, not the password;
  session statement/duration/error logging is disabled before ALTER. Password
  verification uses TCP, not the trusted local socket. Old role connections are
  terminated. A custom HBA, audit plugin, role or database needs owner review.
- RabbitMQ DEFAULT_PASS only initializes an empty user database. This code uses
  the supported `rabbitmqctl change_password tipi` **stdin** form, followed by
  `close_all_user_connections`. The stdin password form and its matching
  `authenticate_user` form are present in upstream
  [4.0.9 ChangePasswordCommand](https://github.com/rabbitmq/rabbitmq-server/blob/v4.0.9/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/change_password_command.ex),
  [AuthenticateUserCommand](https://github.com/rabbitmq/rabbitmq-server/blob/v4.0.9/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/authenticate_user_command.ex),
  [Input](https://github.com/rabbitmq/rabbitmq-server/blob/v4.0.9/deps/rabbitmq_cli/lib/rabbitmq/cli/core/input.ex), and
  [CloseAllUserConnectionsCommand](https://github.com/rabbitmq/rabbitmq-server/blob/v4.0.9/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/close_all_user_connections_command.ex).

Only `JWT_SECRET`, `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD` change. The tool never
opens per-app `.env`, `app.env`, payloads or private Hermes state. Root-only backups
contain the original and replacement manager dotenv; noncredential bytes are
preserved during atomic replacement. It does not rotate app-owned secrets, the
seed, WAM tokens, owner password, or any external service secret.

## Owner consent and ordering

Use a **separate owner root SSH session**, not a process running inside the old
agent's login manager. The reviewed source must already be in the protected
migration source / manager checkout. Do not run agent-owned Python as root.

The migration order is `doctor -> remove_docker -> credentials -> complete`.
`remove_docker` terminates the old user's lingering processes before restarting
its clean user manager. Rotation additionally checks root migration evidence,
account membership, and every live selected-UID process's supplementary groups.
A root-only entry point cannot be reached through the thin agent client.

The migration pauses at `credentials` without explicit consent. For an owner who
has reviewed this consequence, the existing reviewed migration command accepts:

```sh
sudo /usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-privilege migrate --authorize-invalidation
```

This changes only the three **manager** credentials and signs out backend
sessions. No owner choice is inferred for nonstandard usernames, extra Compose
files, literal overrides, unsafe symlinks/hardlinks, duplicate/missing keys,
unsupported dotenv syntax, drift, unavailable services, or preflight auth that
does not prove an existing operator. Those cases stop **before** stop/reset and
need an explicit owner mapping/fix, not a guessed reset mechanism.

Once prepared, the protected root command can inspect/resume independently:

```sh
sudo /usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-credential-rotation status
sudo /usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-credential-rotation rotate --authorize-invalidation
# Only before password-change intent:
sudo /usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-credential-rotation abort --authorize-invalidation
```

Then repeat `wam-privilege migrate --authorize-invalidation` to finish cutover.
All paths/identities are fixed for the installed CLI; no secret is accepted as an
argument or printed in a result. The standalone CLI emits only safe metadata or
a fixed error. Service stdout/stderr is captured, never passed through on error.

## Journal and interruption contract

`/var/lib/wizard-os-migration/credentials` is root-owned 0700. Its journal, lock
and before/after backups are root-owned 0600. Every existing path component is
checked for ownership, symlinks and group/other write access. `.env` is
manager-owned 0600, under root/manager-owned non-writable anchors. Migration
relocation seals these known manager controls without changing app payload modes.
Every file publication uses unique O_EXCL temp files, fchown/fchmod before
publication, fsync(file), rename, fsync(directory). Journal metadata contains no
plaintext secret. Keep all backups private; they are recovery material, not logs.

Phases: `prepared`, `quiesced`, `db_intent`, `db_rotated`, `queue_intent`,
`queue_rotated`, `file_committed`, `restarted`, `verified`.

- Before `db_intent`, `abort` verifies both old passwords, restarts the old backend,
  then records `aborted`. It retains the protected evidence. An owner must archive
  an aborted preparation before starting a separately authorized new attempt.
- At/after **db_intent**, a DB write might have succeeded even if its acknowledgement
  was lost. **Abort and the old migration snapshot rollback are forbidden.** The
  rollback guard is before every restore side effect. Never restore the old
  `.env`/Postgres snapshot to "fix" this; that revives known leaked credentials.
- Resume stops the backend again, reads the identical saved replacement values,
  repeats both idempotent password changes (including connection revocation),
  republishes manager `.env`, and force-recreates **only the backend** with
  `--no-deps`. No DB/queue recreation or volume removal is used.
- Completion requires new Postgres and RabbitMQ passwords to authenticate and old
  passwords to be explicitly rejected (not simply connection failure), plus new
  backend bearer HTTP 200 and old-signing-key bearer HTTP 401 on the protected
  marketplace endpoint. An already verified receipt is rechecked, not blindly
  trusted. A previously completed legacy journal cannot skip the new step.

## Manual recovery (if normal resume refuses)

1. Keep root journal and both dotenv backups intact. Stop other owner/admin jobs
   that mutate the manager dotenv or Compose metadata. Keep the backend stopped.
   Do not print, paste or put any backup value on a shell command line.
2. Restore availability of the same existing Postgres/RabbitMQ services and their
   data/identities; do not initialize a fresh DB or new empty queue in their place.
   If only daemon/container availability failed, repeat the same rotate command.
3. For dotenv or effective-Compose drift, compare privately against the protected
   journal's hashes and resolve the owner change first. The tool will not discard
   a concurrent edit or guess whether old/new stores are authoritative.
4. If a backup/journal is missing or corrupt, normal automatic recovery is blocked.
   Recover its verified root-only copy from secure backup, then resume. If no such
   copy exists, stop: a reviewed owner-specific **forward** reset of both service
   passwords and the manager JWT is required. Never synthesize a phase marker or
   restore the pre-migration database/old passwords. This unsupported disaster
   case is intentionally not papered over by automatic rollback.
5. Once verified, finish migration and run its doctor from the protected manager.
   Root recovery snapshots containing leaked values must remain inaccessible to
   the old UID and must never be used to restore those credentials.

## Focused evidence and pending admission

The fast `tests/test_credential_rotation.py` suite uses explicit synthetic stores.
It tests each durable and unacknowledged-effect boundary, pre-intent abort vs
post-intent forward-only recovery, no-op DB reset rejection, drift, exact app
bytes/UID/GID/mode preservation, private ownership/mode checks, wrong UID,
symlink/hardlink and unsafe ancestor refusal, SCRAM construction, no secret argv,
and suppressed child error output. These tests **are not runtime auth proof**.

`qa/privsep-security/credential-rotation-disposable.py` is staged but NOT executed.
The parent must admit it on **forum**, in a **fresh disposable Linux/amd64 VM with
an empty dedicated Docker daemon** (not the shared forum daemon). Preload the
candidate backend image and Postgres 14 / RabbitMQ 4 images by immutable digest;
no real credentials, host bind paths, tailnet account, existing app data or
production source checkout belongs in that guest. Supply those three actual
resolved digests, never placeholder digests:

```sh
# Inside the clean disposable VM, from the exact protected candidate checkout:
sudo /usr/bin/python3 -I qa/privsep-security/credential-rotation-disposable.py \
  --authorize-disposable \
  --backend-image "$CANDIDATE_BACKEND_DIGEST" \
  --db-image "$POSTGRES14_DIGEST" \
  --queue-image "$RABBITMQ4_DIGEST"
```

The executable rig refuses a nonempty Docker daemon or reused fixture directory.
It stages only synthetic data under `/var/lib/wizard-rotation-acceptance`, exercises
real SQL/rabbitmqctl/backend requests at seven critical boundaries, retains an
**exact bearer minted before rotation** and checks its 401 afterward, checks the
new bearer 200, app bytes/UID/GID/mode, actual legacy-UID denied reads and root-only
journal ownership/modes, and scans captured service logs for plaintext synthetic
keys. It cleans down its own Compose project after each case and writes a
credential-free receipt only after all cases succeed. Parent must preserve that
receipt and destroy/stop the VM on every outcome. No claim of runtime success is
made until that job returns real output.

Still-unproven cases: real installed Compose-version label/merge behavior,
Postgres service authentication/logging under the selected images, RabbitMQ CLI
exit/status stream behavior on selected image, real backend startup after atomic
bind replacement, real live-process/session termination through the full migration,
custom HBA/audit plugins/users/Compose, host reboot at fsync/rename boundaries, and
manual disaster recovery with permanently lost root-only state.
