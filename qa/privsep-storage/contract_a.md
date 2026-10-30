# Contract A: signed-out custody is a security dependency

REUSE the existing official Tailscale dependency installer, custody installer,
loaded-unit/socket/real Serve guards, immutable image attester and the two actual
Service consumers. EXTEND only their focused QA. No new access provider/mode.

`WIZARD_PRIVATE_ACCESS=none` means no private **app** publication/enrollment. The
host still installs the official Tailscale package and starts its daemon so
custody can inspect real persistent Serve state. It never invokes login, up,
auth keys, Serve, Funnel, SSH or operator assignment in this mode. CLI absence,
failed/malformed/unknown-type inspection and existing/partial contracts still
refuse. No anonymous approval path, fabricated owner identity or empty fallback.

Source audit found one prerequisite ordering defect: the separated root helper
called install.sh's mandatory custody verification **before** root installed
custody. Move the unchanged root custody call immediately before install.sh.
This retains acquisition-before-dependency in the custody installer and leaves
install.sh fail-closed. The real-root fixture asserts that ordering.

Cleanup projects are exactly `runtipi`, `wizard-agent-service_wizardapps` and
`wizard-ux_wizardapps`, derived from the finite campaign's app URNs by replacing
`:` with `_`. Network names are not project ownership. No broad prefix or
`_wizardapps` suffix selector. All deletion/readback uses observed full IDs.

## Staged proof, not executed acceptance

The parent alone may admit/submit `contract_a_run.py --execute`. Static preflight
parses/hash-binds complete source members, exact AST test IDs, inherited receipts,
archive config/layers, original disk hashes/stats, process absence and cumulative
capacity. It never imports/collects tests, boots VMs, invokes Docker or builds.

One fresh pair of thin overlays over the committed 851 disks, one ordinary boot:

1. Inherit exactly 103 passing IDs from the failed 0513 job; execute ONLY its two
   repaired real-UID cases, with zero skips/errors/failures. No new test IDs, no
   blanket 105 rerun. Compare full source/member/mode closure outside four QA
   paths, and complete module AST outside each repaired method (including shared
   helpers, imports and decorators). Bind failed receipt, XML, all 17 phase logs
   and native spool log. Failed historical evidence stays failed; a separate
   composite records 103 inherited + 2 fresh = 105, never 105 freshly executed.

   Manager fixture: the old child used an inherited cwd FD below private `/root`
   pytest ancestors and a relative config. The product writer passes that dir
   to tempfile, which uses an absolute pathname; an FD alone cannot grant ancestor
   search. Its historical exception was swallowed, so the precise historical
   frame/errno remains unknown. First run a bounded synthetic private-ancestor
   FD-cwd negative; require actual PermissionError/EACCES at `_mkstemp_inner` or
   stop before the positive/native continuation. Capture only bounded exception
   type, errno and frames over a pipe (no payload/locals/argv/env), with deadline,
   unconditional pipe closure and kill/reap. Then use a realistic absolute root
   under a NEW `/run` parent with 0711 search, manager-owned 0755 leaves and actual
   uid/gid 12340, no groups/effective/permitted/ambient capabilities. Assert actual
   lock/token file type, owner/group, mode and link count. Never chmod existing
   `/run`, `/root`, pytest ancestors or product controls to bypass policy. Preserve
   the causal negative and positive observations as XML properties.

   Git fixture: its captured failure was ENOENT for `runuser`; its isolated PATH
   excluded `/usr/sbin`, unlike the correct production environment. Add only fixed
   system sbin dirs and explicit binary prerequisite checks. Keep exact observer
   argv assertions, the nobody-only synthetic mapping, root dubious-ownership
   refusal before AND after real owner success, independent byte/symlink hashing
   and unchanged ownership. No safe.directory allowance or native identity change.
   A red checkpoint stops before image load or installer activity.
2. Reuse saved backend and Service archives. No rebuild of either. Export and
   attest exact config/platform/labels/ordered layers before AND after installation.
   Immutable attested engine handles must cross the real clean manager handoff;
   tags are locators only, never proof.
