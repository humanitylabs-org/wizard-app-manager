# Shared system functions — v1 (James-approved)

Read this **before** writing any app screen or port for Wizard OS. This is a build rule, not an optional dependency suggestion. Apps implement the owner's task; Wizard implements platform behavior once. The approved `wizard.md` schema is unchanged. API/MCP/view-source proposals keep their separate opt-in approval gate.

## Load, trust and ownership

```html
<script defer src="/wizard-os-home/core/app-client.js"></script>
<script defer src="app.js"></script>
```

`window.WizardApp.version === 1`. `WizardApp.ready({name, version})` negotiates the actual host; `WizardApp.hosted` describes the negotiated connection, not an iframe guess or shell brand. Load this before app code. Do not copy a platform implementation into an app, even when renamed `app-client.js`. The preview/standalone gateway must serve the **same approved resource** at this URL; an arbitrary bare app server is not the complete standalone environment. Missing core is an explicit preview configuration error, not permission to add local fallback code.

For standalone capture, load the approved `/wizard-os-home/core/app-capture.js` resource before the client; do not access its singleton directly from app task code. Apps without capture do not need a recorder module. The shell loads `app-capture.js` before `app-services.js` and calls `WizAppServices.attach(iframe, {appId, path})`; its return callback detaches. The host verifies the exact attached Window **and** the current registered app URL/origin on every privileged call. Same-origin gesture calls use a synchronous facade. Cross-origin message transport is source/origin/schema checked and bounded; messages do not confer owner or agent authority. Do not expose arbitrary parent globals or use shell DOM selectors. Appearance v1 remains authoritative.

Apps own domain routes, forms, overlays, content and backend/upload/transcription APIs. The shared assets own root exit guards, permission, capture persistence, native/browser sharing, skin state, install prompts, service workers and update reload policy. The scanner is source lint, not a sandbox: obfuscated/dynamic code requires review, and a passing lint never substitutes for browser tests.

## Function reference

| Function | Use / return | Activation, trust and state |
|---|---|---|
| `ready({name,version})`, `version`, `hosted` | Negotiate v1 before platform use; no invented shell-type names. Do not assume a particular return shape for `ready`. | Frame detection alone is not privileged attachment. Missing/not-ready host must not exit an app. |
| `back.register({canGoBack,back})` | Both callbacks synchronously return honest booleans. Returns an unregister function. Close app-owned nested state; false at true root. | No gesture requirement. Return true only after an actual domain step. Do not guess from layout changes. |
| `back.push(fn)` | LIFO overlay callback; returns `{release()}`. Release when overlay closes normally. | Domain overlay only; do not implement a second exit guard. |
| `back.useHistory({isRoot})` | Marks app navigation entries and adapts an existing router. App route history and `popstate` rendering remain valid. | Only core owns root sentinels/CloseWatcher. Never rearm fake entries from app `popstate`. |
| `theme.get()` | Read current Appearance v1. Use semantic `--wz-*` roles, presentation and color scheme. | No app-local persisted skin/dark/light state. Preserve app typography, art and layout. |
| `theme.onChange(fn)` | Apply live Appearance v1 changes without document reload. | Host source, exact origin, schema and revision checks belong to core. Keep draft, scroll and domain state. |
| `share.text({title,text,url})` | Promise of `'shared'`, `'copied'`, `'cancelled'` or `'unsupported'`. A URL remains a URL, not silently flattened into text. | Invoke immediately from the click/tap, before unrelated awaits. Native wait is bounded. Clipboard resolves copied only after write completes. AbortError is cancelled; rejected/missing capability is unsupported. |
| `share.file(file,{title})` | Same outcome Promise, file payload retained. | Same gesture/trust rules; never report cancelled as success or download as shared. |
| `update.hold(reason)` | Returns `{release()}`. Acquire while app has unsaved work; release only after save/readback or explicit discard. | No elapsed-time expiry. Core automatically holds during capture. Frame removal/unload or shell restart reconciles holds. Suspect liveness is diagnostic, not permission to reload/discard. |
| `update.onAvailable(fn)` | Observe availability, not an order to reload now. | Central shell policy decides application. No app SW, install prompt or forced timer reload. |
| `capture.start({kind,mime,chunkMs,maxMs,meta,audio,...})` | Promise of Session (below). Options select task capture; adapters preserve PCM/native accuracy. | Call from user gesture. One physical recorder across all apps; busy identifies holding appId. Permission/durable chunks belong to core. |
| `capture.pending()` | Promise of `Result[]` for this app, including recovered partial results. | Recovery is explicit; background/Back never discards a recording. |
| `capture.ack(id)` | Promise of void. Acknowledge only after the task backend has durably stored and read back the result. | Scoping is enforced by registered appId; ack is not merely “upload started”. |

