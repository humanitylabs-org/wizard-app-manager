#!/usr/bin/env python3
"""Finite runtime dispatch: retain the installed CLI for every existing command.

Parent installs this adapter only after acceptance and writes its explicit local
runtime config. No source checkout, config migration, timer or settings writes.
"""
import json
import os
from pathlib import Path
import sys


def select(config, args):
    if set(config) != {'legacy_source', 'updates_source', 'python'}:
        raise ValueError('explicit legacy_source, updates_source and python required')
    key = 'updates_source' if args[:1] == ['app-updates'] else 'legacy_source'
    source, interpreter = Path(config[key]), Path(config['python'])
    if not source.is_absolute() or not interpreter.is_absolute():
        raise ValueError('runtime source and interpreter must be absolute')
    if not (source / 'wam/cli.py').is_file() or not interpreter.is_file():
        raise ValueError('configured CLI runtime is missing')
    return [str(interpreter), '-m', 'wam.cli', *args], {**os.environ, 'PYTHONPATH': str(source)}


def main():
    config_path = Path(os.environ.get('WAM_CLI_DISPATCH_CONFIG',
                                     str(Path.home() / '.wizard-os/wam-cli-dispatch.json')))
    config = json.loads(config_path.read_text())
    argv, env = select(config, sys.argv[1:])
    os.execve(argv[0], argv, env)


if __name__ == '__main__':
    main()
