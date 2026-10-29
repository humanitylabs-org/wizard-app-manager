# Mandatory manager privacy (staged, not admitted)

REUSE: `DockerService.composeApp(up)` is the actual shared install/start/restart/update seam; its existing `WizardStorage.prepare` grant lock remains held through preflight and launch. Reuse Compose 2.40 `!override`, already used by Agent Service `private_seal.runtipi_override`, and that compiler's `tinfoil-sealed-v1` document. No new network renderer or watchdog.

EXTEND: before the lifecycle's destructive callback, read an immutable trusted enrollment from manager `state/mandatory-seals/<app-URN>.json`, compose the required definition last, validate the effective config and launch precisely the validated snapshot. Enrollment is not catalogue, installed app or optional user-config state and is outside app backup/restore paths.

NEW: a narrow mandatory-seal reader/validator, synthetic regressions and a trusted offline enrollment utility. There is no HTTP enrollment/removal API. Ordinary, unenrolled apps retain existing behavior. Catalogues cannot opt themselves out. Invalid/missing enrolled policy fails closed before stop/recreate. No daemon/core/provider changes outside this seam.

The initial policy freezes an approved compiled launch definition (including image, mounts, provider-only key location, sidecars and socket grants). Metadata updates and restart use that approved definition; a changed runtime definition requires a separately reviewed new policy, not automatic learning from optional configuration. This deliberately cannot silently adopt an unreviewed image/command/mount change from a catalogue update. Independent review and actual disposable API lifecycle evidence are required; this document claims neither admission nor runtime success.

Scope: isolated companion branch from the exact tested daemon fead192e9fb2d560fd3b875d092819b32e7189b6. Other active lifecycle work includes owner-update branches/PR325 and PR327; not modified or merged. No live activation/restart/migration, keys retired, core edits, owner data reads or merged branches. RUNTIPI_ADMITTED remains false. Remaining real provider/inference/image, combined-current migration and backup credential-retirement gates remain separate.