Capture Session: `{id, onLevel(fn), stop()->Promise<Result>, cancel()}`. Capture Result: `{id, blob, mime, durationMs, startedAt, meta, partial?}`. Do not invent a return type for `onLevel` or `cancel`. The core implementation is `WizAppCapture.start(appId, opts)`, `pending(appId)`, `ack(appId,id)` in `app-capture.js`; optional `getState()` feeds automatic update holds. Apps use `WizardApp.capture`, never the host capture singleton directly.

## Domain usage (not a platform implementation)

```js
WizardApp.ready({name: 'my-app', version: '0.1.0'});
WizardApp.back.register({
  canGoBack: () => selectedRecord !== null,
  back: () => { if (!selectedRecord) return false; selectedRecord = null; renderList(); return true; }
});
WizardApp.back.useHistory({isRoot: () => router.atHome()});
const overlay = WizardApp.back.push(() => { closeEditor(); return true; });
// When the editor closes using its on-screen button: overlay.release().
shareButton.onclick = async () => {
  const outcome = await WizardApp.share.text({title: 'Record', text: currentText, url: currentURL});
  showShareOutcome(outcome); // cancellation is not a success message
};
// Keep task durability in the app; hold lifetime follows actual unsaved state.
const hold = WizardApp.update.hold('unsaved editor');
async function saveDraft() { await saveToTaskAPI(); await readBackSavedDraft(); hold.release(); }
async function submitCapture(result) {
  await uploadToTaskAPI(result.blob, result.meta);
  await readBackDurableTaskResult(result.id);
  await WizardApp.capture.ack(result.id);
}
```

## Appearance and standalone

The accepted shell skins are Nebula (default), Grove, Tide, Ember and Signal; Paper is retired. The shell registry remains the source of truth. Light/dark protocol fixtures may use an explicitly labelled synthetic skin; never present Paper as an available skin. Hosted html/body are transparent; only app surfaces are drawn over shell glass. Match light **and** dark selected skins, and do not add a title bar/header that repeats the shell's app title. Fullscreen/standalone may retain an existing useful header. Apps can adopt reading/display type roles without replacing intentional reader typography. `boot.js` marks frame presentation before first paint; the shared client owns Appearance v1. The template has a dark/light CSS fallback selected by system media preference when no host has provided appearance, but no theme switch/storage.

Standalone uses the same core client and capture resource, not an app-local imitation. Core provides Back root fallback (first Back warns/stays; a second within ~2 seconds may leave a plain browser tab), shared browser share/capture fallback and local appearance defaults. Hosted Back closes overlay → nested view → app root and stays. Off-origin Back timeout means stay, never exit. Trusted same-origin `WizAppServices.back(iframe)` returns handled/atRoot/notReady synchronously; cross-origin returns pending/stay. Honest legacy `__wizardCloseOverlay` domain callbacks can bridge adoption; no layout-signature guessing.

The one-screen template registers an honest at-root callback without attaching a router adapter it does not need. Only routed apps call `back.useHistory` with their actual root predicate; stamping a one-screen root as route history can mask the standalone warning. The template acquires an update hold for its unsent form draft, retains it on save/readback errors, and releases it after task API readback or explicit input discard.

## Updates and durable capture

Never force reload due to a timer. Recording and unsaved state survive Back, background and visibility changes. The capture core requests persistent storage, stores bounded chunks in IndexedDB and recovers partial recordings after shell restart. Prior holds become recoverable, not active; a missed bounded liveness ping marks suspect and shows diagnostics, retaining chunks and recorder until frame absence is established. No automatic acknowledgement or expiry of unsaved recordings.

Hosted apps contain no SW/install handling. The shared gateway installer must return before scheduling service-worker lookup/registration or install listeners in any iframe; do not inject per-app exceptions. This suppression is not privileged host detection and does not unregister workers. Top-level standalone keeps standard shared-resource fallback and gateway-managed installation where available. Central migration cleans **only** explicit known legacy scope/cache names recorded for a registered app path; no runtime prefix guesses, root shell or unrelated registration removal. Standalone updates still use shared policy and must respect holds; this does not authorize an app-owned worker.

## First-frame action acceptance (required behavioral evidence)

This is behavioral acceptance, not an automatic cross-app lint or release blocker implemented by `wam check` or the existing browser harness. Those tools do not discover and prove arbitrary domain handlers. Do not claim all apps are covered or released because their source lint passes. Apply this evidence requirement to new/changed controls and their applicable surfaces; no all-app sweep or new action framework is required.

