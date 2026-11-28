# Agent-runtime's independent registry writer (parent activation only)

The running `wizard-agent-runtime.service` uses:

```
/usr/bin/python3 /home/telos/.local/share/wizard-agent-runtime/current/agent_runtime.py --driver runtipi --wam /home/telos/workspace/wizard-app-manager --agents-dir /home/telos/.wizard-os/agents --token-file /home/telos/.wizard-os/agent-runtime/runtime.token --tinfoil-key-file /home/telos/.wizard-os/agent-runtime/tinfoil.env --port 7794
```

No credentials were read. The public runtime source `RuntipiDriver.__init__` inserts `--wam` at `sys.path[0]`, then imports `wam.cli` and `wam.private`; install and lifecycle paths call the imported `cli.write_registry`. The current public runtime resolves to release `9f6855f4ed16806d89e6ee4b04deb9a2bdcb4e55`. The WAM tree actually selected is clean commit **888a61c948bd701f6f2e7cb6fd876795f43fdcc4**, NOT `9309d857…`.

The registry module on that old tree has SHA256 `b78947c53b55f951b72a1f6133a276af1964baf65d2ec17dd15849d9ff1445f3` and omits `receives_voice_input`. Running this branch's isolated test with `PYTHONPATH` pointing to that real old tree fails with the missing key for every declaration. Running it with this branch passes. The test uses real `cli.write_registry`, both writes, slots/routes projection, and the actual shared registry copy, but exclusively synthetic installed rows and temporary homes. It never constructs Agents or reads an agent home.

Other live executable writers:
- API: `/usr/bin/python3 -m wam serve`, PID 1439280 at inspection; cwd and explicit `PYTHONPATH` both `/home/telos/.local/share/wizard-search-releases/consolidated-20261105/wam-api-v2/975fcbd37a84f0a79bd593db317f27b500a2cac6`. The API, network watch and connections watch share that imported patched registry module.
- CLI: `/home/telos/.wizard-os/builder/current/bin/wam`, resolved bundle `fe78cab4e62b1abd99cdb47d09e200c93a031b07c97eaa395e57680ff24655fb`; its launcher prepends the bundle root to PYTHONPATH. Its registry is patched.
- Node App Manager image `sha256:85dda7d674915c1d4dcf4c9fe5cf49ae52da459e91ba53504799c5a0c853e370`: actual `/app/main.mjs` contains exactly the two apps.json mirror locations in its grants persistence method. That implementation spreads the existing parsed registry then changes `grants`. It contains neither `receives_voice_input` nor an independent Python writer. This is preserve-only, not a reconstruction fix.
- Other Python reconstructors (install/update/rename/restore/store/network/connection entrypoints) call their own imported `cli.write_registry`; they are not additional independent projection implementations. Any future executable launched from the old workspace must likewise use this exact-baseline fix.

This branch is based exactly on `888a61c9…`; its only runtime change is one boolean projection line in `wam/registry.py`. The already prepared `9cc81b08…` branch is based on `9309…`; replacing the runtime's old WAM with it would change 62 WAM files, including ~15k added lines. Do NOT use it for this runtime writer.

## Minimal parent delivery

Stage the complete immutable archive of this branch at a dedicated release root (same old source plus this one runtime line). Verify every `wam/` file except `registry.py` is byte-identical to `888a61c9…`, and the registry differs by exactly the reviewed insertion. No install, dependencies, Docker image, agent-runtime source, private mounts or model routing changes are needed.

Parent may then preserve the complete current service definition and override **only the `--wam` argument** to the staged root, keeping all other argv unchanged. Parent controls daemon reload/restart; no agent lifecycle calls are required. Restore the original ExecStart/drop-in to roll back. Never patch `apps.json` or make a shell-specific voice exception.

After parent activation, read back the actual process cmdline/import path, regenerate through supported CLI/API and allow the runtime's legitimate next write; verify both owner and shared `apps.json` plus `/wizard/v1/apps` keep Search's boolean true. A service restart alone is not persistent voice-admission proof. This branch and the isolated tests are preparation, NOT a claim that the live owner registry is fixed. The live `/wizard/v1/apps` still reports false at audit time.
