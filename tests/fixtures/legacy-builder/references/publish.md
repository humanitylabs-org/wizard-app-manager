# Publication approval boundary

Prepare with `wam publish <source> --repo owner/name --output <plan-outside-source.json>`. It makes zero network calls/public writes and binds a SHA256 over confined source files. Secret-named files and symlinks reject preparation. Inspect the files and source diff as well as lint; filename checks are not a secret scanner.

The payload explicitly names public visibility, exact repo, wizard-app topic, description, source metadata patch, catalog POST body and owner-signing requirement. No publisher identity is invented. NIP-34 euc/identity cannot be finalized before the approved repository exists, so signing is transparently a separate gate in Wizard's own signing sheet. No external signer.

After explicit human approval, the release owner re-hashes the exact source, stages the metadata source patch in an isolated copy, re-checks, scans full tree/history for secrets, creates and pushes the exact repo, records immutable commit, sets topic, POSTs catalog and reads its listing back. A success response alone is insufficient: commit/version/listed:true must match. Signing/credit is only claimed after actual valid signed-event verification. A second consumer installs that pinned public source through its lifecycle owner. Until then use a private source-bundle rehearsal, labeled as such.

`--execute` is refused. Public repo creation/catalog/signing executor is deliberately beyond this first candidate's authority; preparing a complete actionable payload is the bounded workflow shipped here.
