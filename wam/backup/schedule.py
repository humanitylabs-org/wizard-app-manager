"""Scheduled off-machine backups: `wam backup schedule set|show|off`, `wam backup run`, `wam backup status`.

Config (docs/BACKUP.md, "Scheduled backups"):
  <wizard home>/backup.json                {schema, enabled, every, at, keep, box_id, target}   no secrets
  <wizard home>/secrets/backup-target.json {access_key_id, secret_access_key}                   0600, S3 only
  <wizard home>/backup-status.json         last run: ok, error, size, seconds, object, drill

`target.kind` is `s3` ({endpoint, bucket, prefix, region}) or `path` ({path}). A systemd --user timer
(~/.config/systemd/user/wizard-backup.timer + .service) runs `wam backup run --scheduled`. One run:
  lock → export into a work folder on the box (S3) or straight into the target folder (path)
       → upload (multipart for big files) → read back: size, header bytes, then the restore drill: stream the stored
         object through a full verify (decrypt + every checksum), so a backup that can't be restored is reported
       → prune to the newest `keep` → status file.
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

from . import EXT, capture, fmt, keys as keylib, restore
from .box import Box, human, is_test_env, log
from .s3 import S3, S3Error

CONFIG, TARGET_SECRET, STATUS = "backup.json", "backup-target.json", "backup-status.json"
UNIT = "wizard-backup"
EVERY = {"hourly": "hourly", "daily": "*-*-* {at}:00", "weekly": "Mon *-*-* {at}:00"}


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


def _read_s3_secret(box: Box, creds_file: str | None) -> dict | None:
    if creds_file:
        text = Path(creds_file).expanduser().read_text()
        try:
            d = json.loads(text)
            return {"access_key_id": d["access_key_id"], "secret_access_key": d["secret_access_key"]}
        except (ValueError, KeyError, TypeError):
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if len(lines) >= 2:
                return {"access_key_id": lines[0], "secret_access_key": lines[1]}
            raise ScheduleError("the credentials file needs {access_key_id, secret_access_key} or two lines") from None
    ak, sk = box.env.get("WIZARD_S3_ACCESS_KEY_ID"), box.env.get("WIZARD_S3_SECRET_ACCESS_KEY")
    if ak and sk:
        return {"access_key_id": ak, "secret_access_key": sk}
    if sys.stdin.isatty():
        ak = input("S3 access key id: ").strip()
        sk = getpass.getpass("S3 secret access key: ").strip()
        if ak and sk:
            return {"access_key_id": ak, "secret_access_key": sk}
    return None


def set_schedule(box: Box, *, s3: str | None = None, path: str | None = None, region: str | None = None,
                 keep: int | None = None, at: str | None = None, every: str | None = None,
                 creds_file: str | None = None) -> dict:
    cur = load(box) or {}
    cfg = {"schema": 1, "enabled": True, "every": every or cur.get("every") or "daily",
           "at": at or cur.get("at") or "03:30", "keep": int(keep if keep is not None else cur.get("keep") or 7),
           "box_id": cur.get("box_id") or uuid.uuid4().hex[:12], "target": cur.get("target")}
    if cfg["every"] not in EVERY:
        raise ScheduleError("--every must be hourly, daily or weekly")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", cfg["at"]):
        raise ScheduleError("--at must be HH:MM (24 h)")
    if cfg["keep"] < 1:
        raise ScheduleError("--keep must be at least 1")
    if s3 and path:
        raise ScheduleError("give --s3 or --path, not both")
    if s3:
        t = parse_s3_url(s3)
        t["region"] = region or "auto"
        if not t["prefix"]:
            t["prefix"] = f"wizard/{cfg['box_id']}/"
        cfg["target"] = t
    elif path:
        cfg["target"] = {"kind": "path", "path": str(Path(path).expanduser().absolute())}
    if not cfg["target"]:
        raise ScheduleError("say where backups go: --s3 https://<endpoint>/<bucket>[/<prefix>] or --path DIR")
    if cfg["target"]["kind"] == "s3":
        sec = _read_s3_secret(box, creds_file)
        if sec:
            keylib.write_private(box.home / "secrets" / TARGET_SECRET, json.dumps(sec) + "\n")
        elif not credentials(box):
            raise ScheduleError("S3 credentials are needed: set WIZARD_S3_ACCESS_KEY_ID and WIZARD_S3_SECRET_ACCESS_KEY "
                                "or use --s3-credentials-file")
    else:
        Path(cfg["target"]["path"]).mkdir(parents=True, exist_ok=True)
    if not keylib.stored_key(box.home):
        raise ScheduleError("scheduled backups need this box's recovery key: run `wam backup key new` first")
    save(box, cfg)
    units = write_units(box, cfg)
    sysd = systemctl(box, ["daemon-reload"]) and systemctl(box, ["enable", "--now", f"{UNIT}.timer"])
    return {"ok": True, **public(cfg), "units": [str(u) for u in units], "timer_enabled": bool(sysd)}


def public(cfg: dict | None) -> dict:
    if not cfg:
        return {"configured": False, "enabled": False}
    return {"configured": True, "enabled": bool(cfg.get("enabled")), "every": cfg.get("every"), "at": cfg.get("at"),
            "keep": cfg.get("keep"), "box_id": cfg.get("box_id"), "target": cfg.get("target")}


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
    return {"enabled": True}


def after_restore(box: Box) -> None:
    """A restored schedule is copied but off, with a new box id (two boxes never share a prefix)."""
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
    service = (f"[Unit]\nDescription=Wizard scheduled backup\nAfter=network-online.target\n\n"
               f"[Service]\nType=oneshot\nNice=10\nIOSchedulingClass=idle\nExecStart={wam} backup run --scheduled\n")
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

def _client(box: Box, t: dict) -> S3:
    sec = credentials(box)
    if not sec.get("access_key_id") or not sec.get("secret_access_key"):
        raise ScheduleError("no S3 credentials on this box: run `wam backup schedule set` again")
    return S3(t["endpoint"], t["bucket"], sec["access_key_id"], sec["secret_access_key"], t.get("region") or "auto")


def _ours(name: str) -> bool:
    return name.endswith(EXT)


_STAMP = re.compile(r"(\d{8}-\d{6})" + re.escape(EXT) + "$")


def _age_key(name: str) -> tuple[str, str]:
    """Newest last: by the file name's timestamp, so a renamed box still prunes in time order."""
    m = _STAMP.search(name)
    return (m.group(1) if m else "", name)


