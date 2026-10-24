"""Scheduled off-machine backups into a restic repository: `wam backup schedule set|show|off`, `run`, `status`,
`snapshots`, `prune` (docs/BACKUP.md, "Scheduled backups").

Config:
  <wizard home>/backup.json                {schema 2, enabled, every, at, retention, verify, box_id, target}  no secrets
  <wizard home>/secrets/backup-target.json {access_key_id, secret_access_key}                              0600, S3 only
  <wizard home>/backup-status.json         last run: snapshot, bytes added, repository size, checks, peak disk, error

`target.kind`: `s3` ({endpoint, bucket, prefix, region, path_style, object_lock}), `path` ({path}) or `sftp`
({url: "sftp:user@host:/path"}). The repository password is this box's recovery key (its WZRK-… text), given to
restic only as a 0600 password file (wam/backup/restic.py). A systemd --user timer runs `wam backup run --scheduled`:
  lock → restic present (installed and checksum-checked if missing) → repository opened (or created)
       → capture streamed into `restic backup --stdin` (snapshot.py; only changed chunks upload)
       → retention: `forget --keep-daily D --keep-weekly W --keep-monthly M --prune` (defaults 7/4/6)
       → `restic check` every run, `restic check --read-data-subset=N%` weekly → status file.
A second run while one holds the lock exits at once without doing anything. S3 credentials come from
WIZARD_S3_ACCESS_KEY_ID / WIZARD_S3_SECRET_ACCESS_KEY, --s3-credentials-file or a TTY prompt, never argv.
"""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import crypto, keys as keylib, restic, snapshot
from .box import Box, human, is_test_env, log, managed_operation

CONFIG, TARGET_SECRET, STATUS = "backup.json", "backup-target.json", "backup-status.json"
UNIT = "wizard-backup"
EVERY = {"hourly": "hourly", "daily": "*-*-* {at}:00", "weekly": "Mon *-*-* {at}:00"}
RETENTION = {"daily": 7, "weekly": 4, "monthly": 6}
VERIFY = {"read_data_subset": "10%", "every_days": 7}
PRUNE_BY = ("box", "owner")


class ScheduleError(RuntimeError):
    pass


# ---------------------------------------------------------------- config

def config_path(box: Box) -> Path:
    return box.home / CONFIG


def load(box: Box) -> dict | None:
    try:
        c = json.loads(config_path(box).read_text())
        return c if isinstance(c, dict) else None
    except (OSError, ValueError):
        return None


def save(box: Box, cfg: dict) -> None:
    box.home.mkdir(parents=True, exist_ok=True)
    tmp = config_path(box).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, config_path(box))


def retention(cfg: dict | None) -> dict:
    """keep-daily/weekly/monthly; a schema-1 `keep: N` (newest N files) maps onto keep-daily N."""
    cfg = cfg or {}
    r = dict(RETENTION)
    if isinstance(cfg.get("retention"), dict):
        r.update({k: int(v) for k, v in cfg["retention"].items() if k in RETENTION and v is not None})
    elif cfg.get("keep"):
        r["daily"] = int(cfg["keep"])
    return r


def verify_cfg(cfg: dict | None) -> dict:
    v = dict(VERIFY)
    v.update({k: x for k, x in ((cfg or {}).get("verify") or {}).items() if k in VERIFY})
    return v


def credentials(box: Box) -> dict:
    try:
        return json.loads((box.home / "secrets" / TARGET_SECRET).read_text())
    except (OSError, ValueError):
        return {}


def parse_s3_url(url: str) -> dict:
    """`https://<endpoint host>/<bucket>[/<prefix>]` → target fields."""
    m = re.fullmatch(r"(https?)://([^/]+)/([^/]+)(?:/(.*))?", url.strip())
    if not m:
        raise ScheduleError("--s3 must look like https://<endpoint>/<bucket>[/<prefix>]")
    prefix = (m.group(4) or "").strip("/")
    return {"kind": "s3", "endpoint": f"{m.group(1)}://{m.group(2)}", "bucket": m.group(3),
            "prefix": prefix + "/" if prefix else ""}


