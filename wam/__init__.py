__version__ = "0.1.0"

try:   # PyYAML from the Python that runs wam (always there on the box)
    import yaml as _yaml  # noqa: F401
except ModuleNotFoundError:
    # #971: the builder bundle runs wam with the agent engine's Python, and Hermes v0.23+ engines ship ruamel.yaml
    # only. Fall back to the unmodified pure-Python PyYAML copy that ships with wam (wam/_vendor/README.md).
    import os as _os
    import sys as _sys
    _sys.path.append(_os.path.join(_os.path.dirname(__file__), "_vendor"))
