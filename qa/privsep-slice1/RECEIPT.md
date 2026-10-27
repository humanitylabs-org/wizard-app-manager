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

## Failure analysis after the first forum run

Evidence directory: `/srv/work/privsep/slice1/20261027T152731Z`.

- `candidate-install.log:30-47`: upstream Runtipi copied files and generated
  `.env`, then Docker Hub returned HTTP 429 for `rabbitmq:4-alpine`. The upstream
  CLI printed failure but its setup script returned success, so preparation
  continued (`:49`). This was **not** a Docker-group or sudo denial.
- `candidate-install.log:282,307,322-323,379-415`: later Compose calls pulled the
  images and brought up healthy DB/queue, the manager and proxy. Therefore the
  eventual API 404 cannot be described merely as an image still missing.
- Read-only QCOW2 inspection (no guest boot, mount or disk writes), retained as
  `offline-config-inspection.log`, establishes the failure mechanism. Candidate
  proxy StartedAt is **16:00:37.499972477Z** (`:369`); its static Traefik config
  was written at **16:00:39.923519Z** (`:228`). Proxy logs report
  **EntryPoint doesn't exist**, including dashboard `web`, followed by
  **No valid entryPoint for this router** (`:370-383`). Both guests' saved
  `.env` values are loopback/8480/8443 and both eventually have the same static
  entrypoints (`:3-39`, `:189-225`). This rules out an env-file/port regression.
- `install.sh` bypassed the proxy's healthy-manager dependency with `--no-deps`
  while also starting the manager without `--wait`. That pre-existing cold-start
  race was exposed by the failed upstream first start; moving host preparation
  to root did not change the env values. The proxy loaded no static entrypoints
  and did not reload them when the backend later wrote its config. Subsequent
  internal manager health was insufficient (`candidate-containers.log:3`), and
  its proxy-facing API/health still failed (`candidate-install.log:440,455`).
- The repair waits for manager internal health before a missing proxy's first
  start. A command-double regression models absent static config after partial
  setup; another proves manager health failure stops before proxy/bootstrap.
- The apt test counted a removed ordinary-user sudo/git fallback. It now audits
  all three current package calls across bootstrap and root-only fresh-user
  preparation, preserving the bounded-lock requirement. Retry tests with denied
  Docker access now require exit 2, no sudo/Compose/bootstrap and unchanged index
  and owner data, rather than expecting the deliberately removed sudo fallback.
  The Tailscale HTTPS notice was incidental, not their failure cause.
- Both clean guests now get the same `mirror.gcr.io` Docker daemon mirror from
  cloud-init **before Docker is installed**; no registry credentials. The runner
  records the configured mirror plus proxy diagnostics, installs the candidate
  by immutable SHA and retains the existing VM-stop `finally` block. The mirror
  serves `rabbitmq:4-alpine` from forum (HTTP 200 with OCI-index Accept;
  `mirror-cache-probe.log`). Since the mirror is a cache rather than a complete
  replica, both guests also use the same maximum-three-attempt install retry
  for registry throttling only (60/120-second waits, original final exit code,
  no retries for permission/API errors). Six command-double cases exercise the
  retry, permanent throttle, immediate unrelated failure and pipeline exit code.
- Focused repaired/new regressions: **56 passed**, 19.38 s using
  `uv run --with pytest --with pyyaml python -m pytest -q
  tests/test_privsep_slice1_runner.py tests/test_get_sh_apt_locks.py
  tests/test_update_retry_completion.py tests/test_install_docker.py`.

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
