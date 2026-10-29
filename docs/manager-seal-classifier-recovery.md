# Baseline classifier repair and immutable-artifact composite

## Outcome

The baseline failure was the intended public-network bypass, not a new product regression. Vitest abbreviated the unequal network arrays to `expected [ …(3) ] to deeply equal [ …(2) ]`; the former classifier incorrectly required `_public_app` in that formatter output. No Docker reproduction, dependency install, bundle build or heavy submission was needed or performed for this repair.

`manager_seal_baseline.py` now requires ALL of: the exact machine-report suite/test inventory; parsed exact assertion/callsite stack frames; immutable fixture source SHA; source-defined role network policy; readiness/phase identity; complete inspected container IDs; source-labelled creates; and full connect/inspect container+network identity agreement. It does not accept arbitrary test failures, names containing a substring, or unrelated public-network events. Both QA runners call the same classifier with captured trace and fixture bytes. Fixed trusted diagnostics include the new classifier module. Product/fixture/lock bytes and Git/export modes are unchanged. QA inheritance admits only the explicit new verifier/test/document paths.

## Exact assertion and captured real identity

Pinned fixture `packages/backend/src/modules/docker/__tests__/mandatory-seal.test.ts`, SHA256 `baaafa4c5e8fd7e597890226a9db8aa014a5c34257d1264f2c1a8517069ff14e`:

- Machine report: exactly one failed test, exact suite `mandatory seal direct manager lifecycle (real Docker)`, exact title `disabled, removed and malicious optional config cannot attach a transient public network`; one passed round-trip control, 17 skipped, 19 total.
- Stack frames: `inspect` **579:59**, called at **603:7**. Line 579 compares each inspected row against `doc.services[row.service].networks`, not a general no-error assertion.
- The source populates `expectedContainers` in inspected `rows` order **before** executing that comparison loop. The first row is therefore the exact throwing row: container **265fe00a66db17a0ee3e8ab5f7db5c7227f508b8af1bede471e64a700f6c6ae3**, source-labelled role **agent-synthetic-search**. Its source policy allows only the provider and keyed networks. This is a three-observed versus two-allowed mismatch.
- That same ID appears in `expectedNetworkIds` (despite its name, this is **observed inspect inventory**), its exact fixture/project/service create event, and a captured connect to **0179d55b65abdaeaecd74f135be1fe2e854d2735a4c02496257b25851377b692**, named `agent-synthetic_seal3cfecc950ebb_public_app`. Its two legitimate network IDs match inspect and connect events as well.
- The previously highlighted container **0012a409ddc34f9f7451bbb9fda8f945c5f6170e8dae679e02734781a906a1d3** is the distinct **agent-synthetic-tinfoil** role, also permitted only provider/keyed, also attached to that exact public network. It corroborates the bypass but is NOT the first throwing row. The five owned roles all have captured public_app connections; main additionally has public_main. The full per-container correlation is retained in the derived JSON.

The baseline aborted at first inspect, before final drain. This establishes the positive real attachment and exact original assertion failure. It does not establish a complete no-exposure observation window. The successful candidate's complete event-window/checkpoint evidence is inherited separately, not inferred from this negative trace.

## Preserved saved bytes

Read-only copies and scoped raw traces remain outside Git at `/home/telos/.hermes/cache/scratch/incognito/manager-seal-classifier-evidence/`. Remote original paths remain unchanged under `/srv/work/manager-seal-3cfecc95-baseline-compat/manager-seal-early-results/` and its `saved/` directory. Direct remote SHA256 readback matched all seven local pinned input files after offline verification:

| baseline input | SHA256 |
| --- | --- |
| docker-receipt.json | c83bd3231edbc8637720978a0712773679515d90e01208161a0b033b7cb165d9 |
| docker-artifacts.tar.gz | 91838ebcdb3f243141f92897a2a194934318ca2367d2045225e53afce92b2c4d |
| docker.log | 902b14e4777e8f22eccb2b87d34e6cc00484cdf72f752a437a4454db17892f19 |