def parse_sftp(url: str) -> dict:
    u = url.strip()
    if u.startswith("sftp:"):
        u = u[5:]
    if not re.fullmatch(r"(//)?[^\s:/]+(:\d+)?[:/].+", u):
        raise ScheduleError("--sftp must look like user@host:/path (the box's ssh config and keys are used)")
    return {"kind": "sftp", "url": "sftp:" + u}


def read_s3_secret(box: Box, creds_file: str | None) -> dict | None:
    if creds_file:
        text = Path(creds_file).expanduser().read_text()
        try:
            d = json.loads(text)
            return {"access_key_id": d["access_key_id"], "secret_access_key": d["secret_access_key"]}
        except (ValueError, KeyError, TypeError):
            env = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln and not ln.startswith("#"))
            ak = env.get("AWS_ACCESS_KEY_ID") or env.get("access_key_id")
            sk = env.get("AWS_SECRET_ACCESS_KEY") or env.get("secret_access_key")
            if ak and sk:
                return {"access_key_id": ak.strip(), "secret_access_key": sk.strip()}
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if len(lines) >= 2 and "=" not in lines[0]:
                return {"access_key_id": lines[0], "secret_access_key": lines[1]}
            raise ScheduleError("the credentials file needs {access_key_id, secret_access_key}, "
                                "AWS_ACCESS_KEY_ID=/AWS_SECRET_ACCESS_KEY= lines, or two lines") from None
    ak, sk = box.env.get("WIZARD_S3_ACCESS_KEY_ID"), box.env.get("WIZARD_S3_SECRET_ACCESS_KEY")
    if ak and sk:
        return {"access_key_id": ak, "secret_access_key": sk}
    if sys.stdin.isatty():
        ak = input("S3 access key id: ").strip()
        sk = getpass.getpass("S3 secret access key: ").strip()
        if ak and sk:
            return {"access_key_id": ak, "secret_access_key": sk}
    return None


_read_s3_secret = read_s3_secret   # older name


def target_from_flags(*, s3: str | None = None, path: str | None = None, sftp: str | None = None,
                      region: str | None = None, path_style: bool | None = None,
                      object_lock: bool | None = None) -> dict | None:
    if sum(bool(x) for x in (s3, path, sftp)) > 1:
        raise ScheduleError("give one of --s3, --path or --sftp")
    if s3:
        t = parse_s3_url(s3)
        t["region"] = region or "auto"
        t["path_style"] = bool(path_style)
        t["object_lock"] = bool(object_lock)
        return t
    if path:
        return {"kind": "path", "path": str(Path(path).expanduser().absolute())}
    if sftp:
        return parse_sftp(sftp)
    return None


def password(key: bytes) -> str:
    """The repository password: the recovery key's canonical text (WZRK-…), the same key that opens files."""
    return crypto.encode_recovery_key(key)


def open_repo(box: Box, target: dict, key: bytes, creds: dict | None = None, binary: str | None = None
              ) -> restic.Repo:
    if target.get("kind") == "s3" and not creds:
        creds = credentials(box)
        if not creds.get("access_key_id") or not creds.get("secret_access_key"):
            raise ScheduleError("no S3 credentials on this box: run `wam backup schedule set` again")
    return restic.Repo(box, target, password(key), creds, binary or restic.ensure(box))