3. Invoke the actual root helper selected at get.sh's separated branch with the
   exact argv and `none`. A read-only Python profile observes only its real
   upstream subprocess call/return and custody call; no command/callable/return
   replacement. Capture root-controlled ancestors/control endpoints with numeric
   UID/GID/mode/ACL and directory identities pre/post upstream and after final
   setup. Verify complete root-fetched source closure before upstream. No manager
   writers beforehand, payload permissions are not changed by the QA observer.
   The root helper already chowns the clone before this checkpoint: read HEAD
   with `runuser -u wizard-system -- git -C ... rev-parse HEAD`, not root Git.
   Keep Git's ownership guard unchanged (no safe.directory exemptions); root
   independently hashes the complete source-member list, including symlink bytes.
   **Full get.sh discovery/prepare_server is not a new native claim**; its selected
   helper, actual install.sh and actual custody installer execute unchanged.
4. A conservative read-only listener/drop-in observer must see the real socket
   inode BEFORE the daemon dependency appears, or refuse. Reuse real loaded-unit
   checks. Restart the worker and require a different worker PID with the same
   socket inode. Reject anonymous and forged owner/origin request combinations
   with exact 403 owner-only responses and unchanged finite action-state records.
   Stop/start real tailscaled with the socket already bound and loaded ordering;
   require genuine NeedsLogin, empty Serve, no identity/operator/routes throughout.
   Socket-acquisition/reload/enable/final-verification failures remain isolated
   synthetic transaction fixtures, never native identity positives.
5. Execute exactly the two missing actual Service cases: compiled backend helper,
   physical binds, UID permissions and real HTTP synthetic history/artifacts;
   then stale/unavailable mapping refusal before update/restart mutations. Check
   original committed mapping/journal/completion/sentinel/image before/after;
   no storage transaction or request replay.
6. Stop native services. Create one exact QA-owned negative container with valid
   unrelated project `notes_wizardapps`; actual cleanup must remove campaign full
   IDs and retain it. Preserve the intentional partial report. Only its fixture
   removes that exact negative ID and verifies structural absence; same engine
   and final empty inventory are mandatory. Failed installs collect bounded
   partial cleanup under exclusive lock plus orphan-installer checks, preserve
   first failure, and never turn cleanup into a green acceptance.
7. Stop/reap the only VM and all boot PIDs; rehash/stat original disks and saved
   evidence. Old 474 container absence remains **UNPROVEN**, never booted/repaired
   retroactively. Its failed receipts/archives/logs remain untouched.

Inherit, never rerun/relabel: 278 Python + 3 causal + 12 production-storage,
27 image-identity regressions, six real signed-out prerequisites. Full owner
positive authentication, enrolled tailnet, reboot and release remain separate
unproven gates.

Capacity never resets the campaign cap or lowers reserve 217179869184. Original
remaining 66220945408 already excludes 851 allocation. Credit 474 evidence
2949689344 and the entire retained signed-out stage 207671296 ONCE only after independent complete
stat inventory equality. New stage/COW allocation reduces the remaining pending
original peak once; monitor free >= reserve + unallocated remaining at commands.
The retained, unexecuted 0ae contract-A stage is separately stat-inventoried and
credited ONCE as already-allocated campaign bytes, never deleted or rewritten.
The complete failed 0513 stage is also inventoried and its 53530624 allocated
bytes credited ONCE against that same remaining peak; no historical evidence is
deleted. Current source differs from 0513 only in this runner/doc and the two
failed test methods. All product bytes stay exactly 0513/0ae. The 0ae-to-current
comparison also retains the previously reviewed QA observer change. All 105 IDs
remain accounted for, with only the two failures eligible for fresh execution.
Whole new stage cap 8 GiB, per-QEMU-file cap 6 GiB; these are limits, not headroom
claims. Retain all old archives/images/evidence, no broad cleanup.
