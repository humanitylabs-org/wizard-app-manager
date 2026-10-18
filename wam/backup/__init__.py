"""Wizard backup and move: one encrypted file that holds a whole Wizard (docs/BACKUP.md is the contract).

The same `.wizardbackup` file backs a Wizard up, restores it onto a fresh install and moves it to another machine.

Modules:
  crypto    AES-256-GCM (system libcrypto through ctypes, or `cryptography`), HKDF, scrypt, the recovery key
  fmt       the encrypted container: header, key slots, 1 MiB authenticated chunks
  bundle    the tar stream inside: member rules, checksums.json, streaming verify and staging
  keys      where keys come from (TTY, --key-file, environment, the box's stored key); never argv, never printed
  box       this box: paths, Runtipi client, agents, digests, the stock-Hermes runner
  agents    agent import rules (adapted from try-wizard wizard-import-hermes)
  capture   `wam backup export`
  restore   `wam backup verify|import|undo|activate`
  s3        a stdlib S3 client (SigV4, multipart, ranged GET, list, delete)
  schedule  `wam backup schedule|run|status`: timer units, upload, read-back, prune, status
  cli       the `wam backup ...` command line
"""

EXT = ".wizardbackup"