1. Inspect the real click/submit/gesture handlers and their API/storage/bridge call sites. Record every new/changed mutating or I/O control in the build/PR evidence as `control | handler path:symbol | surface | held-I/O test command/case | pass receipt`. Include widget/ready-card, standalone and in-shell variants when present; mark a surface not applicable with a reason. A full-app-only test does not establish widget behavior. Missing rows, executable cases or receipts mean incomplete behavioral acceptance, even if build/source lint passes.
2. Exercise the actual control/handler, not a replica. Intercept its real request or hold its actual dependency Promise unresolved (e.g. Playwright route interception with an explicit release barrier). Do not fulfill it immediately or use a fixed sleep as proof. While it remains unresolved, assert meaningful visible task feedback within two requestAnimationFrame callbacks and no later than 100 ms after the tap; record elapsed time and outstanding request state. The implementation still updates synchronously in the tap handler: this measurement is a test allowance, not permission to wait two frames. A generic pressed flash/spinner disconnected from the operation does not pass.
3. For reversible finish actions assert the local state change and previous-screen navigation before releasing I/O; for a list assert removal/update in place. For irreversible/external effects assert immediate honest pending/locked feedback and no Sent/Saved/completed claim before confirmed success. Preserve required user activation for shared capture/share calls; do not defer them behind unrelated awaits.
4. While I/O is held, deliver a stale poll/refresh with pre-action state: pending state must survive. A double tap produces one logical operation with no duplicate side effect. Release success, then perform durable readback; assert no flicker, duplication or resurrection and release durability/update holds only after confirmation.
5. Reject the operation in a separate case: restore the correct state, retain retryable input and show one visible plain-language error with retry. Also navigate away/close the widget before rejecting: verify late failure is delivered on the currently visible authorized surface (not an invisible frame toast) and restored state is present on return. A late success must not reopen the view or resurrect a removed item. Exercise retry without duplicate effects. Keep reconciliation alive beyond the initiating view's lifetime; a success-only test is insufficient.
6. Record candidate/source hashes, host, exact commands/cases and pass/fail output for every inventory row. Sustained browser acceptance runs on forum via `wizard_heavy_submit host="forum"`; fixture-only, full-shell and real app/widget receipts remain distinct. Review `await` before feedback as a useful warning sign, never as a source heuristic that proves behavioral compliance.

## Executable and browser gate (required before staging)

1. `wam check <source>` always runs `wam.system_functions.check` in addition to unchanged manifest admission and builder confinement. `--proposal` alone adds the still-proposed API/view-source checks. New/scaffold, stage, update-local and publication preparation all pass through this gate. No new manifest field is required.
2. The lint checks inline HTML/SVG and JS/TS/component/CSS source, plus the builder's known shipped inline Runtipi payload (decode-only; never execute it) for raw getUserMedia/MediaRecorder/AudioContext, navigator.share, CloseWatcher, SW lifecycle, install prompts, app theme switches/storage and root history guards. A filename is **not** a trusted exemption. The normal builder has none. A release-owner supplied absolute path plus SHA256 can exempt exact shared core bytes during a platform audit; the app cannot supply this map. Fixtures test approved core versus an app-local same-name copy and valid route/overlay callbacks.
3. Browser checklist: phone + desktop, dark + light, window + fullscreen; document/draft/scroll survive appearance changes, transparent canvas, no duplicate title. Open nested route/overlay, press Back and on-screen close, assert same step and app still open; root Back stays. Test text/file sharing and cancelled/unsupported without success toast. Test capture permission denial, busy, levels, stop/cancel, background/visibility, reload recovery, durable upload readback before ack. An update never reloads held/recording state. Explicit legacy cleanup preserves unrelated/root workers.
4. `python3 -m pytest -q tests/test_system_functions.py tests/test_builder.py` tests lint and its CLI/staging wiring. `node qa/builder/system-functions-browser.cjs --source <generated-source> --core <approved-core-directory> --out <receipt-directory>` runs generated view + real shared client/host/Appearance resources against an isolated real local task API, with a clearly labelled test host registry/DOM. It is not the full production shell matrix; the parent/platform owner still runs that checklist. Fixture and actual shared-resource receipts must be labelled separately; no production POST/agent writes.
5. Record execution host, exact candidate SHA/resource hashes, commands and receipts. Sustained Linux build/browser runs on forum via `wizard_heavy_submit host="forum"`; use Thales only for macOS/Apple-silicon work. Stage only; public pushes, merging and daily deployment need parent/release authority.
