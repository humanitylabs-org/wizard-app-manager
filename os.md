# Install Wizard OS alongside your existing Hermes

You are the owner's agent. Follow these steps in order. Keep every message to the owner short. Put each command and each link on its own line.

1. Run this read-only check. It changes nothing:

```
curl -fsSL https://aiwizards.com/os.sh | WIZARD_CHECK=1 bash
```

Tell the owner in one line what it found. Before starting, also say: "Wizard's private interface reads your Hermes session database so chats appear; provider credentials are not shared with the interface."

2. If the check says to stop, send the owner its message word for word. Then stop.

3. Start the install. It runs in the background and this command returns within seconds:

```
curl -fsSL https://aiwizards.com/os.sh | bash -s start
```

Tell the owner it can take 10-20 minutes.

4. Check progress about once a minute:

```
curl -fsSL https://aiwizards.com/os.sh | bash -s status
```

If it shows a Tailscale link, send that link alone to the owner, then keep checking.

5. Tell the owner: Hermes restarts once at the end, so this chat goes quiet for about a minute.

6. When status says done, send the owner the private Wizard link it shows. If it shows no link, send its message instead.

## Never

- Never edit Hermes config by hand.
- Never reinstall or update Hermes.
- Never run any other script.
- Never improvise fixes.

If status says failed or stopped, send the owner the last 20 lines it shows and stop.
