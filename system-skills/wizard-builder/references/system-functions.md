# Shared system functions — v1 (James-approved)

Read this **before** writing any app screen or port for WizardOS. This is a build rule, not an optional dependency suggestion. Apps implement the owner's task; Wizard implements platform behavior once. The approved `wizard.md` schema is unchanged. API/MCP/view-source proposals keep their separate opt-in approval gate.

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

## First-frame action acceptance (how the code must behave)

This is the behaviour every mutating control must have. It is not an automatic cross-app lint or release blocker, and
it is **not a per-app test plan**: the shared kit and the `--kind` templates implement it and are tested once in the
platform (`tests/test_builder_list.py`, the shared-client suites). For a single app, start from a kind, keep its
`enqueue` pattern and run `wam verify`. Never write new test harnesses or release packets for one app.

1. The handler changes visible task state synchronously, before any await; meaningful feedback, not a pressed flash.
   (Platform suites measure it within two requestAnimationFrame callbacks and no later than 100 ms, with I/O unresolved.)
2. Reversible finish actions update in place or return to the previous screen before I/O resolves; irreversible or
   external effects show honest pending feedback and no Sent/Saved claim before confirmed success.
3. Pending state survives a stale poll or refresh; a double tap is one logical operation (one `operation_id`).
4. After success the app reads back and shows no flicker, duplication or resurrection.
5. On failure, including a late failure after navigation, the correct state is restored, input kept, and one visible
   plain-language error with retry appears on the visible surface; retry has no duplicate effect.
6. These apply to every surface the control appears on (in-shell, standalone, widget/ready-card).

## Executable gate (wam verify)

1. `wam check <source>` always runs `wam.system_functions.check` in addition to manifest admission and builder
   confinement; builder-made apps also fail on homemade Back (`popstate`/`pushState` outside `WizardApp.back`),
   hand-rolled modals instead of `WzSheet`, and `alert/confirm/prompt`. `--proposal` adds the proposed API/view-source checks.
2. The lint checks inline HTML/SVG and JS/TS/component/CSS source, plus the builder's shipped inline Runtipi payload
   (decode-only; never execute it) for raw getUserMedia/MediaRecorder/AudioContext, navigator.share, CloseWatcher, SW
   lifecycle, install prompts, app theme switches/storage and root history guards. A filename is not a trusted
   exemption; only a release-owner path plus SHA256 can exempt exact shared core bytes during a platform audit.
3. `wam verify <source>` is the whole per-app pre-install check: container build, loopback start, API round trip with a
   container restart and readback, MCP tools/list, `wam check`, one phone-width screenshot per skin. If it passes,
   install and hand over; the owner using the app is the final check.
4. Platform owners (not app builders) keep the shared-resource browser suite: `node qa/builder/system-functions-browser.cjs
   --source <generated-source> --core <approved-core-directory> --out <receipt-directory>` and the shell's own matrix.
   Sustained Linux build/browser runs go to the configured build worker.
