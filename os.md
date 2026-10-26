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

If status shows an owner action to turn on HTTPS for Tailscale (every brand-new Tailscale account starts with it off), send the owner this message word for word, with the link status shows, then keep checking. The install continues by itself once it is on:

> One switch in Tailscale gives your Wizard its private secure link.
>
> 1. Open the link below, signed in with the same Tailscale account.
> 2. Turn on MagicDNS if it is off, then turn on HTTPS Certificates.
>
> That's it. The install continues by itself.

5. Tell the owner: Hermes restarts once at the end, so this chat goes quiet for about a minute.

6. When status says done, send the owner the private Wizard link it shows. If it shows no link, send its message instead.

## Never

- Never edit Hermes config by hand.
- Never reinstall or update Hermes.
- Owners update their own Hermes, never the installer agent: `hermes update` for a Hermes from the official Hermes installer (what `wam updates` names for it), or `wam hermes update` for a release-pinned source checkout.
- Never run any other script.
- Never improvise fixes.

If status says failed or stopped, send the owner the last 20 lines it shows and stop.
