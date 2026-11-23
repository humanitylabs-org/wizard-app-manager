# Vendored for the agent container only

`yaml/` is PyYAML 6.0.3 (pure Python, unmodified: `lib/yaml/*.py` and `LICENSE` from
`pyyaml-6.0.3.tar.gz`, sha256 `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`, MIT licence).

`wam/__init__.py` adds this folder to `sys.path` only when `import yaml` fails. That happens when the builder
bundle runs `wam` with a Hermes v0.23+ engine's Python, which ships ruamel.yaml and no PyYAML (wizard-app-manager#971).
On the box, wam always uses the system's own PyYAML.
