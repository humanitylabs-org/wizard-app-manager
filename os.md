# Install Wizard OS alongside your existing Hermes

You are the owner's agent. Follow these steps in order. Keep every message to the owner short. Put each command and each link on its own line.

1. Run this read-only check. It changes nothing:

```
curl -fsSL https://aiwizards.com/os.sh | WIZARD_CHECK=1 bash
```

Tell the owner in one line what it found.

2. If the check says to stop, send the owner its message word for word. Then stop.

3. Start the install. It runs in the background and this command returns within seconds:

```
curl -fsSL https://aiwizards.com/os.sh | bash -s start
```

If it prints a command for an administrator instead, send that exact command and its message to the owner. They run it once in their own terminal. Never ask for their password in chat and never add passwordless sudo. You can keep using the normal status command below. Rootful Docker access is administrator-equivalent; unsupported rootless preparation stops safely instead of migrating Docker.

Tell the owner it can take 10-20 minutes.

4. Check progress about once a minute:

```
curl -fsSL https://aiwizards.com/os.sh | bash -s status
```

If it shows an owner action with a link (an approval link or a Tailscale link), send the owner action line and its link word for word, then keep checking. The owner types their server password only on that page, never in chat. If it shows a command for an administrator, send its message and the command word for word.

The first time it shows a Tailscale link, send the owner this short explanation once, word for word, before the link:

> To open your Wizard from your phone and laptop, this server joins Tailscale. Tailscale is free for personal use. It builds a private network just for your own devices: they talk to each other directly, peer to peer, and everything is encrypted. Your Wizard never goes on the public internet, so only devices on your Tailscale network can reach it, from anywhere. If you share that network with family or work, they can reach it too unless you limit access in Tailscale.
> 1. Open the link below and sign in. No account yet? Sign up free with Google, Apple, Microsoft or GitHub.
> 2. Install the Tailscale app on your phone and laptop, and sign in with that same account.

5. Tell the owner: Hermes restarts once at the end, so this chat goes quiet for about a minute.

6. When status says done, send the owner this message, word for word, with the private Wizard link that status shows in place of `<link>`. Put the link alone on its own line. If status shows no link, send its message instead.

> Your Wizard is ready. Your private link:
>
> <link>
>
> 1. Open it on a phone or computer that has the Tailscale app installed, signed in with the same account, and turned on. Anywhere else it won't load. If it doesn't load, open Tailscale and switch it on.
> 2. Open it in your normal browser, not inside this chat app: press and hold the link, tap Copy, then paste it into Safari (iPhone) or Chrome (Android). On a computer, just click it.
> 3. This chat shows no preview for the link. That's normal: it's private.
> 4. You'll first see a short guide to send your first message. Follow it, or tap Skip tour.
> 5. To keep it on your phone: in Safari tap Share, then Add to Home Screen. In Chrome tap ⋮, then Add to Home screen (or Install app).
> 6. Later: Setup is with your apps (swipe the orb up on a phone; Alt+O, or Option+O on a Mac). To see the guide again: on a phone, Settings, then Gestures & help, then Practice again; Alt+/ (Option+/ on a Mac), then Show me again on a computer.

## Never

- Never edit Hermes config by hand.
- Never reinstall or update Hermes.
- Owners can explicitly update the release-pinned host Hermes with `wam hermes update`; this is not an installer-agent step.
- Never run any other script.
- Never improvise fixes.

If status says failed or stopped, send the owner the last 20 lines it shows and stop.
