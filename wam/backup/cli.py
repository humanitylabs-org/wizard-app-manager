"""`wam backup ...`: the command line for backup, restore, move and scheduled backups (docs/BACKUP.md).

Every command prints one JSON object with --json and short human text otherwise; progress goes to stderr.
Exit status 0 = done (or a scheduled run with nothing to do), 1 = refused or failed, 2 = usage, 3 = import partly
done: some apps were not restored because the backup is from a newer release than this box offers (never downgraded).
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from . import capture, crypto, fmt, keys as keylib, restic, restore, schedule, snapshot
from .box import Box, HermesError, RuntipiError, SpaceError, human
from .crypto import BackupCryptoError

USAGE = """wam backup export   [-o FILE] [--passphrase] [--no-files]
wam backup verify   FILE
wam backup import   FILE [--yes]
wam backup import   --from-repo [SNAPSHOT|latest] [--s3 URL | --path DIR | --sftp user@host:/path] [--yes]
wam backup snapshots [--s3 URL | --path DIR | --sftp ...]
wam backup undo
wam backup activate --old-is-off
wam backup key      new|show-id|set
wam backup schedule set|show|off  [--s3 https://<endpoint>/<bucket>[/<prefix>] | --path DIR | --sftp user@host:/path]
                    [--keep-daily 7 --keep-weekly 4 --keep-monthly 6] [--object-lock] [--at HH:MM] [--every daily]
wam backup run      [--scheduled]
wam backup prune    [--s3-credentials-file FILE]      (owner credential, for locked buckets)
wam backup status
wam backup restic-install"""


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wam backup", description="Back up, restore or move this Wizard.",
                                 usage=USAGE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--json", action="store_true", help="print one JSON object")
        return p
    e = add("export")
    e.add_argument("-o", "--output")
    e.add_argument("--passphrase", action="store_true", help="also lock the file with a passphrase (asked for, or "
                                                             "WIZARD_BACKUP_PASSPHRASE)")
    e.add_argument("--no-files", action="store_true", help="leave the owner's files out")
    e.add_argument("--key-file", help="a recovery key to lock the file with (instead of / besides this box's)")
    v = add("verify")
    v.add_argument("file")
    v.add_argument("--key-file")
    def target_flags(p):
        p.add_argument("--s3", help="https://<endpoint>/<bucket>[/<prefix>]")
        p.add_argument("--path", help="a local or mounted folder holding the repository")
        p.add_argument("--sftp", help="user@host:/path (the box's ssh config and keys)")
        p.add_argument("--region")
        p.add_argument("--path-style", action="store_true", default=None, help="S3 path-style bucket URLs (MinIO)")
        p.add_argument("--s3-credentials-file")
    i = add("import")
    i.add_argument("file", nargs="?")
    i.add_argument("--from-repo", nargs="?", const="latest", metavar="SNAPSHOT",
                   help="restore a snapshot (default latest) from the backup repository instead of a file")
    i.add_argument("--yes", action="store_true")
    i.add_argument("--key-file")
    target_flags(i)
    sn = add("snapshots")
    sn.add_argument("--key-file")
    target_flags(sn)
    add("undo")
    a = add("activate")
    a.add_argument("--old-is-off", action="store_true")
    k = add("key")
    k.add_argument("action", choices=["new", "show-id", "set"])
    k.add_argument("--key-file", help="set: read the recovery key from this file")
    k.add_argument("--out", help="new: write the new key to this file (0600) instead of showing it")
    k.add_argument("--force", action="store_true", help="new/set: replace this box's stored key")
    s = add("schedule")
    s.add_argument("action", choices=["set", "show", "off"])
    target_flags(s)
    s.add_argument("--keep", type=int, help="same as --keep-daily")
    s.add_argument("--keep-daily", type=int)
    s.add_argument("--keep-weekly", type=int)
    s.add_argument("--keep-monthly", type=int)
    s.add_argument("--object-lock", action="store_true", default=None,
                   help="the bucket has Object Lock: the box never needs to delete locked data")
    s.add_argument("--prune-by", choices=list(schedule.PRUNE_BY))
    s.add_argument("--read-data-subset", help="weekly full-read check size (default 10%%)")
    s.add_argument("--at")
    s.add_argument("--every", choices=sorted(schedule.EVERY))
    r = add("run")
    r.add_argument("--scheduled", action="store_true", help="what the timer runs: does nothing when the schedule is off")
    pr = add("prune")
    pr.add_argument("--s3-credentials-file", help="an owner credential allowed to delete")
    add("status")
    add("restic-install")
    return ap


def _confirm(plan: dict, manifest: dict) -> bool:
    if not sys.stdin.isatty():
        return False
    print(_plan_text(plan), file=sys.stderr)
    try:
        return input("Replace this Wizard with the backup? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _plan_text(p: dict) -> str:
    lines = ["This restore will:"]
    if p.get("install"):
        lines.append("  install: " + ", ".join(p["install"]))
    if p.get("replace"):
        lines.append("  replace the data of: " + ", ".join(p["replace"]))
    if p.get("leave"):
        lines.append("  leave as they are: " + ", ".join(p["leave"]))
    for x in p.get("refused") or []:
        lines.append(f"  NOT restore: {x['message']}")
    lines.append("  import agents: " + ", ".join(p.get("agents") or []))
    lines += [f"  note: {w}" for w in p.get("warnings") or []]
    lines.append("The current Wizard is set aside; `wam backup undo` puts it back.")
    return "\n".join(lines)


def _human(cmd: str, res: dict) -> str:
    if cmd == "import" and res.get("partial"):
        return ("partly restored (" + str(res["stamp"]) + "):\n" + "\n".join("  " + x for x in res["not_restored"])
                + "\nEverything else is restored. Messaging is off and scheduled jobs are paused until you run "
                  "`wam backup activate --old-is-off`. `wam backup undo` puts the previous Wizard back.")
    if not res.get("ok", True):
        return "error: " + str(res.get("error"))
    if res.get("skipped"):
        return str(res["skipped"])
    if cmd == "export":
        f = res["files"]
        return (f"wrote {res['file']} ({res['size']}): {len(res['agents'])} agents, {len(res['apps'])} apps, "
                f"{f['count']} files ({human(f['bytes'])})" + ("" if f["included"] else " [files left out]")
                + f"\nopens with recovery key {', '.join(res['key_ids']) or '-'}"
                + (" or the passphrase" if "passphrase" in res["slots"] else ""))
    if cmd == "verify":
        m = res["manifest"]
        return (f"OK: {res['entries']} entries checked ({human(res['content_bytes'])}), made "
                f"{m.get('created_at')} on a {(m.get('source') or {}).get('mode')} install\n" + _plan_text(res["plan"]))
    if cmd == "import":
        return (f"restored ({res['stamp']}). Messaging is off and scheduled jobs are paused until you run "
                f"`wam backup activate --old-is-off`. `wam backup undo` puts the previous Wizard back.")
    if cmd == "undo":
        return "the previous Wizard is back" + ("" if res["digest_match"] else
                                                f" (differences: {', '.join(res['mismatch'])})")
    if cmd == "activate":
        return "this is now the active Wizard: " + json.dumps(res["agents"])
    if cmd == "run":
        return (f"snapshot {res.get('short_id')}: {human(res.get('data_added') or 0)} new of "
                f"{human(res.get('capture_bytes') or 0)}; repository {human(res.get('repository_bytes') or 0)}; "
                f"restic check passed")
    if cmd == "snapshots":
        return "\n".join(f"{s['short_id']}  {s['time'][:19]}  {s['host']}  {human(s.get('capture_bytes') or 0)}"
                         for s in res["snapshots"]) or "no snapshots"
    return json.dumps(res, indent=2)


# `import` restored everything except the apps it refused (a backup newer than this box's release, wizard-os#91)
PARTIAL = 3


def main(argv: list[str] | None = None, box: Box | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    args = _parser().parse_args(argv)
    box = box or Box()
    try:
        res = _dispatch(args, box)
        code = 0 if res.get("ok", True) else (PARTIAL if res.get("partial") else 1)
    except schedule.Busy as e:
        res, code = ({"ok": True, "skipped": str(e)}, 0) if args.cmd == "run" else ({"ok": False, "error": str(e)}, 1)
    except (BackupCryptoError, restore.RestoreError, capture.ExportError, SpaceError, schedule.ScheduleError,
            restic.ResticError, RuntipiError, HermesError, OSError, ValueError) as e:
        res, code = {"ok": False, "error": str(e), "kind": type(e).__name__}, 1
    if args.json:
        print(json.dumps(res, sort_keys=True))
    else:
        print(_human(args.cmd, res), file=sys.stdout if code == 0 else sys.stderr)
    return code


def _keys_to_open(box: Box, key_file: str | None, path: Path) -> fmt.Keys:
    """Every key given, plus this box's own. In a terminal, when none of them opens the file (a backup from another
    box), ask for its recovery key or passphrase."""
    ks = keylib.gather(box.home, key_file, env=box.env, prompt=False)
    if sys.stdin.isatty():
        try:
            with open(path, "rb") as f:
                header, signed, mac = fmt.read_header(f)
                fmt.unlock(header, signed, mac, ks)
        except fmt.WrongKey:
            got = keylib._tty_prompt("Recovery key or passphrase for this file: ")
            if got:
                keylib.parse_secret(got, ks)
    if not ks:
        raise BackupCryptoError("no key given: use --key-file, WIZARD_BACKUP_KEY / WIZARD_BACKUP_PASSPHRASE, or run "
                                "it in a terminal to be asked")
    return ks


def _repo_flags(args) -> dict:
    return {"s3": args.s3, "path": args.path, "sftp": args.sftp, "region": args.region,
            "path_style": args.path_style, "creds_file": args.s3_credentials_file}


@contextmanager
def _open_repo(box: Box, args):
    """The repository named by flags (or this box's schedule), opened with the first recovery key that works:
    --key-file, WIZARD_BACKUP_KEY, this box's stored key, then (in a terminal) a prompt. The repository password
    is a recovery key; a passphrase cannot open it."""
    ks = keylib.gather(box.home, args.key_file, env=box.env, prompt=False)
    cands = list(dict.fromkeys(ks.recovery))
    tried_prompt = False
    while True:
        for k in cands:
            repo = schedule.repo_for(box, key=k, **_repo_flags(args))
            with repo:
                try:
                    repo.run(["cat", "config"], what="open", timeout=600)
                except restic.WrongKey:
                    continue
                yield repo
                return
        if tried_prompt or not sys.stdin.isatty():
            break
        tried_prompt = True
        got = keylib._tty_prompt("Recovery key for the backup repository (WZRK-...): ")
        if not got or not crypto.looks_like_recovery_key(got):
            break
        cands = [crypto.decode_recovery_key(got)]
    raise BackupCryptoError("the backup repository does not open with any recovery key given (--key-file, "
                            "WIZARD_BACKUP_KEY, this box's key)" if cands or ks.recovery else
                            "no recovery key given: use --key-file or WIZARD_BACKUP_KEY")


def _dispatch(args, box: Box) -> dict:
    c = args.cmd
    if c == "import" and args.from_repo:
        if args.file:
            raise ValueError("give a FILE or --from-repo, not both")
        with schedule.lock(box), _open_repo(box, args) as repo:
            return snapshot.import_from_repo(box, repo, args.from_repo, yes=args.yes, confirm=_confirm)
    if c == "import" and not args.file:
        raise ValueError("give a backup FILE or --from-repo [SNAPSHOT]")
    if c == "snapshots":
        with _open_repo(box, args) as repo:
            return {"ok": True, "repository": repo.url, "snapshots": snapshot.list_snapshots(repo)}
    if c == "prune":
        return schedule.prune_now(box, creds_file=args.s3_credentials_file)
    if c == "restic-install":
        found = restic.find(box)
        return {"ok": True, "version": restic.VERSION, "path": found, "already": True} if found else \
            restic.install(box)
    if c == "export":
        pw = keylib.passphrase_for_export(box.env) if args.passphrase else None
        if args.passphrase and not pw:
            raise BackupCryptoError("--passphrase: none given (WIZARD_BACKUP_PASSPHRASE or a terminal prompt)")
        return capture.export(box, Path(args.output) if args.output else None, passphrase=pw,
                              include_files=not args.no_files, key_file=args.key_file)
    if c == "verify":
        return restore.verify(box, Path(args.file), _keys_to_open(box, args.key_file, Path(args.file)))
    if c == "import":
        ks = _keys_to_open(box, args.key_file, Path(args.file))
        with schedule.lock(box):
            return restore.import_bundle(box, Path(args.file), ks, yes=args.yes, confirm=_confirm)
    if c == "undo":
        with schedule.lock(box):
            return restore.undo(box)
    if c == "activate":
        with schedule.lock(box):
            return restore.activate(box, args.old_is_off)
    if c == "key":
        return _key(args, box)
    if c == "schedule":
        if args.action == "set":
            return schedule.set_schedule(box, s3=args.s3, path=args.path, sftp=args.sftp, region=args.region,
                                         path_style=args.path_style, object_lock=args.object_lock, keep=args.keep,
                                         keep_daily=args.keep_daily, keep_weekly=args.keep_weekly,
                                         keep_monthly=args.keep_monthly, prune_by=args.prune_by,
                                         read_data_subset=args.read_data_subset, at=args.at, every=args.every,
                                         creds_file=args.s3_credentials_file)
        return schedule.show(box) if args.action == "show" else schedule.off(box)
    if c == "run":
        return schedule.run(box, scheduled=args.scheduled)
    if c == "status":
        sh = schedule.show(box)
        try:
            _, j = restore.last_restore(box)
            sh["last_restore"] = {"stamp": j.get("stamp"), "status": j.get("status"),
                                  "activated": bool(j.get("activated_at")),
                                  "undo_failed": (bool(j.get("undo_error")) or j.get("undo_digest_match") is False)
                                                 and j.get("status") != "undone",
                                  "undo_digest_match": j.get("undo_digest_match")}
        except restore.RestoreError:
            sh["last_restore"] = None
        return sh
    raise ValueError(f"unknown command {c}")


def _key(args, box: Box) -> dict:
    have = keylib.stored_key(box.home)
    if args.action == "show-id":
        return {"ok": True, "stored": bool(have), "key_id": crypto.key_id(have) if have else None}
    if have and not args.force:
        raise BackupCryptoError(f"this box already has recovery key {crypto.key_id(have)}; pass --force to replace it "
                                "(backups made with the old key still need the old key)")
    if args.action == "set":
        k = keylib.recovery_for_set(args.key_file, box.env)
        return {"ok": True, **keylib.store_key(box.home, k)}
    k = crypto.new_recovery_key()
    rec = keylib.store_key(box.home, k)
    text = crypto.encode_recovery_key(k)
    if args.out:
        keylib.write_private(Path(args.out).expanduser(), text + "\n")
        return {"ok": True, **rec, "written_to": str(Path(args.out).expanduser())}
    # The one time the key is shown: the owner must save it now (password manager or printed).
    if args.json:
        return {"ok": True, **rec, "recovery_key": text, "shown_once": True}
    print("Your Wizard recovery key (shown once; save it in a password manager or print it):\n\n  " + text + "\n",
          file=sys.stderr if not sys.stdout.isatty() else sys.stdout)
    return {"ok": True, **rec}
