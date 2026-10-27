# Install Wizard OS alongside your existing Hermes

New named-user alongside installs use a separate `wizard-system` account for Runtipi, the App Manager and
Tailscale operator. Your Hermes account gets only the files-root group and an agent-scoped `wam` token, not
Docker or sudo. Use `wam apps|search|install|update|uninstall|start|stop|restart|logs|doctor`; do not bypass a
refusal with Docker or sudo. `wizard link` and `hermes update` stay at your own uid. Backup, links and builder
commands report “not yet available without server access” until their scoped APIs land. Existing layouts are
unchanged; full-host repair instructions are for the administrator, not an elevation route for the agent.

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

> Your Wizard is private: only your own devices can reach it. A free app called Tailscale makes that work.
>
> 1. Make a free Tailscale account, and sign in on the phone or computer you'll use Wizard on.
> 2. Open the link below and sign in with the same account. That connects this server, and you're done.

If status shows an owner action to turn on HTTPS for Tailscale (every brand-new Tailscale account starts with it off), send the owner this message word for word, with the link status shows, then keep checking. The install continues by itself once it is on:

> One switch in Tailscale gives your Wizard its private secure link.
>
> 1. Open the link below, signed in with the same Tailscale account.
> 2. Turn on MagicDNS if it is off, then turn on HTTPS Certificates.
>
> That's it. The install continues by itself.

5. Tell the owner: Hermes restarts once at the end, so this chat goes quiet for about a minute.

6. When status says done, send the owner this message, word for word, with the private Wizard link that status shows in place of `<link>`. Put the link alone inside a code block (three backticks on the line before and after), never as a plain clickable link, so the owner copies it into their browser instead of opening it inside the chat app. If status shows no link, send its message instead.

> Your Wizard is ready. Copy this link:
>
> ```
> <link>
> ```
>
> 1. Turn Tailscale on.
> 2. Paste the link into Safari (iPhone) or Chrome (Android or computer).
> 3. Save it as an app. iPhone: Share, then Add to Home Screen. Android: ⋮, then Install app. Computer: the install icon in the address bar.
> 4. Open Wizard from the new app icon.
>
> DO NOT USE IT IN THE BROWSER. Save it as an app first, or voice won't work.
>
> \*This step goes away soon, when the phone and desktop apps arrive.

## Never

- Never edit Hermes config by hand.
- Never reinstall or update Hermes.
- Owners update their own Hermes, never the installer agent: `hermes update` for a Hermes from the official Hermes installer (what `wam updates` names for it), or `wam hermes update` for a release-pinned source checkout.
- Never run any other script.
- Never improvise fixes.

If status says failed or stopped, send the owner the last 20 lines it shows and stop.
