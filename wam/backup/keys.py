"""Where backup keys come from, and the box's stored recovery key.

Keys and passphrases are never printed, logged or put on a command line. They come from, in order:
  --key-file FILE                 a recovery key (or, with --passphrase-file semantics, a passphrase)
  WIZARD_BACKUP_KEY               a recovery key
  WIZARD_BACKUP_PASSPHRASE        a passphrase
  ~/.wizard-os/secrets/backup-key.json   this box's recovery key (0600), used for exports and scheduled runs
  a TTY prompt (getpass)          when nothing else opens the file
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import time
from .filesystem import Path

from . import crypto
from .fmt import Keys

KEY_FILE = "backup-key.json"


def key_path(home: Path) -> Path:
    return Path(home) / "secrets" / KEY_FILE


def write_private(path: Path, text: str) -> None:
    """Write a 0600 file atomically (folder 0700), never world-readable even for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def stored_key(home: Path) -> bytes | None:
    p = key_path(home)
    try:
        rec = json.loads(p.read_text())
        return crypto.decode_recovery_key(rec["recovery_key"])
    except (OSError, ValueError, KeyError, TypeError, crypto.KeyTypo):
        return None


def store_key(home: Path, key: bytes) -> dict:
    rec = {"schema": 1, "recovery_key": crypto.encode_recovery_key(key), "key_id": crypto.key_id(key),
           "created_at": int(time.time())}
    write_private(key_path(home), json.dumps(rec, indent=2) + "\n")
    return {"key_id": rec["key_id"], "path": str(key_path(home))}


def _tty_prompt(prompt: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def parse_secret(text: str, keys: Keys) -> None:
    """A recovery key (WZRK-...) goes to the recovery list (a typo raises KeyTypo), anything else is a passphrase."""
    text = text.strip("\r\n")
    if not text.strip():
        return
    if crypto.looks_like_recovery_key(text):
        keys.recovery.append(crypto.decode_recovery_key(text))
    else:
        keys.passphrases.append(text)


def gather(home: Path, key_file: str | None = None, *, use_stored: bool = True, prompt: bool = True,
           env=os.environ) -> Keys:
    """Every key the owner made available, for opening a file."""
    keys = Keys()
    if key_file:
        parse_secret(Path(key_file).expanduser().read_text(), keys)
    if env.get("WIZARD_BACKUP_KEY"):
        keys.recovery.append(crypto.decode_recovery_key(env["WIZARD_BACKUP_KEY"]))
    if env.get("WIZARD_BACKUP_PASSPHRASE"):
        keys.passphrases.append(env["WIZARD_BACKUP_PASSPHRASE"])
    if use_stored and (k := stored_key(home)):
        keys.recovery.append(k)
    if not keys and prompt:
        got = _tty_prompt("Recovery key or passphrase: ")
        if got:
            parse_secret(got, keys)
    return keys


def passphrase_for_export(env=os.environ, prompt: bool = True) -> str | None:
    """--passphrase on export: from WIZARD_BACKUP_PASSPHRASE or a confirmed TTY prompt."""
    if env.get("WIZARD_BACKUP_PASSPHRASE"):
        return env["WIZARD_BACKUP_PASSPHRASE"]
    if not prompt:
        return None
    a = _tty_prompt("Passphrase for this file: ")
    if not a:
        return None
    if _tty_prompt("Same passphrase again: ") != a:
        raise SystemExit("the passphrases did not match; nothing was written")
    return a


def recovery_for_set(key_file: str | None, env=os.environ) -> bytes:
    """`wam backup key set`: the recovery key to store, from --key-file, WIZARD_BACKUP_KEY or a TTY prompt."""
    text = None
    if key_file:
        text = Path(key_file).expanduser().read_text()
    elif env.get("WIZARD_BACKUP_KEY"):
        text = env["WIZARD_BACKUP_KEY"]
    else:
        text = _tty_prompt("Recovery key (WZRK-...): ")
    if not text:
        raise SystemExit("no recovery key given (use --key-file, WIZARD_BACKUP_KEY or run it in a terminal)")
    return crypto.decode_recovery_key(text)
