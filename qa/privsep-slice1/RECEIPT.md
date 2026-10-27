# Slice 1 verification checkpoint

**Not acceptance yet. Do not merge.** Parallel development for
humanitylabs-org/wizard-os#285; do not merge until James confirms the whole
change breaks nothing. No live deployment is authorized.

## Executed lightweight checks

- Focused audit/builder/backup/bootstrap tests:
  `python -m pytest -q tests/test_fresh_privilege_audit.py tests/test_builder_fresh_docker.py tests/test_builder_bundle.py tests/test_backup_docker_sudo.py tests/test_get_sh_any_user.py`
  — **86 passed, 5 skipped**, 17.15 s.
- Installer/operator/host-prep/audit tests:
  `python -m pytest -q tests/test_install_docker.py tests/test_private_access_setup.py tests/test_prepare_named_user.py tests/test_fresh_privilege_audit.py`
  — **57 passed**, 44.69 s. This overlaps the preceding audit tests; not an aggregate total.
- Named-owner rerun/bootstrap review/rootless backup regressions:
  `python -m pytest -q tests/test_named_owner_rerun.py tests/test_get_sh_review.py tests/test_backup_hermes_patch.py`
  — **26 passed**, 30.06 s.
- Related linking/catalog/audit regressions:
  `python -m pytest -q tests/test_agent_link.py tests/test_links.py tests/test_links_safety.py tests/test_links_final_projection.py tests/test_private_catalog.py tests/test_catalog_containerd_identity.py tests/test_fresh_privilege_audit.py`
  — **56 passed**, 3.48 s (overlaps audit tests above).
- `node --test tests/runtipi-open.test.mjs` — **4 passed**, 0 failed.
- `git diff --check` and `bash -n get.sh install.sh scripts/prepare-fresh-user.sh scripts/setup-private-access.sh` — exit 0.
- Python commands used `uv run --with pytest --with pyyaml python -m pytest`.
  The bare `uv ... pytest` entry point initially could not import `wam`; running
  pytest as a Python module uses the checkout correctly. That setup error is not
  counted as a test failure or hidden as a pass.

## Still required on the admitted forum worker

The committed `run.py` performs baseline and candidate full pytest/Node suites,
then separate clean Debian 12 KVM installs. It retains command files, outputs,
JUnit counts, image checksum and per-VM cleanup status. It uses no tailnet keys,
model credentials, host mounts or live Wizard box. It also checks legacy-grant
retirement on a root-prepared rerun.

No full-suite before/after count or VM pass is claimed at this checkpoint.
A delegated builder cannot admit heavy jobs; its owning coordinator must submit
the exact staged heavy-job specification and consume its completion evidence.
Replace this checkpoint with the actual command excerpts and full counts only
after that job completes. Authenticated app-link publication cannot be proven
without tailnet login; the runner checks the configured operator separately.

See `SUDO-AUDIT.md` for every deliberately retained sudo category and the
argument-free helper's scope. This does not claim the later alongside/service
account separation or unattended maintenance scheduler is built.
