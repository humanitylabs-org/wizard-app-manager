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

> Your Wizard is private: only your own devices can open it. For that, this server joins Tailscale, a free app that connects your devices securely.
> 1. Open the link below and sign in, or sign up free with Google, Apple, Microsoft or GitHub.
> 2. Install the Tailscale app on your phone and computer and sign in with the same account.

5. Tell the owner: Hermes restarts once at the end, so this chat goes quiet for about a minute.

6. When status says done, send the owner this message, word for word, with the private Wizard link that status shows in place of `<link>`. Put the link alone inside a code block (three backticks on the line before and after), never as a plain clickable link, so the owner copies it into their browser instead of opening it inside the chat app. If status shows no link, send its message instead.

> Your Wizard is ready. Copy this link:
>
> ```
> <link>
> ```
>
> 1. Make sure Tailscale is on, on this phone or computer.
> 2. Paste the link into Safari (iPhone) or Chrome (Android or computer), not inside this chat app.
> 3. A short guide helps you send your first message.

## Never

- Never edit Hermes config by hand.
- Never reinstall or update Hermes.
- Owners can explicitly update the release-pinned host Hermes with `wam hermes update`; this is not an installer-agent step.
- Never run any other script.
- Never improvise fixes.

If status says failed or stopped, send the owner the last 20 lines it shows and stop.
