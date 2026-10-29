# Chat in a Wizard app: it is the Chats thread

James, 22 Oct 2026, after Wisdom's homemade Ask AI sheet: "why for every app I ask you to build are
you implementing some bootstrap thing from scratch when we already have a fully functional chat
interface?… Our standard is way higher than this." The shared `wizard-ui.css` kit is styling only.
Looking like Chats is not being Chats: voice into the composer, attachments, model picker, Stop,
queue, offline outbox with retry, history, reply bubbles, skins and Back come only from the shell.

## Rules (each is checkable)

1. **No second composer.** Any feature where the owner converses with a model opens a real Chats
   thread through `WizardApp.nav.openThread(sessionId, {reply})`. `wam check` fails the
   `app-chat` rule on a chat-like textarea or chat transcript/composer markup. An exemption needs
   James's explicit sign-off recorded in the plan.
2. **The app owns context, not conversation.** The app's server finds or creates one session per
   item (same id on retry), stores only item → session id, and returns the item (passage, record,
   chapter) as a `reply` keyed `<app-id>:<item-id>` that rides with the first message as quoted data.
3. **Isolation is an agent, not a UI.** "Independent model", "no memory", "web only" or restricted
   tools are met by creating or reusing a registry agent (Agents app: model pin, permissions,
   privacy) bound to the app, and creating sessions under that agent. Building UI to get isolation
   is rejected. The plan names the agent the thread runs on; if it is the main agent, say why and
   tell James in plain words.
4. **Plans name the surface.** An approved plan involving conversation states which shell surface
   it reuses (hosted thread, Chats, reply item). If the shell lacks the bridge, extend the shell
   (Wizard UX repo, `HOSTED_APPS` + app-services admission) or return the blocker. Never fork.
5. **Acceptance = parity + faults, on the hosted shell.** Phone and desktop, all writes
   intercepted: the thread opens inside the app window; orb/mic dictation lands in that composer;
   attachment, model picker and Stop present; Back returns to the app and root Back never exits;
   reopening lands in the same thread. Fault injection: provider unreachable for two minutes,
   network dropped mid-stream, resend after error — the message stays queued, exactly one user
   bubble exists, and the reply arrives on recovery without manual action. A dead-end error string
   with no working retry fails regardless of wording. Template: Wizard UX
   `qa-wisdom-hosted-thread.cjs`.

## Shape (Wisdom reference implementation)

- App server: `POST api/<items>/<id>/thread {}` → `{session_id, title, started, reply|null}`;
  creates via `POST /wizard/hermes/api/sessions {source:'wizard-mobile', id, title, model, provider}`
  only on 404.
- App view: `const r = await api(...); WizardApp.nav.openThread(r.session_id, r.reply ? {reply: r.reply} : undefined)`.
- Standalone (no `WizardApp.hosted`) may keep a minimal fallback; it is never the hosted experience.