| inherited candidate input | SHA256 |
| --- | --- |
| SOURCE.json | 0feee5f3c55834e9164f4e45d7ea5564790040f95ac0ef9bfb2fbc465e2c23b3 |
| docker-receipt.json | 76bee4c997f2c176838c0e90a348b9fb2b1250005dca045e0db97d9639c3e6cf |
| docker-artifacts.tar.gz | 168a63a43777a34a1bf3b5a3fa6b5daef6f52339828f9a761a3a36024754de44 |
| docker.log | d3a0ac71040441040291e2bf3dc6f6c5c3b2e5bdd3f38422c39c465ec58e9cbd |

The baseline source manifest SHA is `e6ffbbdc8fd4b2dba9381a2ef5c93e0bfaf1212cd68668cd3972bc869c96d475`, source `3cfecc950ebbd91b2bbc0cd62ba0e834b572cd49`. The verifier checks all preserved exported source bytes and baseline archive, unchanged production/fixture closure to the original candidate, and Git/export modes. Local checkout permissions include inherited group-write bits; those are not misrepresented as exported modes. The parent supplied outer heavy-log SHA `123fa3b6cb915306c6fbd7068f4ca8823b853a37a5053e6814ff7b3721889bba` is context only, not an independently fetched child input or composite dependency.

## Separate derived verdict and reproduction

`docs/manager-seal-classifier-composite.json` is a NEW offline aggregate, not a rewritten worker receipt. `scripts/manager_seal_composite.py` verifies raw input pins, bounded in-memory archive member types/paths/sizes, exact inner/outer receipts, step/dependency log hashes, pinned saved bundle bytes, original machine counts and checkpoint inventory, exact prior rollback negative, and cleanup/reserve assertions. It reads saved input bytes again before writing a new output with exclusive-create semantics. No archive-controlled extraction or source write occurs.

Example, with a **new** output path:

```
PYTHONDONTWRITEBYTECODE=1 python3 scripts/manager_seal_composite.py \
  --evidence /home/telos/.hermes/cache/scratch/incognito/manager-seal-classifier-evidence \
  --pinned-source /home/telos/.hermes/cache/scratch/incognito/manager-seal-baseline-3cfecc95/source \
  --output /home/telos/.hermes/cache/scratch/incognito/manager-seal-classifier-evidence/composite-review.json
```

The composite covers original candidate **19/19**, **22 checkpoints**, adjacent **60/60**, the exact prior rollback negative, source-bound saved bundle, and now-proven baseline bypass. Historical failed receipts retain their false pass/admission labels and failed container exit. Source restoration after the old classifier was never reached; no successful worker completion is invented. Both saved receipts independently report no leaks, fixture volume absent, reserve preserved. There is no fresh Docker, official-image, authenticated API or installed update-transaction claim.

## Bounded checks and remaining gaps

- New adversarial classifier tests: **10/10**. Include formatter truncation, unrelated failures even with valid trace/public-network text, wrong source/frame/callsite/suite/column, wrong container/network/service, arbitrary other-fixture attachment, missing inventory/event/trace/sentinel, duplicate events and wrong counts.
- Focused QA: new 10 + existing bundle 24 + Docker harness 20 = **54/54**, local Python **3.14.7**. Seven edited Python files parse with Python 3.11 grammar. This is NOT a new actual-Python-3.11.2 classifier execution claim. The saved continuation's original **44/44 on actual 3.11.2** remains preserved.
- An additional broad local discover ran **72 tests: 71 passed, 1 error** in unchanged `test_manager_seal_runners.py::test_headers_reject_escape_links_and_inflated_size`: Python 3.14 rejects construction of a deliberately payload-less nonzero tar member at `tf.addfile`, before its tested helper executes. Its bytes are identical to pinned HEAD (SHA `10a6697822a7870b8470c680f652ef2cee955daaee2d071717ae69ae18c3360c`). Not silently fixed or relabelled passing; not a Docker/product failure.

The prepared next lane is `docs/manager-seal-installed-acceptance-spec.md`. It reuses the existing source/bundle for real installed Nest/authenticated HTTP/PostgreSQL/RabbitMQ update transactions, with unchanged authority/resource ceilings. Its infrastructure/asset/pull/fault-injection admission inputs are explicitly missing; nothing is submitted. Most importantly, `mandatory-seal.ts:144` refuses a second distinct revision while a previous anchor exists: no trusted installed-transition/ack implementation exists. One accepted V1→V2 and a V3 refusal cannot satisfy **successive accepted updates**. That product gap, official image/full installation and `RUNTIPI_ADMITTED` remain open.