def run(box: Box, scheduled: bool = False) -> dict:
    cfg = load(box)
    if not cfg or not cfg.get("target"):
        if scheduled:
            return {"ok": True, "skipped": "no backup schedule on this box"}
        raise ScheduleError("no backup target: run `wam backup schedule set --s3 ... | --path DIR` first")
    if scheduled and not cfg.get("enabled"):
        return {"ok": True, "skipped": "scheduled backups are off"}
    with lock(box):
        return _run(box, cfg)


def _run(box: Box, cfg: dict) -> dict:
    t0 = time.time()
    t = cfg["target"]
    st = {"started_at": int(t0), "target": t.get("kind"), "ok": False}
    key = keylib.stored_key(box.home)
    tmp_file = None
    try:
        if not key:
            raise ScheduleError("this box has no stored recovery key (`wam backup key new`)")
        keys = fmt.Keys([key])
        name = capture.default_output(box).name
        if t["kind"] == "path":
            folder = Path(t["path"])
            folder.mkdir(parents=True, exist_ok=True)
            res = capture.export(box, folder / name, keys=keys)
            out = Path(res["file"])
            log("restore drill: full verify of the stored file")
            with open(out, "rb") as f:
                drill = restore.verify_stream(f, fmt.Keys([key]))
            stored = {"object": str(out), "bytes": out.stat().st_size}
            pruned = _prune_path(folder, int(cfg.get("keep") or 7))
        else:
            work = box.work_dir()
            tmp_file = work / name
            res = capture.export(box, tmp_file, keys=keys)
            s3 = _client(box, t)
            objkey = (t.get("prefix") or "") + name
            log(f"uploading {human(res['bytes'])}")
            s3.put_file(objkey, tmp_file, progress=log)
            head = s3.head(objkey)
            if head["bytes"] != res["bytes"]:
                raise ScheduleError(f"the uploaded object is {head['bytes']} bytes, expected {res['bytes']}")
            with open(tmp_file, "rb") as f:
                local_head = f.read(64)
            if s3.get_range(objkey, 0, len(local_head) - 1) != local_head:
                raise ScheduleError("the uploaded object's first bytes differ from the file written")
            tmp_file.unlink()
            tmp_file = None
            log("restore drill: downloading and fully verifying the uploaded object")
            with s3.open(objkey) as body:
                drill = restore.verify_stream(body, fmt.Keys([key]))
            if drill["cipher_bytes"] > head["bytes"]:
                raise ScheduleError("restore drill read more than the object holds")
            stored = {"object": objkey, "bytes": head["bytes"]}
            pruned = _prune_s3(s3, t.get("prefix") or "", int(cfg.get("keep") or 7))
        st.update({"ok": True, **stored, "size": human(stored["bytes"]), "entries": res["entries"],
                   "drill": {"ok": True, "entries": drill["entries"], "at": int(time.time())}, "pruned": pruned,
                   "error": None})
    except Exception as e:  # noqa: BLE001 - every failure is reported in the status file
        st.update({"ok": False, "error": str(e)[:500]})
    finally:
        if tmp_file is not None:
            Path(tmp_file).unlink(missing_ok=True)
        st["finished_at"] = int(time.time())
        st["seconds"] = round(time.time() - t0, 1)
        write_status(box, st)
    return st


def _prune_path(folder: Path, keep: int) -> list[str]:
    files = sorted((p for p in folder.iterdir() if p.is_file() and _ours(p.name)), key=lambda p: _age_key(p.name))
    drop = files[:-keep] if keep and len(files) > keep else []
    for p in drop:
        p.unlink()
    return [p.name for p in drop]


def _prune_s3(s3: S3, prefix: str, keep: int) -> list[str]:
    objs = sorted((o for o in s3.list(prefix) if _ours(o["key"]) and "/" not in o["key"][len(prefix):]),
                  key=lambda o: _age_key(o["key"]))
    drop = objs[:-keep] if keep and len(objs) > keep else []
    for o in drop:
        s3.delete(o["key"])
    return [o["key"] for o in drop]


__all__ = ["set_schedule", "show", "off", "run", "enable", "after_restore", "lock", "Busy", "ScheduleError",
           "S3Error"]