def set_schedule(box: Box, *, s3: str | None = None, path: str | None = None, sftp: str | None = None,
                 region: str | None = None, path_style: bool | None = None, object_lock: bool | None = None,
                 keep: int | None = None, keep_daily: int | None = None, keep_weekly: int | None = None,
                 keep_monthly: int | None = None, prune_by: str | None = None, read_data_subset: str | None = None,
                 at: str | None = None, every: str | None = None, creds_file: str | None = None) -> dict:
    key = keylib.stored_key(box.home)
    if not key:
        raise ScheduleError("scheduled backups need this box's recovery key: run `wam backup key new` first")
    cur = load(box) or {}
    ret = retention(cur)
    for k, v in (("daily", keep_daily if keep_daily is not None else keep), ("weekly", keep_weekly),
                 ("monthly", keep_monthly)):
        if v is not None:
            ret[k] = int(v)
    ver = verify_cfg(cur)
    if read_data_subset:
        ver["read_data_subset"] = read_data_subset
    cfg = {"schema": 2, "engine": "restic", "enabled": True, "every": every or cur.get("every") or "daily",
           "at": at or cur.get("at") or "03:30", "retention": ret, "verify": ver,
           "prune_by": prune_by or cur.get("prune_by") or "box",
           "box_id": cur.get("box_id") or uuid.uuid4().hex[:12], "target": cur.get("target")}
    if cfg["every"] not in EVERY:
        raise ScheduleError("--every must be hourly, daily or weekly")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", cfg["at"]):
        raise ScheduleError("--at must be HH:MM (24 h)")
    if ret["daily"] < 1 or ret["weekly"] < 0 or ret["monthly"] < 0:
        raise ScheduleError("--keep/--keep-daily must be at least 1 (weekly and monthly 0 or more)")
    if cfg["prune_by"] not in PRUNE_BY:
        raise ScheduleError("--prune-by must be box or owner")
    if not re.fullmatch(r"\d{1,2}(\.\d+)?%|[1-9]/\d+", ver["read_data_subset"]):
        raise ScheduleError("--read-data-subset must look like 10% or 1/7")
    t = target_from_flags(s3=s3, path=path, sftp=sftp, region=region, path_style=path_style,
                          object_lock=object_lock)
    if t:
        if t["kind"] == "s3" and not t["prefix"]:
            t["prefix"] = f"wizard/{cfg['box_id']}/"
        cfg["target"] = t
    elif cfg["target"] and object_lock is not None and cfg["target"].get("kind") == "s3":
        cfg["target"]["object_lock"] = bool(object_lock)
    if not cfg["target"]:
        raise ScheduleError("say where backups go: --s3 https://<endpoint>/<bucket>[/<prefix>], --path DIR "
                            "or --sftp user@host:/path")
    creds = None
    if cfg["target"]["kind"] == "s3":
        creds = read_s3_secret(box, creds_file) or credentials(box) or None
        if not creds:
            raise ScheduleError("S3 credentials are needed: set WIZARD_S3_ACCESS_KEY_ID and "
                                "WIZARD_S3_SECRET_ACCESS_KEY or use --s3-credentials-file")
    binary = restic.ensure(box)
    with open_repo(box, cfg["target"], key, creds, binary) as repo:     # fail now, not at 03:30
        try:
            opened = repo.open_or_init()
        except restic.WrongKey:
            raise ScheduleError("this target already holds a backup repository locked with another key") from None
    if creds:
        keylib.write_private(box.home / "secrets" / TARGET_SECRET, json.dumps(creds) + "\n")
    save(box, cfg)
    units = write_units(box, cfg)
    sysd = systemctl(box, ["daemon-reload"]) and systemctl(box, ["enable", "--now", f"{UNIT}.timer"])
    return {"ok": True, **public(cfg), "units": [str(u) for u in units], "timer_enabled": bool(sysd),
            "linger": enable_linger(box), "repository": {"url": restic.repo_url(cfg["target"]), **opened},
            "restic": binary}


def public(cfg: dict | None) -> dict:
    if not cfg:
        return {"configured": False, "enabled": False}
    return {"configured": True, "enabled": bool(cfg.get("enabled")), "every": cfg.get("every"), "at": cfg.get("at"),
            "retention": retention(cfg), "verify": verify_cfg(cfg), "prune_by": cfg.get("prune_by") or "box",
            "box_id": cfg.get("box_id"), "target": cfg.get("target"), "engine": "restic"}


