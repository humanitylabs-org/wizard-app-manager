__version__ = "0.1.0"

# #971: the builder bundle runs wam with the agent engine's Python, and Hermes v0.23+ engines ship ruamel.yaml only.
# When no PyYAML can be found at all, use the unmodified pure-Python copy that ships with wam (wam/_vendor/README.md).
# find_spec only looks: it never runs a module, so nothing here can fail an import of wam (the box keeps its own).
import importlib.util as _util
import os as _os
import sys as _sys

if _util.find_spec("yaml") is None:
    _sys.path.append(_os.path.join(_os.path.dirname(__file__), "_vendor"))
