# Finite installer-health capture (`privsep-health-capture-v1`)

Implements `installer_health_diagnostic.md` as ONE native run, labelled
**`current-upstream-sealed-at-run`**: the historical 610 upstream base (setup
script, CLI, rendered base compose, base images) was never sealed and is not
recovered. The guest runs the unchanged product path and records what upstream
actually served, as metadata only. Product bytes stay those of
`610716410029f2f70fe4233102584c30e9b9022f`; every changed path is under
`qa/privsep-storage/` or `tests/test_health_capture*`.

| File | Role |
| --- | --- |
| `health_capture_collector.py` | Pure logic: Observer, deadline basis, finite log parser, migration membership, classify, closed export schema. No I/O. |
| `health_capture_guest.py` | In-guest driver. Profile hook on the root helper's exact `run()` calls only; the exported data is schema-validated. |
| `health_capture_run.py` | Host runner `--seal / --preflight / --execute`, with the capacity monitor, exact-ID cleanup, and preservation fences. |
| `health_capture_comparator.py` | Staging only. Takes public read-only snapshots and builds the sealed migration allowlist. |
| `tests/test_health_capture_collector.py` | 31 synthetic collector-only fixtures. They run in the native job before the VM boots, and any failure aborts the job. |

## Flow (guest)

1. **`fresh`**: checks the source closure of the sealed archive, and checks that the host has no BASE, no tailscale and no manager writer.
2. **`install`**: runs the real `prepare-alongside.py` once under `sys.setprofile`.
   - **`prepare-named-user.sh` call:** the fetched checkout must equal the sealed members. A /proc watcher hashes the fetched `setup.sh`.
   - **`prepare-named-user.sh` return:** records the `runtipi-cli` SHA, the version (finite regex) and the base compose SHA.
   - **line 291 call:**
     - Checks the exact argv and the exact sysenv key set.
     - Binds engine, boot and socket identity.
     - Records the 4 filtered lists and the pre-existing manager.
     - Renders `docker compose config --no-interpolate`, then derives the deadline from that rendered config, or refuses.
     - Starts the 1 Hz sampler.
   - **First incident:** the incident is frozen. If the manager is unhealthy with curl 22, one fixed localhost probe runs. The real command then gets a grace of at most 30s, after which our own child tree is quiesced.
   - **line 291 return:** a healthy return raises `HealthyStop`, so post-291 actions never run. An exception keeps the original exception.
   - **Post-collection:** a final failure-bound snapshot, then at most one log tail, then the H4 migration read, the image IDs and digests, and the in-guest healthcheck cross-check. All of this happens before any cleanup.
3. **`cleanup`**:
   - Stops our installer tree.
   - Creates an unowned negative control (the `notes_wizardapps` label from the service image).
   - Runs the owned exact-ID cleanup, which must refuse while the negative is present.
   - Deletes the negative by its full ID and reads back a 404.
   - Runs the owned cleanup again, then reads back absence.

## Deadline

Per gated service the budget is `start_period + retries*(interval+timeout)`.
- Gate 88 is `max(db, queue)`.
- Gate 116 is the manager budget.
- The total is gate 88 + gate 116 + 30s sampling + 30s post-collection, with a hard cap of 900s.

With the comparator values, db is 5+20*(5+10)=305 and runtipi is 10+20*(5+10)=310, giving **675s**.

The run refuses (INSUFFICIENT) instead of guessing if:
- the timings are unparseable or interpolated;
- a gated healthcheck is missing;
- the release index or `WIZARD_RELEASE_PINS` is present;
- any of the 4 services is missing;
- the total exceeds 900s.

If install.sh:84 rewrites the override, the config is re-rendered. A changed basis, or an inspected `Config.Healthcheck` that differs from the rendered one, sets a basis mismatch. A healthy run or a deadline hit with a basis mismatch gives INSUFFICIENT `deadline_basis_mismatch`.

The pre-291 watchdog is 900s, separate from the deadline.

## Result vocabulary

Results:
- `REPRODUCED_GATE88_RUNTIPI_DB`, `REPRODUCED_GATE88_RUNTIPI_QUEUE`
- `REPRODUCED_GATE116_RUNTIPI`, `REPRODUCED_LINE118_RUNTIPI_REVERSE_PROXY`
- `NOT_REPRODUCED` (never "fixed"), `INSUFFICIENT`, `DEADLINE`

Each result carries a finite `result_reason` and, for incidents, a `cause_class`:
- `H1_DB_*`
- `H2_MANAGER_{EXITED,CONNECT_FAILED,CHECK_TIMEOUT}`
- `H3_MANAGER_HTTP_{503,ERROR}`
- `QUEUE_*`, `PROXY_*`, `OOM_KILLED`, `UNCLASSIFIED`

Hypotheses H1–H5 each take finite values.

## Never exported

- `State.Error`, `Health.Log[].Output`, `Config.Env`, any label other than the two Compose identity labels, argv, and log text.
- Healthcheck test contents. Only canonical hashes and booleans leave the guest.
- Migration names outside the sealed allowlist. These are exported as `unknown_count` only.
- Free-text errors. Collector errors use the finite `COLLECTOR_ERRORS` set and are appended separately.