def show(box: Box) -> dict:
    cfg = load(box)
    return {"ok": True, **public(cfg), "credentials_stored": bool(credentials(box)),
            "key_stored": bool(keylib.stored_key(box.home)), "last": read_status(box)}


def off(box: Box) -> dict:
    cfg = load(box)
    if cfg:
        cfg["enabled"] = False
        save(box, cfg)
    sysd = systemctl(box, ["disable", "--now", f"{UNIT}.timer"])
    return {"ok": True, **public(cfg), "timer_disabled": bool(sysd)}


def enable(box: Box) -> dict:
    """`activate`: switch a copied schedule back on (keeping the new box id)."""
    cfg = load(box)
    if not cfg or not cfg.get("target"):
        return {"enabled": False}
    cfg["enabled"] = True
    save(box, cfg)
    write_units(box, cfg)
    systemctl(box, ["daemon-reload"]) and systemctl(box, ["enable", "--now", f"{UNIT}.timer"])
    enable_linger(box)
    return {"enabled": True}


def after_restore(box: Box) -> None:
    """A restored schedule is copied but off, with a new box id (two boxes never share an S3 prefix; a shared
    folder/SFTP repository keeps them apart by restic host name)."""
    cfg = load(box)
    if cfg:
        old = cfg.get("box_id")
        cfg["enabled"] = False
        cfg["box_id"] = uuid.uuid4().hex[:12]
        t = cfg.get("target") or {}
        if t.get("kind") == "s3" and old and old in (t.get("prefix") or ""):
            t["prefix"] = t["prefix"].replace(old, cfg["box_id"])
        cfg["restored_from_box"] = old
        save(box, cfg)
        systemctl(box, ["disable", "--now", f"{UNIT}.timer"])


# ---------------------------------------------------------------- systemd

def unit_dir(box: Box) -> Path:
    return Path(box.env.get("HOME") or Path.home()) / ".config" / "systemd" / "user"


def unit_texts(cfg: dict, wam: str) -> dict[str, str]:
    cal = EVERY[cfg.get("every") or "daily"].format(at=cfg.get("at") or "03:30")
    service = (f"[Unit]\nDescription=Wizard scheduled backup (restic)\nAfter=network-online.target\n"
               f"Wants=network-online.target\n\n"
               f"[Service]\nType=oneshot\nNice=10\nIOSchedulingClass=idle\nTimeoutStartSec=infinity\n"
               f"ExecStart={wam} backup run --scheduled\n")
    timer = (f"[Unit]\nDescription=Wizard scheduled backup timer\n\n[Timer]\nOnCalendar={cal}\nPersistent=true\n"
             f"RandomizedDelaySec=15min\n\n[Install]\nWantedBy=timers.target\n")
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def write_units(box: Box, cfg: dict) -> list[Path]:
    d = unit_dir(box)
    d.mkdir(parents=True, exist_ok=True)
    wam = box.env.get("WAM_BIN") or "%h/.local/bin/wam"
    out = []
    for name, text in unit_texts(cfg, wam).items():
        (d / name).write_text(text)
        out.append(d / name)
    return out


