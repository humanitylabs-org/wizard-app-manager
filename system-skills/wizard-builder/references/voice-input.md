# Receives voice input

Status: **Approved by James, 15 Oct 2026** (capability name, shell-independent routing and app-owned results). First implementation: Manager, Video Editor and Search. Shell interaction choices below are the Wizard UX trial, not requirements on other shells. Canonical contract: humanitylabs-org/wizard-os, spec/VOICE_INPUT.md, DECISIONS #62.

## Declaration and ownership

```yaml
receives_voice_input: true
```

An optional, strict boolean in `wizard.md`. Missing or `false` leaves normal shell routing unchanged. `true` requires a declared `view` with path and port. The installed registry and Agent Service app projection carry a boolean with the same name; strings such as `"true"`, integers and null are invalid declarations. The user-facing label is **Receives voice input**.

This declares an input capability. It does not declare an orb, microphone implementation, agent identity, model or notification channel. A shell can expose a button, orb, keyboard shortcut, hardware control or another deliberate voice gesture. The shell owns permission, capture, transcription, custody and destination choice. The app owns interpretation, work and feedback. Receiving transcribed speech does not itself require the app's `needs` list to contain `mic`.

The shell never creates a Chat merely to report an app action succeeded. Search can show results; a watchlist can add a film and acknowledge inside the app; an editor can show pending feedback beside its clip. An app may choose its existing agent workflow and reply channel, including an appropriate Chat thread. This is separate from `agent_binding` (which agent receives the app's work) and the owner's grants (which agents can use the app). The capability grants no new model, app, private-agent or external-action authority.

## Shared client contract, first implementation

```js
WizardApp.voiceInput.register({
  snapshot() {
    // Called when the shell seals a recording destination.
    // null means this view deliberately has no voice recipient.
    return { label: 'Video Editor · selected clip', clipId, draftId, time };
  },
  async receive({ request_id, text, target }, authorize) {
    // target is the bounded deep-copied snapshot, not the current selection.
    // Validate target and reconcile request_id before any repeat operation.
    authorize(); // immediately before the side effect, including after awaits
    await admitDurably({ request_id, text, target });
    showProgressInThisApp(request_id);
    return { accepted: true, request_id };
  }
});
```

The example's `admitDurably` and `showProgressInThisApp` are app-owned pseudocode, not additional shared APIs. The existing `conversation.register` receiver remains a compatibility alias during Manager migration.

- Shell admission requires both the installed declaration and the registered, authorized app frame/document. A page cannot grant itself the capability or supply an arbitrary delivery URL.
- A receiver snapshot is bounded plain JSON with a short human-readable `label` and app-defined stable context. The shell deep-copies it. App implementations validate their context before accepting it. Intentional `null` means ordinary shell routing; a declared but broken/unavailable receiver produces a visible recovery state, not silent delivery elsewhere.
- The destination and context are captured at recording start. Ordinary focus, selected clip, draft and playhead changes do not retarget a take. An explicit user bypass gesture may override the route. Once sent, retries retain the exact destination, request ID and payload.
- v1 transports text (up to the existing 12,000-character shell limit), not an app-owned microphone stream. Unsupported files or oversized text remain recoverable; the shell does not strip them and pretend full delivery.
- `authorize` verifies the original registration immediately before a side effect. Reload, navigation, uninstall or receiver replacement can revoke it. The first implementation retains input for recovery rather than silently rebinding an old take to a new document.
- `{accepted:true,request_id}` means durable admission, not completed work. The app must deduplicate replay of the same ID/payload and reject conflicting reuse. App feedback distinguishes pending, completed and failed work.
- The shell retains original audio and transcript until verified admission. Lost acknowledgement is an uncertain send: retry reconciles the same request identity. It never creates a fallback Chat.
- Apps without this capability continue working. A shell without this capability leaves an app's existing typed controls usable. Capability availability and first-use guidance must be honest.

## Wizard UX trial (not the portable standard)

A normal tap speaks to the active voice-capable app. An app-target color and visible destination label make that routing apparent. Existing insertion-only field dictation and stationary hold-for-system-command behavior remain intact.

For an explicit general-agent bypass, the trial uses **hold → slide onto the general agent target → release to keep recording → tap to send**. The release latches rather than sending; recording uses one continuous buffer. This is gesture-based routing, not instant recognition of an agent's spoken name. The target's actual label comes from configuration; other shells can choose a different interaction. Accessible controls offer the same explicit destination choice. Cancellation/Back retains recoverable input and stays in the app.

## Acceptance and rollout

1. Declare/check the capability and preserve it through installed metadata. A similarly named app or self-declared page cannot capture voice.
2. Show actual foreground destination on phone and desktop; record, change focus, send and verify the original app received exactly one request.
3. Manager acknowledges within its conversation; Search updates results within Search; Video Editor binds feedback to the frozen clip/draft/time and uses its existing in-app work history. No synthetic success Chat is added by the shell.
4. Exercise explicit bypass, stationary commands, field dictation and Back. The latch release does not send; the subsequent tap sends once with the full recording.
5. Fail admission and lose the acknowledgement after admission. Audio stays recoverable and a retry cannot duplicate the operation.
6. Test both disabled/absent declaration and unavailable receiver; test reload/revocation separately from intentional inactive `snapshot() === null`.

Contract publication, checker enforcement, installed metadata support and live app adoption are distinct milestones. The Wizard App Manager mirror is reviewed through its owner's draft-PR process; private launcher metadata can exercise the approved contract before that installer update lands. This does not imply every existing install already receives the new metadata.
