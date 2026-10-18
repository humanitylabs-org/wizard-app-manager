# Install Wizard OS alongside your existing Hermes

You are the owner's agent. Follow these steps in order. Keep every message to the owner short. Put each command and each link on its own line.

1. Run this read-only check. It changes nothing:

```
curl -fsSL https://aiwizards.com/os.sh | WIZARD_CHECK=1 bash
```

Tell the owner in one line what it found.

2. If the check says to stop, send the owner its message word for word. Then stop.

3. Start the install. Tell the owner it can take 10-20 minutes:

```
curl -fsSL https://aiwizards.com/os.sh | bash
```

4. Watch the output until it finishes. If it shows a Tailscale login link, send that link alone to the owner, then keep watching.

5. Tell the owner: Hermes restarts once at the end, so this chat goes quiet for about a minute.

6. When it is done, send the owner the private Wizard link from the final block.

## Never

- Never edit Hermes config by hand.
- Never reinstall or update Hermes.
- Never run any other script.
- Never improvise fixes.

If something fails, send the owner the last 20 lines of the output and stop.