def systemctl(box: Box, args: list[str]) -> bool:
    """`systemctl --user ...`; injectable (Box.systemctl) and never run under tests."""
    if box.systemctl is not None:
        return bool(box.systemctl(args))
    if is_test_env(box.env):
        return False
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def enable_linger(box: Box) -> bool:
    """A user timer only fires while the user has a systemd instance: `loginctl enable-linger` keeps one running
    while nobody is logged in. Injectable (`box.linger`), never run under tests."""
    fn = getattr(box, "linger", None)
    if fn is not None:
        return bool(fn())
    if is_test_env(box.env):
        return False
    user = box.env.get("USER") or box.env.get("LOGNAME") or ""
    try:
        cur = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"], capture_output=True,
                             text=True, timeout=30)
        if cur.returncode == 0 and cur.stdout.strip() == "yes":
            return True
        return subprocess.run(["loginctl", "enable-linger", user], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------- status and lock

def read_status(box: Box) -> dict | None:
    try:
        return json.loads((box.home / STATUS).read_text())
    except (OSError, ValueError):
        return None


def write_status(box: Box, st: dict) -> None:
    prev = read_status(box) or {}
    if st.get("ok"):
        st["last_success_at"] = st.get("finished_at")
    else:
        st["last_success_at"] = prev.get("last_success_at")
    if not st.get("read_data") and prev.get("read_data"):
        st["read_data"] = prev["read_data"]      # the weekly full-read check is carried between runs
    tmp = box.home / (STATUS + ".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, box.home / STATUS)


class Busy(RuntimeError):
    pass


@contextmanager
def lock(box: Box):
    """One backup engine run at a time on this box (run, import, undo, activate). The lock is a flock on the Wizard
    home folder itself, so taking it creates nothing (a refused import leaves the box byte-for-byte unchanged) and
    the kernel drops it when the process dies."""
    box.home.mkdir(parents=True, exist_ok=True)
    fd = os.open(box.home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another backup or restore is running on this box") from None
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------- one run

@managed_operation
def run(box: Box, scheduled: bool = False, now: float | None = None) -> dict:
    cfg = load(box)
    if not cfg or not cfg.get("target"):
        if scheduled:
            return {"ok": True, "skipped": "no backup schedule on this box"}
        raise ScheduleError("no backup target: run `wam backup schedule set --s3 ... | --path DIR | --sftp ...` "
                            "first")
    if scheduled and not cfg.get("enabled"):
        return {"ok": True, "skipped": "scheduled backups are off"}
    try:
        with lock(box):
            return _run(box, cfg, now)
    except Busy as e:
        if scheduled:
            _note_skipped(box, str(e))
        raise


def _note_skipped(box: Box, reason: str) -> None:
    """A scheduled run that found another backup/restore running: recorded in backup-status.json (`last_skipped`)
    so status shows why there is no new snapshot, without touching the last run's own record."""
    try:
        st = read_status(box) or {}
        st["last_skipped"] = {"at": int(time.time()), "reason": reason, "scheduled": True}
        tmp = box.home / f"{STATUS}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, box.home / STATUS)
    except OSError:
        pass


def apply_retention(repo: restic.Repo, cfg: dict, host: str | None, *, owner: bool = False) -> dict:
    """forget + prune by the retention policy. On an Object Lock bucket the box prunes only fully unused packs
    (`--max-unused unlimited`: nothing is rewritten, so nothing new is locked) and a delete the storage refuses is
    expected (the owner prunes, or the lock ages out), not a failed run."""
    t = cfg.get("target") or {}
    pol = retention(cfg)
    if (cfg.get("prune_by") or "box") == "owner" and not owner:
        return {"ok": True, "by": "owner", "deferred": True, "policy": pol,
                "note": "this box does not delete; the owner runs `wam backup prune` with an owner credential"}
    locked = bool(t.get("object_lock")) and not owner
    try:
        r = repo.forget(pol, host=host, prune=True, max_unused="unlimited" if locked else None)
        return {"ok": True, "by": "owner" if owner else "box", "policy": pol, **r}
    except restic.Refused as e:
        if locked:
            return {"ok": True, "by": "box", "policy": pol, "deferred": True,
                    "note": f"the bucket refused a delete (Object Lock / policy): {e}"}
        raise


def _read_data_due(cfg: dict, prev: dict | None, now: float) -> bool:
    last = ((prev or {}).get("read_data") or {})
    days = float(verify_cfg(cfg)["every_days"])
    return not last.get("ok") or now - float(last.get("at") or 0) >= days * 86400 - 3600


def _run(box: Box, cfg: dict, now: float | None = None) -> dict:
    t0 = time.time()
    now = now or t0
    t = cfg["target"]
    prev = read_status(box)
    st: dict = {"started_at": int(t0), "target": t.get("kind"), "repository": restic.repo_url(t), "ok": False,
                "engine": "restic", "restic_version": restic.VERSION}
    host = snapshot.host_name(cfg.get("box_id") or "box")
    try:
        key = keylib.stored_key(box.home)
        if not key:
            raise ScheduleError("this box has no stored recovery key (`wam backup key new`)")
        with open_repo(box, t, key) as repo:
            st["repository_created"] = repo.open_or_init()["initialized"]
            log("capturing into the repository (only changed data is uploaded)")
            bk = snapshot.backup(box, repo, host)
            st.update({k: bk[k] for k in ("snapshot", "short_id", "data_added", "data_added_packed", "capture_bytes",
                                          "entries", "agents", "apps", "peak_extra_disk_bytes", "restic_cache_bytes")})
            st["backup_seconds"] = bk["seconds"]
            log(f"snapshot {bk['short_id']}: {human(bk['data_added'] or 0)} new of {human(bk['capture_bytes'] or 0)}")
            st["retention"] = apply_retention(repo, cfg, host)
            log("restic check")
            st["check"] = repo.check()
            if _read_data_due(cfg, prev, now):
                sub = verify_cfg(cfg)["read_data_subset"]
                log(f"restic check --read-data-subset={sub} (weekly)")
                st["read_data"] = {**repo.check(read_data_subset=sub), "subset": sub}
            stats = repo.stats_raw()
            st["repository_bytes"] = stats.get("total_size")
            st["repository_uncompressed_bytes"] = stats.get("total_uncompressed_size")
            st["snapshots"] = stats.get("snapshots_count")
        st.update({"ok": True, "verified": True, "size": human(st.get("data_added") or 0), "error": None})
    except Exception as e:  # noqa: BLE001 - every failure is reported in the status file
        st.update({"ok": False, "verified": False, "error": str(e)[:500], "kind": type(e).__name__})
    finally:
        st["finished_at"] = int(time.time())
        st["seconds"] = round(time.time() - t0, 1)
        write_status(box, st)
    return st


# ---------------------------------------------------------------- owner commands

def repo_for(box: Box, *, key: bytes, s3: str | None = None, path: str | None = None, sftp: str | None = None,
             region: str | None = None, path_style: bool | None = None, creds_file: str | None = None) -> restic.Repo:
    """The repository named by flags, else this box's schedule target (with its stored credentials)."""
    t = target_from_flags(s3=s3, path=path, sftp=sftp, region=region, path_style=path_style)
    creds = None
    if t is None:
        cfg = load(box) or {}
        t = cfg.get("target")
        if not t:
            raise ScheduleError("no backup target: give --s3/--path/--sftp or set a schedule")
        if t.get("kind") == "s3":
            creds = read_s3_secret(box, creds_file) if creds_file else credentials(box)
    elif t["kind"] == "s3":
        creds = read_s3_secret(box, creds_file) or None
    if t.get("kind") == "s3" and not creds:
        raise ScheduleError("S3 credentials are needed: --s3-credentials-file, WIZARD_S3_ACCESS_KEY_ID / "
                            "WIZARD_S3_SECRET_ACCESS_KEY, or a terminal prompt")
    return open_repo(box, t, key, creds)


def prune_now(box: Box, *, creds_file: str | None = None) -> dict:
    """`wam backup prune`: forget + prune by this box's retention, normally with an owner credential (the box's own
    may not delete on a locked bucket)."""
    cfg = load(box)
    if not cfg or not cfg.get("target"):
        raise ScheduleError("no backup schedule on this box")
    key = keylib.stored_key(box.home)
    if not key:
        raise ScheduleError("this box has no stored recovery key")
    with lock(box):
        with repo_for(box, key=key, creds_file=creds_file) as repo:
            res = apply_retention(repo, cfg, snapshot.host_name(cfg.get("box_id") or "box"), owner=True)
            res["check"] = repo.check()
    return {"ok": True, **res}


__all__ = ["set_schedule", "show", "off", "run", "enable", "after_restore", "lock", "Busy", "ScheduleError",
           "retention", "repo_for", "prune_now", "read_s3_secret"]
